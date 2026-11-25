"""Exercise issue migration, recovery and alert delivery across reporter boundaries."""

from __future__ import annotations

import copy
import io
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import check_container_monitor_result as monitor_gate  # noqa: E402
import chrome_update_security as chrome_security  # noqa: E402
import image_release_needed as release  # noqa: E402
import image_security_issue as shared  # noqa: E402
import plan_go_remediation as go  # noqa: E402
import plan_platform_remediation as platform  # noqa: E402
import report_remediation_attention as attention  # noqa: E402
import report_workflow_failure as workflow  # noqa: E402

BOT = {"login": "github-actions[bot]"}
RUN = {"name": "Image security", "head_sha": "a" * 40}
FINDING = {"id": "CVE-test", "severity": "High", "fix": {"state": "unknown", "versions": []}}


def observe(issue, lane, signals, *, active=True, related=None, content="Evidence"):
    payload = shared.update(
        issue, "antivirus", lane, content, signals, active=active, related=related
    )
    if issue is None:
        assert payload is not None
        return {"number": 1, "state": "open", "user": BOT, **payload}
    return issue | (payload or {})


def scan(run_id, *, clean=False):
    return {
        "antivirus": {
            "build.json": {"profile": "antivirus", "run_id": run_id, "run_attempt": "1"},
            "assessment.json": {
                "assessedAt": (
                    datetime(2026, 1, 1, tzinfo=UTC) + timedelta(seconds=int(run_id))
                ).isoformat(),
                "outcome": "clean" if clean else "vulnerable",
                "findings": [] if clean else [FINDING],
            },
            "grype.json": {
                "matches": []
                if clean
                else [
                    {
                        "vulnerability": FINDING,
                        "artifact": {"id": "pkg", "name": "python", "version": "3.14.10"},
                    }
                ],
            },
        }
    }


class IssueAPI:
    def __init__(self):
        self.items = []
        self.comments = []
        self.writes = []

    def pages(self, endpoint):
        return copy.deepcopy(self.comments if "/comments" in endpoint else self.items)

    def request(self, endpoint, *, method, payload):
        value = json.loads(payload)
        self.writes.append((endpoint, method, value))
        if endpoint.endswith("/comments"):
            self.comments.append({**value, "user": BOT})
        elif method == "POST":
            self.items.append(
                {"number": len(self.items) + 1, "state": "open", "user": BOT, **value}
            )
        else:
            number = int(endpoint.rsplit("/", 1)[1])
            next(item for item in self.items if item["number"] == number).update(value)
        return b"{}"

    def gh(self, endpoint, *args, body=None):
        if body is None:
            return json.dumps([self.pages(endpoint)])
        method = "PATCH" if "PATCH" in args else "POST"
        return self.request(endpoint, method=method, payload=json.dumps(body)).decode()


class TestImageIssueReporting(unittest.TestCase):
    def test_equal_scan_retries_only_retirement_already_linked_to_canonical_issue(self):
        api = IssueAPI()
        canonical = observe(None, "scan", {"finding:CVE-test"})
        legacy = {
            "number": 9,
            "state": "open",
            "user": BOT,
            "body": "<!-- image-remediation-attention:antivirus -->",
        }
        api.items = [canonical, legacy]

        def interrupted(endpoint, *args, body=None):
            if endpoint.endswith("/9"):
                raise OSError("Retirement write failed")
            return api.gh(endpoint, *args, body=body)

        with patch.object(attention, "gh", interrupted), self.assertRaises(OSError):
            attention.notify("scan", RUN, scan("2"), "example/repo", "2", "1")
        before = copy.deepcopy(api.items[0])
        api.items.append(legacy | {"number": 10})
        with patch.object(attention, "gh", api.gh):
            attention.notify("scan", RUN, scan("2"), "example/repo", "2", "1")
        self.assertEqual(api.items[0], before)
        self.assertEqual(api.items[1]["state"], "closed")
        self.assertEqual(api.items[2]["state"], "open")
        self.assertEqual(api.comments, [])

    def test_two_dismissed_legacy_reports_retain_the_latest_release_baseline(self):
        state = {
            "resolved": False,
            "reasons": {"vulnerabilities": {"findings": [FINDING, FINDING | {"id": "CVE-other"}]}},
        }
        legacy = {
            "number": 9,
            "state": "closed",
            "user": BOT,
            "body": "<!-- image-release-needed:antivirus -->\n<!-- image-release-state\n"
            + json.dumps(state)
            + "\n-->",
        }
        remediation = legacy | {
            "number": 10,
            "body": "<!-- image-remediation-attention:antivirus -->",
        }
        selected = shared.select([legacy, remediation], "antivirus")
        self.assertEqual(selected, remediation)
        adopted = observe(selected, "platform", {"finding:CVE-test"}, related=[legacy])
        self.assertEqual(adopted["state"], "closed")
        self.assertIn("<!-- image-release-state", adopted["body"])
        refreshed = observe(adopted, "release", shared.release_signals(state))
        self.assertEqual(refreshed["state"], "closed")
        self.assertIn("finding:CVE-other", shared.lanes(refreshed)["release"]["signals"])

    def test_release_reconciliation_keeps_prose_edits_and_removal_quiet(self):
        packages = [
            {"package": "one", "minimumVersion": "1.0.0", "reason": "Original reason"},
            {"package": "two", "minimumVersion": "2.0.0", "reason": "Another reason"},
        ]
        state = {
            "resolved": False,
            "currentVersion": "0.1.0",
            "currentDigest": "sha256:" + "a" * 64,
            "releaseEvidence": "release",
            "status": "Package minimum not satisfied",
            "reasons": {"requirements": {"packages": packages}},
        }
        api = IssueAPI()
        release.reconcile(api, "antivirus", state, None)
        api.items[0]["state"] = "closed"
        packages[0]["reason"] = "Clarified reason"
        packages.pop()
        release.reconcile(api, "antivirus", state, api.items[0])
        self.assertEqual(api.items[0]["state"], "closed")
        self.assertIn("Clarified reason", api.items[0]["body"])
        self.assertEqual(api.comments, [])

    def test_stale_scan_cannot_replace_newer_findings_or_recovery(self):
        for current_clean in (False, True):
            with self.subTest(current_clean=current_clean):
                api = IssueAPI()
                with patch.object(attention, "gh", api.gh):
                    attention.notify("scan", RUN, scan("1"), "example/repo", "1", "1")
                    current = scan("3", clean=current_clean)
                    attention.notify("scan", RUN, current, "example/repo", "3", "1")
                    before = copy.deepcopy(api.items)
                    writes = len(api.writes)
                    stale = scan("2", clean=not current_clean)
                    attention.notify("scan", RUN, stale, "example/repo", "2", "1")
                    attention.notify("scan", RUN, current, "example/repo", "3", "1")
                self.assertEqual(api.items, before)
                self.assertEqual(len(api.writes), writes)

    def test_release_requirements_ignore_prose_and_removal_but_detect_new_constraints(self):
        packages = [
            {"package": "one", "minimumVersion": "1.0.0", "reason": "Original reason"},
            {"package": "two", "minimumVersion": "2.0.0", "reason": "Another reason"},
        ]
        state = {"reasons": {"requirements": {"packages": packages}}}
        known = shared.release_signals(state)
        tracker = observe(None, "release", known) | {"state": "closed"}
        packages[0]["reason"] = "Clarified reason"
        packages.pop()
        signals = shared.release_signals(state)
        self.assertLess(signals, known)
        self.assertEqual(observe(tracker, "release", signals)["state"], "closed")
        self.assertIsNone(shared.notice(tracker | {"state": "open"}, signals))
        packages[0]["minimumVersion"] = "1.1.0"
        self.assertEqual(
            observe(tracker, "release", shared.release_signals(state))["state"], "open"
        )

    def test_legacy_release_dismissal_survives_partial_remediation_adoption(self):
        state = {
            "resolved": False,
            "reasons": {"vulnerabilities": {"findings": [FINDING, FINDING | {"id": "CVE-other"}]}},
        }
        legacy = {
            "number": 1,
            "state": "closed",
            "body": "<!-- image-release-needed:antivirus -->\n<!-- image-release-state\n"
            + json.dumps(state)
            + "\n-->",
        }
        adopted = observe(legacy, "platform", {"finding:CVE-test"})
        self.assertEqual(adopted["state"], "closed")
        refreshed = observe(adopted, "release", shared.release_signals(state))
        self.assertEqual(refreshed["state"], "closed")
        self.assertIn("finding:CVE-other", shared.lanes(refreshed)["release"]["signals"])
        state["reasons"]["vulnerabilities"]["findings"].append(FINDING | {"id": "CVE-new"})
        self.assertEqual(
            observe(refreshed, "release", shared.release_signals(state))["state"], "open"
        )

    def test_chrome_advisory_signals_ignore_image_identity_and_installed_version(self):
        expected = {
            "evidence": {"version": "1.0.0", "registryDigest": "sha256:" + "a" * 64},
            "after": "158.0.0.1-1",
            "findings": [{"id": "CVE-2026-12345", "severity": "High", "fixedVersion": "158.0.0.1"}],
        }
        values = {
            "receipt.json": {"before": "157.0.0.1-1", "package": {"version": "158.0.0.1-1"}},
            "delivery-requirement.json": chrome_security.requirement(expected),
        }
        api = IssueAPI()
        with patch.object(attention, "gh", api.gh):
            attention.notify("chrome", RUN, values, "example/repo", "1", "1")
            api.items[0]["state"] = "closed"
            expected["evidence"] = {"version": "1.0.1", "registryDigest": "sha256:" + "b" * 64}
            values["receipt.json"]["before"] = "157.0.0.2-1"
            values["delivery-requirement.json"] = chrome_security.requirement(expected)
            attention.notify("chrome", RUN, values, "example/repo", "2", "1")
            self.assertEqual(api.items[0]["state"], "closed")
            self.assertEqual(api.comments, [])
            expected["findings"][0]["severity"] = "Critical"
            values["delivery-requirement.json"] = chrome_security.requirement(expected)
            attention.notify("chrome", RUN, values, "example/repo", "3", "1")
        self.assertEqual(api.items[0]["state"], "open")

    def test_existing_canonical_state_imports_open_legacy_activity(self):
        legacy = {
            "number": 9,
            "state": "open",
            "body": "<!-- image-remediation-attention:antivirus -->",
        }
        for state in ("open", "closed"):
            with self.subTest(state=state):
                canonical = observe(None, "release", {"finding:CVE-test"}) | {"state": state}
                updated = observe(canonical, "release", set(), active=False, related=[legacy])
                self.assertEqual(updated["state"], "open")
                self.assertTrue(shared.lanes(updated)["legacy-remediation"]["active"])
                self.assertFalse(shared.lanes(updated)["release"]["active"])

    def test_receipts_distinguish_recurrence_from_retry_and_evidence_refresh(self):
        first = observe(None, "scan", {"finding:A"})
        message = shared.notice(first, {"finding:A", "finding:B"})
        self.assertIsNotNone(message)
        comments = [{"user": BOT, "body": message}]
        refreshed = observe(first, "scan", {"finding:A"}, content="New evidence URL")
        self.assertEqual(shared.notice(refreshed, {"finding:A", "finding:B"}), message)
        again = observe(observe(first, "scan", {"finding:A", "finding:B"}), "scan", {"finding:A"})
        recurrence = shared.notice(again, {"finding:A", "finding:B"})
        assert message is not None and recurrence is not None
        self.assertTrue(shared.already_notified(comments, message))
        self.assertNotEqual(recurrence, message)
        self.assertFalse(shared.already_notified(comments, recurrence))

    def test_release_refreshes_links_without_a_notification(self):
        api = IssueAPI()
        state = {
            "resolved": False,
            "currentVersion": "0.1.0",
            "currentDigest": "sha256:" + "a" * 64,
            "releaseEvidence": "release",
            "candidateEvidence": "candidate-old",
            "candidateFindings": [FINDING],
            "status": "Blocked on candidate remediation",
            "reasons": {"vulnerabilities": {"findings": [FINDING], "evidence": "monitor-old"}},
        }
        release.reconcile(api, "antivirus", state, None)
        refreshed = copy.deepcopy(state)
        refreshed["candidateEvidence"] = "candidate-new"
        refreshed["reasons"]["vulnerabilities"]["evidence"] = "monitor-new"
        release.reconcile(api, "antivirus", refreshed, api.items[0])
        self.assertIn("candidate-new", api.items[0]["body"])
        self.assertIn("monitor-new", api.items[0]["body"])
        self.assertEqual(api.comments, [])
        self.assertEqual(len(api.writes), 2)
        release.reconcile(api, "antivirus", refreshed, api.items[0])
        self.assertEqual(len(api.writes), 2)

    def test_planners_and_pending_rows_preserve_critical_escalation(self):
        identity = {"profile": "antivirus", "imageId": "sha256:" + "a" * 64}
        for planner in (platform, go):
            with self.subTest(planner=planner.__name__):
                high = observe(None, "platform", {"finding:CVE-test"}) | {"state": "closed"}
                match = {
                    "vulnerability": FINDING | {"severity": "Critical"},
                    "artifact": {
                        "type": "python",
                        "name": "python",
                        "version": "3.14.10",
                        "locations": [],
                    },
                }
                inputs = {} if planner is platform else ({}, {})
                with (
                    patch.object(planner, "inputs", return_value=inputs),
                    patch.object(planner, "evidence", return_value=(identity, [match], {})),
                ):
                    plan = planner.plan(ROOT, [Path("retained")])
                self.assertEqual(plan["findings"][0]["severity"], "Critical")
                rows = attention.pending(
                    {"plan.json": plan}, "platform" if planner is platform else "go"
                )
                self.assertEqual(rows[0]["severity"], "Critical")
                updated = observe(high, "platform", shared.findings_signals(rows))
                self.assertEqual(updated["state"], "open")

    def test_clean_scan_clears_only_its_section_and_recurrence_reopens(self):
        api = IssueAPI()
        with patch.object(attention, "gh", api.gh):
            attention.notify("scan", RUN, scan("1"), "example/repo", "1", "1")
            attention.notify("scan", RUN, scan("2", clean=True), "example/repo", "2", "1")
            self.assertFalse(shared.lanes(api.items[0])["scan"]["active"])
            self.assertEqual(shared.lanes(api.items[0])["scan"]["signals"], [])
            self.assertEqual(api.items[0]["state"], "open")
            api.items[0]["state"] = "closed"
            attention.notify("scan", RUN, scan("3"), "example/repo", "3", "1")
        self.assertEqual(len(api.items), 1)
        self.assertEqual(api.items[0]["state"], "open")

    def test_missing_scan_does_not_clear_findings_or_other_reporters(self):
        api = IssueAPI()
        api.items.append(
            observe(observe(None, "platform", {"finding:other"}), "scan", {"finding:CVE-test"})
        )
        before = copy.deepcopy(api.items)
        with patch.object(attention, "gh", api.gh):
            attention.notify("scan", RUN, {}, "example/repo", "2", "1")
            self.assertEqual(api.items, before)
            attention.notify("scan", RUN, scan("2", clean=True), "example/repo", "2", "1")
        self.assertTrue(shared.lanes(api.items[0])["platform"]["active"])
        self.assertFalse(shared.lanes(api.items[0])["scan"]["active"])

    def test_clean_scan_requires_current_consistent_evidence_and_no_new_issue(self):
        for damage in ("attempt", "raw-findings", "missing-inventory"):
            with self.subTest(damage=damage):
                values = scan("2", clean=True)["antivirus"]
                if damage == "attempt":
                    values["build.json"]["run_attempt"] = "0"
                elif damage == "raw-findings":
                    values["grype.json"] = scan("2")["antivirus"]["grype.json"]
                else:
                    del values["grype.json"]
                with self.assertRaises((ValueError, KeyError)):
                    attention.scan_rows(values, "2", "1")
        api = IssueAPI()
        with patch.object(attention, "gh", api.gh):
            attention.notify("scan", RUN, scan("2", clean=True), "example/repo", "2", "1")
        self.assertEqual(api.items, [])

    def test_delivered_source_proposal_clears_only_its_remediation_section(self):
        api = IssueAPI()
        api.items.append(
            observe(
                observe(None, "release", {"finding:CVE-test"}), "platform", {"finding:CVE-test"}
            )
        )
        values = {
            "plan.json": {
                "schemaVersion": 1,
                "evidence": [{"profile": "antivirus"}],
                "findings": [
                    {
                        "profile": "antivirus",
                        "id": "CVE-test",
                        "component": "python",
                        "installed": "1",
                        "severity": "High",
                        "status": "update",
                    }
                ],
            },
            "qualified.json": {"status": "ready"},
            "publication.json": {
                "status": "created",
                "url": "https://github.com/example/repo/pull/1",
            },
        }
        with patch.object(attention, "gh", api.gh):
            attention.notify("platform", RUN, values, "example/repo", "2", "1")
        self.assertTrue(shared.lanes(api.items[0])["release"]["active"])
        self.assertFalse(shared.lanes(api.items[0])["platform"]["active"])
        self.assertEqual(api.items[0]["state"], "open")


def jobs():
    return [
        {
            "jobs": [
                {"name": "Select image security profiles", "conclusion": "success"},
                {"name": "Prepare shared Python APKs", "conclusion": "skipped"},
                {"name": "Report findings requiring maintainer action", "conclusion": "success"},
                {
                    "name": "Image security (antivirus)",
                    "conclusion": "failure",
                    "steps": [
                        {
                            "name": "Enforce image vulnerability applicability",
                            "conclusion": "failure",
                        },
                    ],
                },
                {
                    "name": "Image security checks",
                    "conclusion": "failure",
                    "steps": [
                        {"name": "Set up job", "conclusion": "success"},
                        {
                            "name": "Require successful selection and selected scans",
                            "conclusion": "failure",
                        },
                    ],
                },
            ]
        }
    ]


class TestWorkflowAlertDelivery(unittest.TestCase):
    def test_monitor_suppression_requires_gate_proof_and_no_other_step_failure(self):
        gate = "Require healthy current releases and complete monitoring delivery"
        health = {
            "attention": True,
            "operationalAttention": False,
            "summary": "Findings",
            "problems": "CVE",
        }
        environment = {
            "MONITOR_NOTIFICATION": json.dumps(health),
            "MONITOR_JOB_RESULTS": json.dumps(
                {
                    "begin": "success",
                    "scan": "success",
                    "finish": "success",
                    "result": "failure",
                }
            ),
            "MONITOR_VULNERABILITY_ONLY": "true",
        }
        for damage in (None, "setup", "download", "missing-proof", "jobs-api"):
            with self.subTest(damage=damage):
                job = {
                    "name": "result",
                    "conclusion": "failure",
                    "steps": [{"name": gate, "conclusion": "failure"}],
                }
                if damage in {"setup", "download"}:
                    job["steps"].append({"name": damage, "conclusion": "failure"})
                with patch.dict(workflow.os.environ, environment):
                    if damage == "missing-proof":
                        workflow.os.environ["MONITOR_VULNERABILITY_ONLY"] = "false"
                    with patch.object(
                        workflow,
                        "gh",
                        return_value=json.dumps([{"jobs": [job]}]),
                        side_effect=OSError("Unavailable") if damage == "jobs-api" else None,
                    ):
                        self.assertEqual(workflow.monitor_findings_only(), damage is None)

    def test_monitor_gate_proof_requires_readable_vulnerability_only_diagnostics(self):
        health = {
            "attention": True,
            "operationalAttention": False,
            "summary": "Findings",
            "problems": "CVE",
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for damage in (None, "missing", "malformed", "operational", "job-failed"):
                with self.subTest(damage=damage):
                    diagnostics, output = root / "diagnostics.json", root / "output"
                    diagnostics.write_text(
                        "null"
                        if damage == "malformed"
                        else json.dumps(health | {"operationalAttention": damage == "operational"}),
                        encoding="utf-8",
                    )
                    output.write_text("", encoding="utf-8")
                    with (
                        patch.dict(
                            monitor_gate.os.environ,
                            {
                                "BEGIN": "failure" if damage == "job-failed" else "success",
                                "SCAN": "success",
                                "FINISH": "success",
                                "ATTENTION": "true",
                                "MONITOR_DIAGNOSTICS": str(
                                    root / "missing" if damage == "missing" else diagnostics
                                ),
                                "GITHUB_OUTPUT": str(output),
                                "GITHUB_STEP_SUMMARY": "",
                            },
                        ),
                        redirect_stdout(io.StringIO()),
                    ):
                        self.assertEqual(monitor_gate.main(), 1)
                    self.assertEqual(
                        output.read_text("utf-8"),
                        f"vulnerability_only={str(damage is None).lower()}\n",
                    )

    def setUp(self):
        environment = patch.dict(
            workflow.os.environ,
            {
                "GITHUB_REPOSITORY": "example/repo",
                "GITHUB_SERVER_URL": "https://github.com",
                "GITHUB_RUN_ID": "123",
                "GITHUB_RUN_ATTEMPT": "1",
                "GITHUB_SHA": "a" * 40,
            },
        )
        environment.start()
        self.addCleanup(environment.stop)

    def test_aggregate_job_must_have_failed_only_at_its_expected_gate(self):
        for damage in (None, "missing", "setup", "cancelled", "no-steps"):
            with self.subTest(damage=damage):
                pages = jobs()
                check = pages[0]["jobs"][-1]
                if damage == "missing":
                    pages[0]["jobs"].pop()
                elif damage == "setup":
                    check["steps"][0]["conclusion"] = "failure"
                elif damage == "cancelled":
                    check["steps"][-1]["conclusion"] = "cancelled"
                elif damage == "no-steps":
                    check["steps"] = []
                with patch.object(workflow, "gh", return_value=json.dumps(pages)):
                    self.assertEqual(workflow.scan_findings_only(), damage is None)

    def test_unavailable_or_malformed_jobs_still_deliver_the_workflow_alert(self):
        for response in (
            "{",
            "null",
            "[]",
            '[{"jobs": null}]',
            '[{"jobs": [null]}]',
            subprocess.CalledProcessError(1, ["gh", "api"]),
        ):
            with self.subTest(response=response):
                api = IssueAPI()

                def request(endpoint, *args, body=None):
                    if "/jobs?" in endpoint:
                        if isinstance(response, Exception):
                            raise response
                        return response
                    return api.gh(endpoint, *args, body=body)

                with patch.object(workflow, "gh", request):
                    workflow.main(
                        [
                            "--image-security",
                            "--marker",
                            "<!-- image-security-failure-tracker -->",
                            "--title",
                            "Image workflow failed",
                            "--reproduce",
                            "Inspect retained logs",
                            "--expected",
                            "Build and scan evidence is available",
                            "--packages",
                            "maf-sandbox",
                        ]
                    )
                self.assertEqual(len(api.items), 1)
                self.assertIn("/runs/123/attempts/1", api.items[0]["body"])

    def test_incomplete_monitor_health_cannot_suppress_an_alert(self):
        with patch.dict(
            workflow.os.environ,
            {
                "MONITOR_NOTIFICATION": json.dumps(
                    {"attention": True, "operationalAttention": False}
                ),
                "MONITOR_JOB_RESULTS": json.dumps(
                    {
                        "begin": "success",
                        "scan": "success",
                        "finish": "success",
                        "result": "failure",
                    }
                ),
            },
        ):
            self.assertFalse(workflow.monitor_findings_only())


if __name__ == "__main__":
    unittest.main()
