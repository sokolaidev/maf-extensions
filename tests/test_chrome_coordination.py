"""Chrome delivery uses committed image evidence without waiting inside the writer lock."""

from __future__ import annotations

import copy
import io
import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import chrome_update as chrome  # noqa: E402
import image_remediation_evidence as evidence  # noqa: E402
import report_remediation_attention as attention  # noqa: E402
from container_release_history import encode  # noqa: E402

AT = "2026-11-27T06:00:00Z"
SOURCE = "a" * 40
PROFILE = "document-export"


class ChromeCoordinationTests(unittest.TestCase):
    def setUp(self):
        self.run: dict[str, Any] = {
            "id": 123,
            "run_attempt": 1,
            "status": "completed",
            "conclusion": "success",
            "updated_at": AT,
            "head_sha": SOURCE,
            "head_branch": "main",
            "head_repository": {"full_name": "sokolaidev/maf-extensions"},
            "path": ".github/workflows/container-image-monitor.yml",
            "event": "schedule",
        }
        self.catalogue: dict[str, Any] = {
            "current": {PROFILE: "image"},
            "releases": {
                "image": {
                    "registryDigest": "sha256:" + "b" * 64,
                    "latestAttempt": {
                        "monitorRunId": "123",
                        "monitorRunAttempt": 1,
                        "outcome": "vulnerable",
                    },
                }
            },
        }
        self.client = SimpleNamespace(request=self.request)

    def request(self, endpoint):
        self.assertEqual(endpoint, "repos/sokolaidev/maf-extensions/actions/runs/123/attempts/1")
        return encode(self.run)

    def receipt(self):
        return {
            "source": SOURCE,
            "acquiredAt": AT,
            "oldestAssessment": AT,
            "monitorSelection": "catalogue",
            "monitor": evidence.committed_monitor(self.client, self.catalogue, [PROFILE]),
            "images": evidence.catalogue_images(self.catalogue, [PROFILE]),
        }

    def assert_current(self, receipt):
        snapshot = SimpleNamespace(catalogue=self.catalogue)
        with (
            patch.object(evidence, "now", return_value=AT),
            patch.object(evidence, "main_head", return_value=SOURCE),
            patch.object(evidence, "History", return_value=SimpleNamespace(head=lambda: snapshot)),
            patch.object(evidence, "latest_monitor", side_effect=AssertionError("queued run read")),
        ):
            evidence.current(self.client, receipt)

    def test_queued_monitor_does_not_replace_committed_evidence(self):
        self.assert_current(self.receipt())

    def test_changed_digest_or_assessment_still_invalidates_delivery(self):
        for change in ("digest", "assessment", "attempt"):
            with self.subTest(change=change):
                receipt = self.receipt()
                previous = copy.deepcopy(self.catalogue)
                record = self.catalogue["releases"]["image"]
                if change == "digest":
                    record["registryDigest"] = "sha256:" + "c" * 64
                elif change == "assessment":
                    record["latestAttempt"]["outcome"] = "clean"
                else:
                    record["latestAttempt"]["monitorRunAttempt"] = 2
                with self.assertRaises(evidence.EvidenceChanging):
                    self.assert_current(receipt)
                self.catalogue = previous

    def test_running_monitor_defers_then_completion_allows_delivery(self):
        attempt = self.catalogue["releases"]["image"]["latestAttempt"]
        attempt["outcome"] = "running"
        with self.assertRaises(evidence.EvidenceChanging):
            evidence.committed_monitor(self.client, self.catalogue, [PROFILE])
        attempt["outcome"] = "vulnerable"
        self.assert_current(self.receipt())

    def test_failed_monitor_requires_successful_state_delivery(self):
        self.run["conclusion"] = "failure"
        with patch.object(evidence, "state_delivery", return_value=False):
            with self.assertRaisesRegex(ValueError, "verified delivery"):
                self.receipt()
        with patch.object(evidence, "state_delivery", return_value=True):
            self.assert_current(self.receipt())

    def test_incomplete_or_foreign_producer_cannot_authorize(self):
        for field, value in (
            ("id", 456),
            ("run_attempt", 2),
            ("head_branch", "topic"),
            ("path", ".github/workflows/other.yml"),
            ("event", "pull_request"),
            ("status", "in_progress"),
        ):
            with self.subTest(field=field):
                previous = self.run[field]
                self.run[field] = value
                with self.assertRaises(ValueError):
                    self.receipt()
                self.run[field] = previous

    def test_unavailable_assessment_cannot_reuse_older_evidence(self):
        self.catalogue["releases"]["image"]["latestAttempt"]["outcome"] = "unavailable"
        with self.assertRaisesRegex(ValueError, "usable assessment"):
            self.receipt()

    def test_catalogue_acquisition_releases_lock_without_sleeping(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(
                evidence, "acquire", side_effect=evidence.EvidenceChanging("running")
            ) as acquire,
            patch.object(evidence.time, "sleep", side_effect=AssertionError("sleep under lock")),
        ):
            result = evidence.acquire_with_retry(
                self.client, ROOT, Path(directory) / "work", use_catalogue_monitor=True
            )
            self.assertIsNone(result)
            self.assertEqual(acquire.call_count, 1)

    def test_monitor_completion_can_request_only_evidence_based_security_delivery(self):
        with patch.dict("os.environ", {"GITHUB_EVENT_NAME": "workflow_run"}):
            self.assertIsNone(chrome.declared_requirement(None, ""))
            with self.assertRaises(ValueError):
                chrome.declared_requirement("package", "new API")

    def test_deferred_delivery_is_visible_without_claiming_a_security_fix(self):
        values = {"deferral.json": {"status": "deferred", "reason": "monitor running"}}
        rows = attention.pending(values, "chrome")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["fix"], {"versions": []})
        self.assertIn("retry automatically", rows[0]["reason"])

    def test_final_notification_takes_precedence_over_preparation(self):
        preparation = "chrome-update-preparation-123-1"
        final = "chrome-update-attention-123-1"
        self.assertEqual(
            attention.artifact_names("chrome", "123", "1", [preparation, final]), [final]
        )
        self.assertEqual(
            attention.artifact_names("chrome", "123", "1", [preparation]),
            [preparation, "chrome-update-delivery-123-1"],
        )

    def reconcile_notification(self, values):
        issue = {"number": 7, "state": "open", "user": {"login": "github-actions[bot]"}}
        for lane, signals in (
            ("chrome", {"finding:Chrome update pending"}),
            ("scan", {"finding:CVE-other"}),
            ("release", {"publish:replacement"}),
        ):
            issue.update(
                attention.image_issue.update(issue, PROFILE, lane, f"{lane} evidence", signals)
                or {}
            )
        others = {
            lane: state
            for lane, state in attention.image_issue.lanes(issue).items()
            if lane != "chrome"
        }

        def gh(endpoint, *args, body=None):
            if body is None:
                return json.dumps([[issue]])
            self.assertEqual(endpoint, "repos/example/repo/issues/7")
            issue.update(body)
            return "{}"

        with patch.object(attention, "gh", side_effect=gh):
            attention.notify(
                "chrome",
                {"name": "Chrome image updates", "head_sha": SOURCE},
                values,
                "example/repo",
                "123",
                "2",
            )
        current = attention.image_issue.lanes(issue)
        self.assertEqual({lane: current[lane] for lane in others}, others)
        self.assertEqual(issue["state"], "open")
        self.assertIn("scan evidence", issue["body"])
        self.assertIn("release evidence", issue["body"])
        return current["chrome"]

    def test_package_and_security_delivery_clear_only_chrome_pending_state(self):
        for kind in ("package", "security"):
            for status in ("created", "updated", "unchanged"):
                with self.subTest(kind=kind, status=status):
                    state = self.reconcile_notification(
                        {
                            "receipt.json": {"package": {"version": "158"}},
                            "delivery-requirement.json": {"kind": kind},
                            "publication.json": {
                                "status": status,
                                "url": "https://github.com/example/repo/pull/9",
                            },
                        }
                    )
                    self.assertFalse(state["active"])
                    self.assertEqual(state["signals"], [])

    def test_completed_no_security_fix_clears_only_chrome_pending_state(self):
        state = self.reconcile_notification(
            {
                "receipt.json": {"package": {"version": "158"}},
                "delivery-requirement.json": {"status": "no-security-fix"},
            }
        )
        self.assertFalse(state["active"])
        self.assertEqual(state["signals"], [])

    def test_unchanged_discovery_snapshot_clears_only_chrome_pending_state(self):
        with io.BytesIO() as raw:
            with zipfile.ZipFile(raw, "w") as bundle:
                bundle.writestr(
                    "receipt.json",
                    json.dumps({"before": "158", "package": {"version": "158"}}),
                )
                bundle.writestr(
                    "discovery.json",
                    json.dumps({"status": "unchanged", "before": "158", "after": "158"}),
                )
            raw.seek(0)
            values = attention.documents(raw)
        state = self.reconcile_notification(values)
        self.assertFalse(state["active"])
        self.assertEqual(state["signals"], [])

    def test_incomplete_or_contradictory_discovery_preserves_pending_state(self):
        for discovery in (
            {},
            {"status": "update", "before": "157", "after": "158"},
            {"status": "unchanged", "before": "157", "after": "158"},
            {"status": "unchanged", "before": "157", "after": "157"},
        ):
            with self.subTest(discovery=discovery):
                state = self.reconcile_notification(
                    {
                        "receipt.json": {"before": "158", "package": {"version": "158"}},
                        "discovery.json": discovery,
                    }
                )
                self.assertTrue(state["active"])

    def test_unavailable_or_deferred_authorization_preserves_chrome_pending_state(self):
        for values in (
            {},
            {"delivery-requirement.json": {"status": "no-security-fix"}},
            {
                "receipt.json": {"package": {"version": "158"}},
                "delivery-requirement.json": {"kind": "package"},
            },
            {
                "receipt.json": {"package": {"version": "158"}},
                "delivery-requirement.json": {"status": "no-security-fix"},
                "deferral.json": {"status": "deferred"},
            },
        ):
            with self.subTest(values=values):
                self.assertTrue(self.reconcile_notification(values)["active"])
