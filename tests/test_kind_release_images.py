"""Kind releases refuse missing, incompatible or unhealthy image evidence before merge/upload."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import check_kind_release_images as guard  # noqa: E402

PACKAGE = "maf-sandbox-shell"
SOURCE = "a" * 40
IMAGE = "sha256:" + "b" * 64


def commit(root):
    subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-qm", "fixture"], cwd=root, check=True, capture_output=True)
    return guard.revision(root, "HEAD")


@pytest.fixture
def repo(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    for key, value in (
        ("user.name", "Test"),
        ("user.email", "test@example.com"),
        ("core.autocrlf", "false"),
    ):
        subprocess.run(["git", "config", key, value], cwd=tmp_path, check=True)
    for package in (PACKAGE, "maf-sandbox", "maf-sandbox-docker"):
        directory = tmp_path / "packages" / package
        (directory / "src").mkdir(parents=True)
        (directory / "src" / "module.py").write_text("VALUE = 1\n")
        (directory / "pyproject.toml").write_text(
            f'[project]\nname = "{package}"\nversion = "0.1.0"\ndependencies = []\n'
        )
    (tmp_path / "uv.lock").write_text(
        'version = 1\n[[package]]\nname = "maf-sandbox-shell"\nversion = "0.1.0"\n'
        'source = {editable = "packages/maf-sandbox-shell"}\n'
    )
    return tmp_path, commit(tmp_path)


def test_version_only_release_preserves_qualified_environment(repo):
    root, base = repo
    metadata = root / "packages" / PACKAGE / "pyproject.toml"
    metadata.write_text(metadata.read_text().replace("0.1.0", "0.1.1"))
    lock = root / "uv.lock"
    lock.write_text(lock.read_text().replace("0.1.0", "0.1.1"))
    (metadata.parent / "CHANGELOG.md").write_text("New release\n")
    head = commit(root)
    assert guard.releasing(root, base, head) == [PACKAGE]
    guard.matching_source(root, base, head, PACKAGE, "shell")


@pytest.mark.parametrize(
    "path,content",
    [
        (f"packages/{PACKAGE}/src/module.py", "VALUE = 2\n"),
        ("packages/maf-sandbox/src/module.py", "VALUE = 2\n"),
        ("packages/maf-sandbox-docker/src/module.py", "VALUE = 2\n"),
        ("uv.lock", "version = 2\n"),
        ("pyproject.toml", '[project]\nname = "workspace"\n'),
    ],
)
def test_implementation_pr_can_merge_but_old_image_cannot_qualify_release(repo, path, content):
    root, base = repo
    (root / path).write_text(content)
    head = commit(root)
    assert guard.releasing(root, base, head) == []
    with pytest.raises(ValueError, match="qualification"):
        guard.matching_source(root, base, head, PACKAGE, "shell")


def test_dependency_change_is_not_a_version_only_bump(repo):
    root, base = repo
    path = root / "packages" / PACKAGE / "pyproject.toml"
    path.write_text(path.read_text().replace("dependencies = []", 'dependencies = ["new>=1"]'))
    with pytest.raises(ValueError, match="dependency/build"):
        guard.matching_source(root, base, commit(root), PACKAGE, "shell")


@pytest.mark.parametrize(
    "path",
    [
        "images/shell-sandbox/image.json",
        "images/shell-sandbox/runner.py",
        "images/shell-sandbox/Dockerfile",
        "images/shell-sandbox/dependencies.json",
        "scripts/shell_qualification.py",
        "scripts/shell_runtime_devices.py",
    ],
)
def test_image_payload_or_dependency_changes_require_new_qualification(repo, path):
    root, base = repo
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("changed input\n")
    head = commit(root)
    assert guard.releasing(root, base, head) == []
    with pytest.raises(ValueError, match="shell image inputs"):
        guard.matching_source(root, base, head, PACKAGE, "shell")


@pytest.mark.parametrize(
    "path",
    [
        "images/graphviz-sandbox/image.json",
        "images/shell-sandbox/README.md",
    ],
)
def test_unrelated_images_and_consumer_guides_do_not_invalidate_shell(repo, path):
    root, base = repo
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("unrelated change\n")
    guard.matching_source(root, base, commit(root), PACKAGE, "shell")


@pytest.mark.parametrize(
    "package,profile",
    [(package, profile) for package, profiles in guard.KIND_IMAGES.items() for profile in profiles],
)
def test_every_required_profile_is_compared(evidence, monkeypatch, package, profile):
    catalogue, _, qualification, _ = evidence
    record = catalogue["releases"].pop("shell/0.1.0")
    record["profile"] = profile
    record["candidate"]["profile"] = profile
    qualification["runtime"]["profile"] = profile
    catalogue["current"] = {profile: f"{profile}/0.1.0"}
    catalogue["releases"] = {f"{profile}/0.1.0": record}
    monkeypatch.setattr(guard, "KIND_IMAGES", {package: (profile,)})
    comparisons = []
    monkeypatch.setattr(guard, "matching_source", lambda *args: comparisons.append(args))
    guard.check(Path("."), SOURCE, [package])
    assert comparisons == [(Path("."), SOURCE, SOURCE, package, profile)]


def test_first_kind_release_is_selected_and_removed_metadata_fails(repo):
    root, base = repo
    path = root / "packages/maf-sandbox-drawio/pyproject.toml"
    path.parent.mkdir()
    path.write_text('[project]\nname = "maf-sandbox-drawio"\nversion = "0.1.0"\n')
    assert guard.releasing(root, base, commit(root)) == ["maf-sandbox-drawio"]
    (root / "packages" / PACKAGE / "pyproject.toml").unlink()
    with pytest.raises(ValueError, match="removed"):
        guard.releasing(root, base, commit(root))


def test_unreleased_kind_scaffold_can_merge_before_first_image(repo):
    root, base = repo
    path = root / "packages/maf-sandbox-drawio/pyproject.toml"
    path.parent.mkdir()
    path.write_text('[project]\nname = "maf-sandbox-drawio"\nversion = "0.0.0"\n')
    scaffold = commit(root)
    assert guard.releasing(root, base, scaffold) == []
    path.write_text(path.read_text().replace("0.0.0", "0.1.0"))
    assert guard.releasing(root, scaffold, commit(root)) == ["maf-sandbox-drawio"]


@pytest.fixture
def evidence(monkeypatch):
    candidate = {"profile": "shell", "imageId": IMAGE, "attemptId": "1"}
    record = {
        "profile": "shell",
        "sourceCommit": SOURCE,
        "registryDigest": IMAGE,
        "candidate": candidate,
    }
    catalogue = {"current": {"shell": "shell/0.1.0"}, "releases": {"shell/0.1.0": record}}
    status = {
        "monitoringStatus": "clean",
        "publicationState": "completed",
        "releaseScope": "current",
        "digest": IMAGE,
    }
    qualification = {
        "candidate": candidate,
        "runId": "1",
        "anonymousPullVerified": True,
        "identity": {"candidateIdentityVerified": True},
        "runtime": {
            "passed": True,
            "profile": "shell",
            "imageId": IMAGE,
            "containersRemaining": [],
        },
    }
    calls = []

    def baseline(client, selected, directory):
        calls.append(selected)
        (directory / "evidence-index.json").write_text(json.dumps({"qualification.json": IMAGE}))
        (directory / "qualification.json").write_text(json.dumps(qualification))
        return {"evidence": "https://example.com/evidence"}

    monkeypatch.setattr(
        guard,
        "History",
        lambda client: SimpleNamespace(head=lambda: SimpleNamespace(catalogue=catalogue)),
    )
    monkeypatch.setattr(guard, "matching_source", lambda *args: None)
    monkeypatch.setattr(guard, "baseline", baseline)
    monkeypatch.setattr(guard, "report", lambda *args, **kwargs: status)
    return catalogue, status, qualification, calls


def test_completed_matching_image_with_clean_current_monitor_passes(evidence):
    catalogue, _, _, calls = evidence
    result = guard.check(Path("."), SOURCE, [PACKAGE])
    assert result[0]["image"].endswith("@" + IMAGE)
    assert result[0]["monitoringStatus"] == "clean"
    assert calls == [catalogue["releases"]["shell/0.1.0"]]


def test_missing_shell_image_blocks_release(evidence):
    evidence[0]["current"].clear()
    with pytest.raises(ValueError, match="no completed shell image"):
        guard.check(Path("."), SOURCE, [PACKAGE])
    assert evidence[3] == []


@pytest.mark.parametrize("status", ["vulnerable", "stale", "unavailable", "no-longer-monitored"])
def test_only_current_clean_monitoring_allows_a_release(evidence, status):
    evidence[1]["monitoringStatus"] = status
    with pytest.raises(ValueError, match="release refused"):
        guard.check(Path("."), SOURCE, [PACKAGE])


@pytest.mark.parametrize(
    "field,value",
    [
        ("releaseScope", "historical"),
        ("publicationState", "incomplete"),
        ("digest", "other"),
        ("publicationFailure", "unexpected-registry-digest"),
    ],
)
def test_monitoring_must_still_identify_the_completed_current_image(evidence, field, value):
    evidence[1][field] = value
    with pytest.raises(ValueError, match="release refused") as failure:
        guard.check(Path("."), SOURCE, [PACKAGE])
    assert f"{field}: {value}" in str(failure.value)
    assert "restore a completed, matching current image release" in str(failure.value)


@pytest.mark.parametrize("workflow_status", ["queued", "in_progress", "waiting"])
@pytest.mark.parametrize("recorded_outcome", ["clean", "running"])
def test_cli_explains_pending_monitor_and_recorded_image_assessment(
    evidence, monkeypatch, capsys, workflow_status, recorded_outcome
):
    status = evidence[1]
    reason = "Latest monitor workflow has no confirmed successful state delivery"
    status.update(
        monitoringStatus="unavailable",
        reason=reason,
        observedMonitorWorkflow={
            "id": 201,
            "run_attempt": 2,
            "status": workflow_status,
            "conclusion": None,
        },
        latestAttempt={
            "outcome": recorded_outcome,
            "monitorRunId": "200",
            "monitorRunAttempt": 1,
            "assessedAt": "2026-01-01T00:00:00Z",
        },
    )
    monkeypatch.setattr(sys, "argv", ["check_kind_release_images.py", "--package", PACKAGE])
    monkeypatch.setattr(guard, "revision", lambda *args: SOURCE)
    monkeypatch.setattr(guard, "document", lambda *args: {"project": {"name": PACKAGE}})
    with pytest.raises(SystemExit) as failure:
        guard.main()
    assert failure.value.code == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert f"Kind release blocked: {PACKAGE}/shell" in output.err
    assert f"Image: {guard.PREFIX}/shell@{IMAGE}" in output.err
    assert f"Reason: {reason}" in output.err
    assert "https://github.com/sokolaidev/maf-extensions/actions/runs/201/attempts/2" in output.err
    assert f"status={workflow_status}" in output.err
    assert f"Recorded image assessment: {recorded_outcome} (run=200, attempt=1" in output.err
    assert "Wait for the latest monitor to finish and record a clean assessment" in output.err


@pytest.mark.parametrize(
    "monitoring,action",
    [
        ("vulnerable", "Remediate the image findings"),
        ("stale", "obtain a fresh clean assessment"),
        ("no-longer-monitored", "current image that is still monitored"),
        ("unavailable", "Inspect the monitor and catalogue evidence"),
    ],
)
def test_unhealthy_assessments_explain_recovery_without_suggesting_blind_retry(
    evidence, monitoring, action
):
    evidence[1]["monitoringStatus"] = monitoring
    with pytest.raises(ValueError) as failure:
        guard.check(Path("."), SOURCE, [PACKAGE])
    assert action in str(failure.value)
    assert "Wait for the latest monitor" not in str(failure.value)


def test_failed_monitor_links_exact_attempt_and_preserves_failure_reason(evidence):
    evidence[1].update(
        monitoringStatus="unavailable",
        reason="Latest monitor workflow has no confirmed successful state delivery",
        monitorWorkflow={
            "id": 201,
            "run_attempt": 2,
            "status": "completed",
            "conclusion": "failure",
        },
    )
    with pytest.raises(ValueError) as failure:
        guard.check(Path("."), SOURCE, [PACKAGE])
    message = str(failure.value)
    assert "actions/runs/201/attempts/2" in message
    assert "status=completed, conclusion=failure" in message
    assert "Reason: Latest monitor workflow has no confirmed successful state delivery" in message
    assert "Inspect the monitor and catalogue evidence" in message
    assert "Wait for the latest monitor" not in message


def test_unavailable_evidence_without_monitor_keeps_safe_reason(evidence):
    evidence[1].clear()
    evidence[1].update(monitoringStatus="unavailable", reason="Monitoring evidence unavailable")
    with pytest.raises(ValueError) as failure:
        guard.check(Path("."), SOURCE, [PACKAGE])
    message = str(failure.value)
    assert "Reason: Monitoring evidence unavailable" in message
    assert "Latest observed monitor:" not in message
    assert "Inspect the monitor and catalogue evidence" in message


def test_pending_monitor_does_not_mask_catalogue_retrieval_failure(evidence):
    evidence[1].update(
        monitoringStatus="unavailable",
        reason="Monitoring evidence unavailable",
        observedMonitorWorkflow={
            "id": 201,
            "run_attempt": 2,
            "status": "queued",
            "conclusion": None,
        },
    )
    with pytest.raises(ValueError) as failure:
        guard.check(Path("."), SOURCE, [PACKAGE])
    assert "Inspect the monitor and catalogue evidence" in str(failure.value)
    assert "Wait for the latest monitor" not in str(failure.value)


def test_diagnostics_do_not_dump_scanner_metadata(evidence):
    evidence[1].update(
        monitoringStatus="unavailable",
        latestAttempt={
            "outcome": "unavailable",
            "reason": "Monitor worker did not deliver an observation",
            "database": {"path": "private scanner path"},
        },
        registryDiscovery={"outcome": "unavailable"},
    )
    with pytest.raises(ValueError) as failure:
        guard.check(Path("."), SOURCE, [PACKAGE])
    assert "private scanner path" not in str(failure.value)
    assert "Image assessment reason: Monitor worker did not deliver an observation" in str(
        failure.value
    )
    assert "Registry discovery is unavailable" in str(failure.value)


@pytest.mark.parametrize(
    "field,value",
    [
        ("passed", False),
        ("passed", 1),
        ("imageId", "other"),
        ("profile", "other"),
        ("containersRemaining", ["container"]),
    ],
)
def test_signed_runtime_must_have_passed_for_selected_image(evidence, field, value):
    evidence[2]["runtime"][field] = value
    with pytest.raises(ValueError, match="qualification"):
        guard.check(Path("."), SOURCE, [PACKAGE])


@pytest.mark.parametrize(
    "field,value",
    [("runId", "2"), ("anonymousPullVerified", False), ("identity", {}), ("candidate", {})],
)
def test_qualification_must_identify_anonymous_pulled_candidate(evidence, field, value):
    evidence[2][field] = value
    with pytest.raises(ValueError, match="qualification"):
        guard.check(Path("."), SOURCE, [PACKAGE])


def test_unindexed_qualification_is_not_trusted(tmp_path):
    (tmp_path / "evidence-index.json").write_text("{}")
    with pytest.raises(ValueError, match="Signed evidence"):
        guard.qualified(tmp_path, {})


@pytest.mark.parametrize(
    "failure",
    [
        ValueError("incomplete delivery"),
        ValueError("bad signature"),
        ValueError("High/Critical assessment"),
    ],
)
def test_authenticated_baseline_failure_propagates(evidence, monkeypatch, failure):
    def fail(*args):
        raise failure

    monkeypatch.setattr(guard, "baseline", fail)
    with pytest.raises(ValueError, match=str(failure)):
        guard.check(Path("."), SOURCE, [PACKAGE])


def test_non_image_packages_and_implementation_changes_need_no_catalogue(monkeypatch):
    def unexpected(*args):
        raise AssertionError("must not query image history")

    monkeypatch.setattr(guard, "History", unexpected)
    assert guard.check(Path("."), SOURCE, []) == []
    assert guard.check(Path("."), SOURCE, ["maf-compaction", "maf-sandbox-codeact"]) == []


@pytest.mark.workflow
def test_version_gate_joins_required_context_and_checks_upload_after_approval():
    root = Path(__file__).resolve().parents[1]
    tests = yaml.safe_load((root / ".github/workflows/tests.yml").read_text(encoding="utf-8"))
    job = tests["jobs"]["kind-images"]
    assert "kind-images" in tests["jobs"]["tests"]["needs"]
    assert "if" not in job
    assert job["permissions"] == {"contents": "read", "actions": "read"}
    step = next(s for s in job["steps"] if "--base" in s.get("run", ""))
    assert step["if"] == "github.event_name == 'pull_request'"
    assert (
        "--base" in step["run"]
        and step["env"]["BASE_SHA"] == "${{ github.event.pull_request.base.sha }}"
    )
    publish = yaml.safe_load(
        (root / ".github/workflows/publish-packages.yml").read_text(encoding="utf-8")
    )
    for name in ("build", "publish"):
        assert publish["jobs"][name]["permissions"]["actions"] == "read"
        steps = publish["jobs"][name]["steps"]
        guard_step = next(s for s in steps if "check_kind_release_images.py" in s.get("run", ""))
        assert "if" not in guard_step
        assert '--package "$PACKAGE"' in guard_step["run"]
    steps = publish["jobs"]["publish"]["steps"]
    check_index = next(
        i for i, s in enumerate(steps) if "check_kind_release_images.py" in s.get("run", "")
    )
    upload_index = next(i for i, s in enumerate(steps) if s.get("name") == "Publish")
    assert check_index < upload_index
    checkout = next(s for s in steps if s.get("with", {}).get("path") == "release-guard")
    assert "if" not in checkout
    assert checkout["with"]["fetch-depth"] == 0
    assert "images/release-profiles.json" in checkout["with"]["sparse-checkout"]
