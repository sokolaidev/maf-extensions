"""One image tracker preserves dismissal, evidence and independent recovery boundaries."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import image_security_issue as issues  # noqa: E402


def tracker(body="", **extra):
    return {
        "number": 7,
        "body": body,
        "state": "open",
        "user": {"login": "github-actions[bot]"},
        **extra,
    }


def observe(issue, signals, *, lane="scan", active=True, content="Evidence"):
    payload = issues.update(issue, "antivirus", lane, content, set(signals), active=active)
    return tracker(**payload) if issue is None else issue | (payload or {})


@pytest.mark.parametrize("reason", ["not_planned", "completed"])
def test_dismissed_issue_is_not_replaced_or_reopened_for_fewer_findings(reason):
    issue = observe(None, {"finding:CVE-python", "finding:CVE-sqlite"})
    issue.update(state="closed", state_reason=reason)
    updated = observe(issue, {"finding:CVE-python"}, content="New run; SQLite excluded")
    assert updated["number"] == 7 and updated["state"] == "closed"
    assert "SQLite excluded" in updated["body"]
    assert issues.notice(issue, {"finding:CVE-python"}) is None


@pytest.mark.parametrize(
    "added", ["finding:CVE-new", "fix:CVE-python:3.14.11", "publish:CVE-python"]
)
def test_new_finding_fix_or_publication_opportunity_reopens_same_issue(added):
    issue = observe(None, {"finding:CVE-python"})
    issue.update(state="closed", state_reason="not_planned")
    updated = observe(issue, {"finding:CVE-python", added})
    assert updated["number"] == 7 and updated["state"] == "open"


def test_release_resolution_does_not_close_unresolved_remediation():
    issue = observe(None, {"finding:CVE-python"}, lane="platform")
    issue = observe(issue, {"finding:CVE-python"}, lane="release")
    updated = observe(issue, set(), lane="release", active=False, content="Replacement verified")
    assert updated["state"] == "open"
    assert issues.lanes(updated)["platform"]["active"]
    assert not issues.lanes(updated)["release"]["active"]


def test_release_only_issue_can_close_and_reopen_after_verified_recovery():
    issue = observe(None, {"finding:CVE-python"}, lane="release")
    updated = observe(issue, set(), lane="release", active=False)
    assert updated["state"] == "closed" and updated["state_reason"] == "completed"
    updated = observe(updated, {"finding:CVE-python"}, lane="release")
    assert updated["number"] == 7 and updated["state"] == "open"


def test_legacy_dismissal_is_preserved_on_migration():
    issue = tracker(
        "<!-- image-remediation-attention:antivirus -->\nOld findings",
        state="closed",
        state_reason="not_planned",
    )
    updated = observe(issue, {"finding:CVE-python"}, content="Current findings")
    assert updated["state"] == "closed"
    assert "Old findings" in updated["body"]


def test_migration_retains_notes_and_links_before_retiring_duplicates():
    original = tracker("<!-- image-remediation-attention:antivirus -->\nUpstream blocker notes")
    duplicate = tracker(
        "<!-- image-release-needed:antivirus -->\nSigned release evidence", number=9
    )
    assert issues.select([duplicate, original], "antivirus") == original
    payload = issues.update(
        original,
        "antivirus",
        "release",
        "Release state",
        set(),
        active=False,
        related=[duplicate, original],
    )
    assert payload is not None and "state" not in payload
    assert "Upstream blocker notes" in payload["body"] and "Earlier report: #9" in payload["body"]
    retired = issues.retirement(duplicate, original, "antivirus")
    assert retired is not None and retired["state"] == "closed"
    assert "Signed release evidence" in retired["body"] and "#7" in retired["body"]
    assert "does not establish security recovery" in retired["body"]


def test_canonical_identity_wins_over_newer_legacy_tracker():
    canonical = observe(None, {"finding:CVE-python"})
    canonical.update(state="closed", state_reason="not_planned")
    legacy = tracker("<!-- image-release-needed:antivirus -->", number=99)
    assert issues.select([legacy, canonical], "antivirus") == canonical


def test_human_issues_pull_requests_and_other_profiles_are_not_adopted():
    body = issues.marker("antivirus")
    candidates = [
        tracker(body, user={"login": "maintainer"}),
        tracker(body, pull_request={}),
        tracker(issues.marker("hyperlight-aks-probe")),
    ]
    assert issues.select(candidates, "antivirus") is None


def test_cross_workflow_identical_findings_do_not_notify_again():
    issue = observe(None, {"finding:CVE-python", "fix:CVE-python:3.14.11"}, lane="platform")
    assert issues.notice(issue, {"finding:CVE-python"}) is None
    updated = observe(issue, {"finding:CVE-python"}, lane="scan")
    assert set(issues.lanes(updated)) == {"platform", "scan"}
    assert issues.notice(updated, {"finding:CVE-python", "fix:CVE-python:3.14.11"}) is None


def test_notification_retry_recognizes_bot_receipt_but_not_human_copy():
    issue = observe(None, {"finding:CVE-python"})
    notice = issues.notice(issue, {"finding:CVE-python", "fix:CVE-python:3.14.11"})
    assert notice is not None
    assert not issues.already_notified([{"body": notice, "user": {"login": "maintainer"}}], notice)
    assert issues.already_notified(
        [{"body": notice, "user": {"login": "github-actions[bot]"}}], notice
    )


def test_installed_version_and_scanner_timestamp_are_not_new_alerts():
    before = {"id": "CVE-one", "installed": "3.14.10", "fix": {"versions": ["3.14.11"]}}
    after = before | {
        "installed": "3.14.10-r1",
        "fix": {"versions": ["3.14.11"], "available": [{"date": "new"}]},
    }
    assert issues.findings_signals([before]) == issues.findings_signals([after])


def test_shared_body_refresh_is_idempotent_and_preserves_other_lanes():
    issue = observe(None, {"finding:CVE-python"}, lane="platform")
    issue = observe(issue, {"finding:CVE-python"}, lane="release")
    updated = observe(issue, {"finding:CVE-python"}, lane="release")
    assert updated == issue
    assert updated["body"].count("<!-- image-issue-section:platform -->") == 1
    assert updated["body"].count("<!-- image-issue-section:release -->") == 1


def test_remediation_adopts_release_tracker_before_release_writer_refreshes_it():
    legacy = tracker(
        "<!-- image-release-needed:antivirus -->\n<!-- image-release-state\n{}\n-->\nOld release"
    )
    issue = observe(legacy, {"finding:CVE-python"}, lane="platform", content="Python blocker")
    issue = observe(
        issue, {"finding:CVE-python"}, lane="release", content="Refreshed release evidence"
    )
    assert "Python blocker" in issue["body"]
    assert "Refreshed release evidence" in issue["body"]
    assert "Old release" not in issue["body"]
