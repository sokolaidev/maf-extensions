"""Unmapped findings and rebuild-only fixes must reach maintainers without notification spam."""

from __future__ import annotations

import io
import json
import sys
import zipfile
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import report_remediation_attention as report  # noqa: E402


def finding(status="unsupported-component", component="python", **extra):
    return {
        "profile": "antivirus",
        "id": "CVE-test",
        "component": component,
        "installed": "3.14.10",
        "fix": {"versions": ["3.14.11"]},
        "status": status,
        **extra,
    }


def source_run(**extra):
    return {
        "name": "Platform image remediation",
        "head_branch": "main",
        "head_repository": {"full_name": "example/repo"},
        "event": "schedule",
        "path": ".github/workflows/platform-image-remediation.yml",
        "head_sha": "a" * 40,
        "run_started_at": "2026-11-21T15:00:00Z",
        **extra,
    }


def archive(**entries):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as output:
        for name, value in entries.items():
            output.writestr(name, json.dumps(value))
    return stream.getvalue()


@pytest.mark.parametrize(
    "status",
    [
        "unsupported-component",
        "unsupported-vendor",
        "no-compatible-fix",
        "unmapped-binary",
        "incompatible-source-policy",
    ],
)
def test_findings_are_reported_even_without_planned_edits(status):
    rows = report.pending({"plan.json": {"findings": [finding(status)], "updates": []}}, "platform")
    assert len(rows) == 1
    assert status in rows[0]["reason"]
    assert rows[0]["fix"]["versions"] == ["3.14.11"]


def test_missing_fix_is_not_reported_as_proof_no_upstream_fix_exists():
    row = report.pending(
        {"plan.json": {"findings": [finding("no-compatible-fix", fix={})]}}, "platform"
    )[0]
    assert "does not prove no upstream fix exists" in row["reason"]


def test_vendor_fixed_version_is_visible_when_scanner_has_none():
    row = finding("rebuild-required", fix={"versions": []}, vendorFix={"fixedVersion": "2.13.9-r3"})
    pending = report.pending({"plan.json": {"findings": [row]}}, "platform")
    assert pending[0]["fix"] == {"versions": ["2.13.9-r3"], "state": "fixed"}


@pytest.mark.parametrize("status", sorted(report.REBUILD))
def test_rebuild_is_a_release_action_even_after_a_partial_pr(status):
    values = {
        "plan.json": {"findings": [finding(status)]},
        "qualified.json": {"status": "release-needed"},
        "publication.json": {"status": "created", "url": "https://github.com/example/repo/pull/1"},
    }
    reason = report.pending(values, "platform")[0]["reason"]
    assert "Publish a replacement image" in reason
    assert "other findings may remain" in reason


def test_delivered_pr_does_not_hide_unrelated_or_partially_supported_findings():
    values = {
        "plan.json": {
            "findings": [
                finding("update", component="fixed"),
                finding(),
                finding(
                    component="partial",
                    decisions=[{"status": "update"}, {"status": "unmapped-binary"}],
                ),
            ]
        },
        "qualified.json": {"status": "ready"},
        "publication.json": {"status": "updated", "url": "https://github.com/example/repo/pull/1"},
    }
    rows = report.pending(values, "go")
    assert {row["component"] for row in rows} == {"python", "partial"}
    assert "unmapped-binary" in rows[1]["reason"]


@pytest.mark.parametrize("qualification", ["not-qualified", "unresolved", "ready", "deferred"])
def test_planned_update_without_delivery_notifies(qualification):
    values = {
        "plan.json": {"findings": [finding("update")]},
        "qualified.json": {"status": qualification},
    }
    assert qualification in report.pending(values, "platform")[0]["reason"]


@pytest.mark.parametrize(
    "kind,qualification", [("platform", "release-needed"), ("go", "source-already-fixed")]
)
@pytest.mark.parametrize("status", ["update", *sorted(report.REBUILD)])
def test_qualified_rebuild_reports_publication_and_preserves_unsupported(
    kind, qualification, status
):
    values = {
        "plan.json": {"findings": [finding(status), finding("unsupported-component")]},
        "qualified.json": {"status": qualification},
    }
    rows = report.pending(values, kind)
    assert "Publish a replacement image" in rows[0]["reason"]
    assert "other findings may remain" in rows[0]["reason"]
    assert "no qualified PR" not in rows[0]["reason"]
    assert "Automation cannot deliver" in rows[1]["reason"]


def test_chrome_advisory_changes_notify_again_with_unchanged_versions(monkeypatch):
    tracker = None
    comments = []
    writes = []

    def gh(endpoint, *args, body=None):
        nonlocal tracker
        if body:
            writes.append(body["body"])
            record = {"body": body["body"], "user": {"login": "github-actions[bot]"}}
            if tracker is None:
                tracker = {**record, "number": 7}
            else:
                comments.append(record)
            return "{}"
        return json.dumps([comments if "/comments?" in endpoint else [tracker] if tracker else []])

    monkeypatch.setattr(report, "gh", gh)
    values = {
        "delivery-requirement.json": {"kind": "security", "reason": "High finding CVE-first"},
        "receipt.json": {"before": "157", "package": {"version": "158"}},
    }
    report.notify("chrome", source_run(), values, "example/repo", "123", "1")
    report.notify("chrome", source_run(), values, "example/repo", "124", "1")
    assert len(writes) == 1
    values["delivery-requirement.json"]["reason"] = "High finding CVE-second"
    report.notify("chrome", source_run(), values, "example/repo", "125", "1")
    assert len(writes) == 2
    assert "CVE-first" in writes[0]
    assert "CVE-second" in writes[1]
    report.notify("chrome", source_run(), values, "example/repo", "126", "1")
    assert len(writes) == 2


def test_chrome_authorized_security_update_without_pr_notifies():
    values = {
        "delivery-requirement.json": {"kind": "security"},
        "receipt.json": {"before": "157", "package": {"version": "158"}},
    }
    assert report.pending(values, "chrome")[0]["fix"]["versions"] == ["158"]
    values["publication.json"] = {
        "status": "unchanged",
        "url": "https://github.com/example/repo/pull/1",
    }
    assert report.pending(values, "chrome") == []


@pytest.mark.parametrize(
    "values",
    [
        {},
        {"plan.json": {"findings": []}},
        {"delivery-requirement.json": {"status": "no-security-fix"}},
    ],
)
def test_no_findings_or_no_chrome_security_requirement_does_not_invent_an_issue(values):
    assert report.pending(values, "chrome") == []


def scan():
    match = {
        "vulnerability": {"id": "CVE-test", "severity": "High", "fix": {"versions": ["2"]}},
        "artifact": {"id": "pkg", "name": "openssl", "version": "1"},
    }
    return {
        "build.json": {"profile": "hyperlight-aks-probe", "run_id": "123", "run_attempt": "2"},
        "assessment.json": {
            "outcome": "vulnerable",
            "findings": [{"id": "CVE-test", "severity": "High"}],
        },
        "grype.json": {"matches": [match]},
    }


def test_scan_only_profile_notifies_and_preserves_non_applicability():
    values = scan()
    values["grype.json"]["matches"].append(
        {"vulnerability": {"id": "CVE-excluded", "severity": "High"}, "artifact": {"id": "other"}}
    )
    values["assessment.json"]["notAffected"] = [{"id": "CVE-excluded", "artifactId": "other"}]
    rows = report.scan_rows(values, "123", "2")
    assert len(rows) == 1 and rows[0]["profile"] == "hyperlight-aks-probe"


def test_stale_or_inconsistent_scan_refuses_reporting():
    with pytest.raises(ValueError, match="another attempt"):
        report.scan_rows(scan(), "123", "3")
    values = scan()
    values["assessment.json"]["findings"] = []
    with pytest.raises(ValueError, match="differ"):
        report.scan_rows(values, "123", "2")


def test_archive_reader_ignores_nested_evidence_and_traversal():
    raw = archive(
        **{
            "remediation/plan.json": {"findings": []},
            "remediation/attempts/1/plan.json": {"stale": True},
            "../publication.json": {"status": "created"},
            "results/candidate/qualification.json": {"status": "ready"},
        }
    )
    assert report.documents(raw) == {"plan.json": {"findings": []}}


def test_archive_reader_refuses_ambiguous_roots():
    with pytest.raises(ValueError, match="Duplicate"):
        report.documents(archive(**{"plan.json": {}, "remediation/plan.json": {}}))


def test_collection_filters_old_attempts_and_prefers_delivery_snapshot(monkeypatch):
    names = [
        "platform-remediation-plan",
        "platform-remediation-qualification-123-2",
        "platform-remediation-delivery-123-2",
    ]
    artifacts = [
        {
            "id": i,
            "name": name,
            "created_at": "2026-11-21T15:10:00Z",
            "expired": False,
            "size_in_bytes": 100,
        }
        for i, name in enumerate(names, 1)
    ]
    artifacts.append({**artifacts[0], "id": 9, "created_at": "2026-11-21T14:00:00Z"})
    monkeypatch.setattr(
        report,
        "gh",
        lambda endpoint, *args: (
            json.dumps([{"artifacts": artifacts}])
            if "/artifacts?" in endpoint
            else json.dumps(source_run())
        ),
    )
    downloads = []

    def download(args):
        downloads.append(args[-1])
        return archive(**{"publication.json": {"stage": args[-1]}})

    monkeypatch.setattr(report.subprocess, "check_output", download)
    _, values = report.collect("platform", "example/repo", "123", "2")
    assert len(downloads) == 3
    assert values["publication.json"]["stage"].endswith("/3/zip")
    assert not any("/9/" in path for path in downloads)


@pytest.mark.parametrize(
    "change",
    [
        {"event": "pull_request"},
        {"head_branch": "topic"},
        {"head_repository": {"full_name": "attacker/repo"}},
        {"path": ".github/workflows/other.yml"},
    ],
)
def test_collection_refuses_untrusted_workflow_before_reading_artifacts(monkeypatch, change):
    monkeypatch.setattr(report, "gh", lambda *args: json.dumps(source_run(**change)))
    with pytest.raises(ValueError, match="trusted main"):
        report.collect("platform", "example/repo", "123", "2")


@pytest.mark.parametrize("existing", [False, True])
def test_notifications_create_or_update_only_the_owned_profile_tracker(monkeypatch, existing):
    marker = "<!-- image-remediation-attention:antivirus -->"
    tracker = {"number": 7, "body": marker, "user": {"login": "github-actions[bot]"}}
    issues = [
        [
            {**tracker, "number": 1, "pull_request": {}},
            {**tracker, "number": 2, "user": {"login": "human"}},
        ],
        [tracker] if existing else [],
    ]
    calls = []

    def gh(endpoint, *args, body=None):
        calls.append((endpoint, body))
        return json.dumps(issues) if "?state=open" in endpoint else "{}"

    monkeypatch.setattr(report, "gh", gh)
    monkeypatch.setattr(report, "unchanged", lambda *args: False)
    values = {"plan.json": {"findings": [finding()]}, "deferral.json": {"reason": "main advanced"}}
    report.notify("platform", source_run(), values, "example/repo", "123", "2")
    endpoint, body = calls[-1]
    assert endpoint == (
        "repos/example/repo/issues/7/comments" if existing else "repos/example/repo/issues"
    )
    assert "3.14.11" in body["body"] and "unsupported-component" in body["body"]
    assert "/runs/123/attempts/2" in body["body"] and "main advanced" in body["body"]
    assert "does not publish images" in body["body"]


def test_identical_findings_across_runs_do_not_comment_again(monkeypatch):
    tracker = {
        "number": 7,
        "body": "<!-- image-remediation-attention:antivirus -->",
        "user": {"login": "github-actions[bot]"},
    }
    monkeypatch.setattr(report, "gh", lambda *args, **kwargs: json.dumps([[tracker]]))
    markers = []
    monkeypatch.setattr(report, "unchanged", lambda *args: markers.append(args[-2]) or True)
    values = {"plan.json": {"findings": [finding()]}}
    report.notify("platform", source_run(), values, "example/repo", "123", "1")
    values["plan.json"]["findings"][0]["fix"]["available"] = [
        {"date": "2026-11-22", "kind": "first-observed", "version": "3.14.11"}
    ]
    report.notify("platform", source_run(), values, "example/repo", "124", "1")
    assert markers[0] == markers[1]
    values["plan.json"]["findings"][0]["status"] = "no-compatible-fix"
    report.notify("platform", source_run(), values, "example/repo", "125", "1")
    assert markers[2] != markers[1]


@pytest.mark.parametrize("kind,filename", report.WORKFLOWS.items())
def test_attention_runs_after_all_evidence_jobs_even_when_they_succeed(kind, filename):
    workflow = yaml.safe_load((ROOT / ".github/workflows" / filename).read_text(encoding="utf-8"))
    job = workflow["jobs"]["report-attention"]
    assert (
        "always()" in job["if"] and "pull_request" in job["if"] and "refs/heads/main" in job["if"]
    )
    assert "failure" not in job["if"]
    assert job["permissions"] == {"contents": "read", "actions": "read", "issues": "write"}
    assert job["concurrency"] == {
        "group": "image-remediation-attention",
        "queue": "max",
        "cancel-in-progress": False,
    }
    assert job["steps"][-1]["run"].endswith(f"--kind {kind}")
    assert (
        "publish" if kind in {"platform", "go"} else "check" if kind == "scan" else "update"
    ) in job["needs"]


def test_evidence_cannot_inject_mentions_or_table_markup():
    assert report.cell("@maintainer\n| <script>`") == "&#64;maintainer &#124; &lt;script&gt;&#96;"
    assert len(report.cell("<" * 400)) <= 240


def test_shared_issue_deduplicates_each_workflow_independently(monkeypatch):
    lane = "<!-- image-remediation-workflow:platform -->"
    state = report.notification_marker("same findings")
    tracker = {"number": 7, "body": lane + state}
    comments = [
        [
            {
                "body": "<!-- image-remediation-workflow:scan -->different findings",
                "user": {"login": "github-actions[bot]"},
            },
            {"body": lane + "human text", "user": {"login": "maintainer"}},
        ]
    ]
    monkeypatch.setattr(report, "gh", lambda *args: json.dumps(comments))
    assert report.unchanged(tracker, "repos/example/repo/issues", state, lane)
    comments.append(
        [
            {
                "body": lane + report.notification_marker("changed findings"),
                "user": {"login": "github-actions[bot]"},
            }
        ]
    )
    assert not report.unchanged(tracker, "repos/example/repo/issues", state, lane)


def test_large_plans_fit_in_an_issue_and_point_to_complete_artifacts(monkeypatch):
    writes = []

    def gh(*args, body=None):
        if body:
            writes.append(body)
        return "[]"

    monkeypatch.setattr(report, "gh", gh)
    values = {
        "plan.json": {"findings": [finding(component=str(i) + "<" * 400) for i in range(100)]}
    }
    report.notify("platform", source_run(), values, "example/repo", "123", "1")
    assert len(writes) == 1
    assert len(writes[0]["body"]) < 65000
    assert "Showing 40 of 100" in writes[0]["body"]
