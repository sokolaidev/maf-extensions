"""Approval presentation preserves evidence identity, missing-data visibility and protected gates."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import container_release_approval as approval  # noqa: E402
from container_release import digest, profiles, write  # noqa: E402

pytestmark = pytest.mark.workflow


@pytest.fixture
def prepared(tmp_path):
    candidate = {
        "profile": "bicep-avm",
        "version": "0.1.0",
        "sourceCommit": "a" * 40,
        "sourceRef": "refs/heads/main",
        "attemptId": "123",
        "registryDigest": "sha256:" + "b" * 64,
        "assessedManifestDigest": "sha256:" + "b" * 64,
        "imageId": "sha256:" + "c" * 64,
    }
    reports = {
        "build.json": {"platform": "linux/amd64"},
        "runtime.json": {
            "imageId": candidate["imageId"],
            "profile": candidate["profile"],
            "kind": "offline-packaging",
            "passed": True,
            "containersRemaining": [],
            "liveDeploymentVerified": False,
            "bicepAvm": {"checks": [{"name": "offline-compile", "passed": True}]},
        },
        "assessment.json": {
            "assessedAt": "2026-10-11T12:00:00Z",
            "outcome": "clean",
            "database": {
                "built": "2026-10-11T06:00:00Z",
                "valid": True,
                "schemaVersion": 6,
                "from": "https://example.com/db",
            },
        },
        "grype.json": {
            "matches": [
                {"vulnerability": {"severity": s}} for s in ["Medium", "Medium", "Low", "Unknown"]
            ],
            "ignoredMatches": [],
        },
        "sbom.syft.json": {"artifacts": [{"name": "one"}, {"name": "two"}]},
    }

    def save():
        for name, report in reports.items():
            write(tmp_path / name, report)
        write(
            tmp_path / "preparation-index.json", {name: digest(tmp_path / name) for name in reports}
        )
        candidate["preparationEvidenceSha256"] = digest(tmp_path / "preparation-index.json")
        write(tmp_path / "candidate.json", candidate)

    save()
    return SimpleNamespace(directory=tmp_path, candidate=candidate, reports=reports, save=save)


def test_summary_contains_full_identity_evidence_links_and_scope(prepared):
    result = approval.render(
        prepared.candidate,
        prepared.directory,
        "release",
        "[Artifact](https://example.com/artifact)",
    )
    for expected in (
        "Publish bicep-avm:0.1.0 to public GHCR",
        "linux/amd64",
        "| Recorded platform | <code>linux/amd64</code>",
        "ghcr.io/sokolaidev/maf-extensions/bicep-avm",
        prepared.candidate["registryDigest"],
        prepared.candidate["imageId"],
        digest(prepared.directory / "candidate.json"),
        prepared.candidate["preparationEvidenceSha256"],
        f"/blob/{'a' * 40}/images/bicep-sandbox/README.md#prepared-avm-profile",
        f"/commit/{'a' * 40}",
        "/actions/runs/123",
        "[Artifact]",
        "offline-compile",
        "| Inventory packages (Syft) | <code>2</code>",
        "| Vulnerability matches: Medium | <code>2</code>",
        "| Vulnerability matches: High | <code>0</code>",
        "| Ignored/suppressed matches | <code>0</code>",
        "| Database age at assessment (hours) | <code>6.0</code>",
        "2026-10-11T12:00:00Z",
        "24 hours",
        "failed refresh blocks publication",
        "require new approval",
        "sign provenance and SBOM",
        "anonymous pull",
        "post-publication consumer verification",
        "open this workflow run in a browser",
    ):
        assert expected in result


@pytest.mark.parametrize(
    "filename",
    ["runtime.json", "assessment.json", "grype.json", "sbom.syft.json", "preparation-index.json"],
)
def test_missing_reports_are_unavailable_not_zero_or_success(prepared, filename):
    (prepared.directory / filename).unlink()
    result = approval.render(prepared.candidate, prepared.directory, "release", "evidence")
    assert approval.UNAVAILABLE in result
    if filename in {"grype.json", "preparation-index.json"}:
        assert "| Vulnerability matches: High | <code>0</code>" not in result
    if filename in {"assessment.json", "preparation-index.json"}:
        assert "<code>clean</code>" not in result


@pytest.mark.parametrize(
    "filename",
    ["runtime.json", "assessment.json", "grype.json", "sbom.syft.json", "preparation-index.json"],
)
def test_changed_evidence_refuses_summary(prepared, filename):
    write(prepared.directory / filename, {})
    with pytest.raises(ValueError, match="differs|changed"):
        approval.render(prepared.candidate, prepared.directory, "release", "evidence")


def test_recovery_displays_original_reports_when_publication_refreshed_them(prepared):
    original = prepared.directory / "assessment.json"
    original.rename(prepared.directory / "approval-assessment.json")
    write(original, {"assessedAt": "2026-10-13T00:00:00Z", "outcome": "clean"})
    result = approval.render(
        prepared.candidate, prepared.directory, "recover", "evidence", stage="complete"
    )
    assert "2026-10-11T12:00:00Z" in result
    assert "2026-10-13T00:00:00Z" not in result
    assert "Recover complete evidence for bicep-avm:0.1.0" in result
    assert "does not authorize rebuilding" in result
    assert "original candidate-bound approval" in result


def test_cleanup_and_suppression_are_never_inferred(prepared):
    del prepared.reports["runtime.json"]["containersRemaining"]
    del prepared.reports["grype.json"]["ignoredMatches"]
    prepared.save()
    rows = dict(
        approval.evidence_rows(
            approval.bound_reports(prepared.directory, prepared.candidate), prepared.candidate
        )
    )
    assert rows["Cleanup outcome / remaining containers"] == approval.UNAVAILABLE
    assert rows["Ignored/suppressed matches"] == approval.UNAVAILABLE


def test_suppressed_findings_and_failed_runtime_are_visible(prepared):
    prepared.reports["runtime.json"]["passed"] = False
    prepared.reports["runtime.json"]["containersRemaining"] = ["probe"]
    prepared.reports["grype.json"]["ignoredMatches"] = [{"reason": "ignore-rule"}]
    prepared.reports["assessment.json"]["outcome"] = "vulnerable"
    prepared.save()
    result = approval.render(prepared.candidate, prepared.directory, "release", "evidence")
    assert "false" in result and "probe" in result and "ignore-rule" in result
    assert "<code>vulnerable</code>" in result
    assert "| Ignored/suppressed matches | <code>1</code>" in result


def test_evidence_and_abandonment_reason_cannot_inject_markdown(prepared):
    reason = "</code><script>alert(1)</script>\n| pass | [click](https://example.com)"
    result = approval.render(
        prepared.candidate, prepared.directory, "abandon", "evidence", reason=reason
    )
    assert "Permanently abandon bicep-avm:0.1.0" in result
    assert "cannot be reused" in result
    assert "<script>" not in result and "\n| pass |" not in result
    assert "&lt;script&gt;" in result


def test_every_profile_has_a_resolving_consumer_guide():
    assert set(approval.PROFILE_GUIDES) == set(profiles())
    for purpose, guide in approval.PROFILE_GUIDES.values():
        assert purpose
        assert (ROOT / guide.split("#")[0]).is_file()


@pytest.mark.parametrize("expired", [False, True])
def test_artifact_links_point_at_exact_retained_id(monkeypatch, expired):
    monkeypatch.setattr(
        approval.GitHub,
        "request",
        lambda *a: (
            '{"total_count":1,"artifacts":[{"id":456,"name":"retained-image-123","expired":'
            + str(expired).lower()
            + "}]}"
        ),
    )
    link = approval.artifact_link("123", "retained-image-123")
    assert link == approval.UNAVAILABLE if expired else "/actions/runs/123/artifacts/456" in link


def test_release_cli_writes_summary_and_metadata_without_changing_candidate(prepared, monkeypatch):
    summary = prepared.directory / "summary.md"
    output = prepared.directory / "output"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.setenv("GITHUB_RUN_ID", "123")
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "2")
    monkeypatch.setattr(approval, "artifact_link", lambda *a: "retained artifact")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "summary",
            "release",
            "--candidate",
            str(prepared.directory / "candidate.json"),
            "--directory",
            str(prepared.directory),
        ],
    )
    before = digest(prepared.directory / "candidate.json")
    approval.main()
    assert digest(prepared.directory / "candidate.json") == before
    assert "approval_name=Publish bicep-avm:0.1.0 to public GHCR" in output.read_text()
    assert "/actions/runs/123/attempts/2#summary" in output.read_text()
    assert before in summary.read_text()


def test_workflow_summaries_precede_unchanged_protection_and_candidate_binding():
    release = yaml.safe_load((ROOT / ".github/workflows/container-image-release.yml").read_text())
    recover = yaml.safe_load((ROOT / ".github/workflows/container-image-recover.yml").read_text())
    for workflow in (release, recover):
        assert "inputs.profile" in workflow["run-name"] and "inputs.version" in workflow["run-name"]
        assert workflow["permissions"] == {"contents": "read", "actions": "read"}
    jobs = release["jobs"]
    assert "Permanently abandon" in release["run-name"]
    assert jobs["approve"]["name"] == "${{ needs.prepare.outputs.approval_name }}"
    assert jobs["approve"]["environment"] == {
        "name": "container-release",
        "url": "${{ needs.prepare.outputs.summary_url }}",
    }
    assert jobs["abandon"]["environment"] == {
        "name": "container-release",
        "url": "${{ needs.policy.outputs.summary_url }}",
    }
    assert jobs["abandon"]["needs"] == "policy"
    assert jobs["approve"]["needs"] == "prepare"
    for job in ("reserve", "promote", "qualify", "complete", "sign-completion", "deliver"):
        assert (
            jobs[job]["env"]["APPROVED_CANDIDATE_SHA256"]
            == "${{ needs.prepare.outputs.candidate_sha256 }}"
        )
    prepare = jobs["prepare"]["steps"]
    renderer = next(s for s in prepare if s.get("id") == "approval-summary")
    assert "mode != 'delivery'" in renderer["if"]
    assert prepare.index(renderer) > next(
        i for i, s in enumerate(prepare) if s.get("name") == "Retain the original OCI candidate"
    )
    assert recover["jobs"]["write"]["environment"] == {
        "name": "container-release",
        "url": "${{ needs.inspect.outputs.summary_url }}",
    }
    assert recover["jobs"]["write"]["needs"] == "inspect"
    assert "inputs.stage" in recover["run-name"]
    inspect = recover["jobs"]["inspect"]["steps"]
    selection = next(s for s in inspect if s.get("id") == "selection")
    assert '--profile "$PROFILE" --version "$VERSION"' in selection["run"]
    assert inspect[-1]["id"] == "approval-summary"


@pytest.mark.parametrize("available", [True, False])
def test_abandonment_uses_reserved_candidate_and_available_original_reports(
    prepared, monkeypatch, available
):
    summary = prepared.directory / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    monkeypatch.setenv("GITHUB_OUTPUT", str(prepared.directory / "outputs"))
    monkeypatch.setenv("GITHUB_RUN_ID", "789")
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "1")
    monkeypatch.setattr(
        approval,
        "History",
        lambda: SimpleNamespace(
            head=lambda: SimpleNamespace(
                catalogue={
                    "releases": {
                        "bicep-avm/0.1.0": {"state": "incomplete", "candidate": prepared.candidate}
                    }
                }
            )
        ),
    )
    monkeypatch.setattr(
        approval,
        "artifact_link",
        lambda *a: "retained artifact" if available else approval.UNAVAILABLE,
    )
    downloads = []
    monkeypatch.setattr(approval, "run", lambda command, **kwargs: downloads.append(command))
    directory = prepared.directory if available else prepared.directory / "missing"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "summary",
            "abandon",
            "--profile",
            "bicep-avm",
            "--version",
            "0.1.0",
            "--reason",
            "Maintainer ended retries",
            "--directory",
            str(directory),
        ],
    )
    approval.main()
    result = summary.read_text()
    assert prepared.candidate["registryDigest"] in result
    assert "Permanently abandon" in result and "Maintainer ended retries" in result
    if available:
        assert downloads and "retained-image-123" in downloads[0]
        assert "offline-compile" in result
    else:
        assert not downloads
        assert approval.UNAVAILABLE in result
        assert "<code>clean</code>" not in result


def test_database_summary_does_not_reproduce_scanner_host_paths(prepared):
    prepared.reports["assessment.json"]["database"]["path"] = "scanner-local-cache"
    prepared.save()
    assert "scanner-local-cache" not in approval.render(
        prepared.candidate, prepared.directory, "release", "evidence"
    )
