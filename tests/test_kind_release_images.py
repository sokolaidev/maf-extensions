"""Kind releases require qualified compatible images independently of vulnerability monitoring."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from test_container_release_history import FakeGitHub
from test_verify_container_release import evidence as signed_evidence

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import check_kind_release_images as guard  # noqa: E402
import container_release_status as monitoring  # noqa: E402
from container_release import (  # noqa: E402
    COMPLETION,
    PROVENANCE,
    SPDX,
    assessment_status,
    complete,
    digest,
    observe,
    read,
    reserve,
    scan_result,
    validate_catalogue,
    write,
)
from container_release_assets import image_tag  # noqa: E402
from container_release_history import History, sha256  # noqa: E402
from verify_container_release import policy_flags  # noqa: E402

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
    status = {"outcome": "clean"}
    record["latestAttempt"] = status
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

    def release_evidence(client, selected, directory):
        calls.append(selected)
        (directory / "evidence-index.json").write_text(json.dumps({"qualification.json": IMAGE}))
        (directory / "qualification.json").write_text(json.dumps(qualification))
        return "https://example.com/evidence"

    monkeypatch.setattr(
        guard,
        "History",
        lambda client: SimpleNamespace(head=lambda: SimpleNamespace(catalogue=catalogue)),
    )
    monkeypatch.setattr(guard, "matching_source", lambda *args: None)
    monkeypatch.setattr(guard, "release_evidence", release_evidence)

    def unexpected_monitor_query(*args, **kwargs):
        pytest.fail("Package compatibility must not depend on live image monitoring")

    monkeypatch.setattr(monitoring, "report", unexpected_monitor_query)
    return catalogue, status, qualification, calls


def test_completed_matching_image_passes_without_a_security_verdict(evidence):
    catalogue, _, _, calls = evidence
    result = guard.check(Path("."), SOURCE, [PACKAGE])
    assert result[0]["image"].endswith("@" + IMAGE)
    assert "monitoringStatus" not in result[0]
    assert calls == [catalogue["releases"]["shell/0.1.0"]]


def test_missing_shell_image_blocks_release(evidence):
    evidence[0]["current"].clear()
    with pytest.raises(ValueError, match="no completed shell image"):
        guard.check(Path("."), SOURCE, [PACKAGE])
    assert evidence[3] == []


@pytest.mark.parametrize("status", ["vulnerable", "unavailable", "running"])
def test_image_security_health_does_not_block_a_compatible_package(evidence, status):
    evidence[1]["outcome"] = status
    evidence[1]["findings"] = [{"severity": "High", "id": "CVE-example"}]
    assert guard.check(Path("."), SOURCE, [PACKAGE])[0]["image"].endswith("@" + IMAGE)


def test_missing_monitor_attempt_does_not_block_a_compatible_package(evidence):
    evidence[0]["releases"]["shell/0.1.0"].pop("latestAttempt")
    assert guard.check(Path("."), SOURCE, [PACKAGE])


@pytest.mark.parametrize("replacement", ["shell/0.2.0", None])
def test_current_pointer_changed_during_verification_blocks_release(
    evidence, monkeypatch, replacement
):
    catalogue = evidence[0]
    verify = guard.release_evidence

    def advance(*args):
        result = verify(*args)
        if replacement is None:
            catalogue["current"].pop("shell")
        else:
            catalogue["current"]["shell"] = replacement
        return result

    monkeypatch.setattr(guard, "release_evidence", advance)
    with pytest.raises(ValueError, match="Current shell image changed"):
        guard.check(Path("."), SOURCE, [PACKAGE])


def test_monitor_updates_and_unrelated_current_pointers_do_not_block_release(evidence, monkeypatch):
    verify = guard.release_evidence

    def advance(*args):
        result = verify(*args)
        evidence[0]["current"]["document"] = "document/0.2.0"
        evidence[1]["outcome"] = "unavailable"
        return result

    monkeypatch.setattr(guard, "release_evidence", advance)
    assert guard.check(Path("."), SOURCE, [PACKAGE])


def test_catalogue_unavailable_after_verification_blocks_release(evidence, monkeypatch):
    snapshots = iter([SimpleNamespace(catalogue=evidence[0]), None])
    monkeypatch.setattr(guard, "History", lambda _: SimpleNamespace(head=lambda: next(snapshots)))
    with pytest.raises(ValueError, match="catalogue became unavailable"):
        guard.check(Path("."), SOURCE, [PACKAGE])


def test_all_profiles_are_rechecked_after_the_last_verification(evidence, monkeypatch):
    catalogue, _, qualification, calls = evidence
    other = json.loads(json.dumps(catalogue["releases"]["shell/0.1.0"]))
    other["profile"] = other["candidate"]["profile"] = "document"
    catalogue["releases"]["document/0.1.0"] = other
    catalogue["current"]["document"] = "document/0.1.0"
    monkeypatch.setattr(guard, "KIND_IMAGES", {PACKAGE: ("shell", "document")})
    verify = guard.release_evidence

    def advance(client, record, directory):
        qualification["candidate"] = record["candidate"]
        qualification["runtime"]["profile"] = record["profile"]
        if record["profile"] == "document":
            catalogue["current"]["shell"] = "shell/0.2.0"
        return verify(client, record, directory)

    monkeypatch.setattr(guard, "release_evidence", advance)
    with pytest.raises(ValueError, match="Current shell image changed"):
        guard.check(Path("."), SOURCE, [PACKAGE])
    assert [record["profile"] for record in calls] == ["shell", "document"]


@pytest.fixture
def authenticated_release(repo, tmp_path, monkeypatch):
    root, source = repo
    directory = tmp_path / "evidence"
    directory.mkdir()
    expected, statements = signed_evidence(directory, profile="shell")
    expected["sourceCommit"] = source
    candidate = {key: value for key, value in expected.items() if key != "evidenceIndexSha256"}
    at = "2026-01-01T00:00:00Z"
    report = {
        "source": {"type": "image", "target": {"imageID": candidate["imageId"]}},
        "descriptor": {
            "db": {"status": {"valid": True, "built": at, "from": "test-db", "schemaVersion": "v6"}}
        },
        "matches": [{"vulnerability": {"id": "CVE-example", "severity": "High"}}],
    }
    write(directory / "grype.json", report)
    write(directory / "assessment.json", scan_result(report, candidate["imageId"], at))
    write(
        directory / "qualification.json",
        {
            "candidate": candidate,
            "runId": candidate["attemptId"],
            "anonymousPullVerified": True,
            "identity": {"candidateIdentityVerified": True},
            "runtime": {
                "passed": True,
                "profile": "shell",
                "imageId": candidate["imageId"],
                "containersRemaining": [],
            },
        },
    )
    write(directory / "completion.jsonl", {})
    index = read(directory / "evidence-index.json")
    index.update({name: "" for name in ("qualification.json", "assessment.json")})
    write(directory / "evidence-index.json", {name: digest(directory / name) for name in index})
    evidence_hash = digest(directory / "evidence-index.json")
    completion = statements[COMPLETION][0]["verificationResult"]["statement"]["predicate"]
    completion.update(sourceCommit=source, evidenceIndexSha256=evidence_hash)
    provenance = statements[PROVENANCE][0]["verificationResult"]["statement"]["predicate"]
    provenance["buildDefinition"]["resolvedDependencies"][0]["digest"]["gitCommit"] = source
    client = FakeGitHub()
    history = History(client)

    def delivered(catalogue):
        catalogue["releases"]["shell/0.1.0"]["delivery"] = "complete"
        return catalogue

    for operation, transform in (
        ("reserve", lambda catalogue: reserve(catalogue, candidate, at)),
        ("complete", lambda catalogue: complete(catalogue, candidate, evidence_hash, at)),
        ("deliver", delivered),
    ):
        history.append(transform, operation=operation, source=source, at=at)
    release = client.create(image_tag(candidate), source)
    client.publish(release["id"])
    for asset_id, path in enumerate(sorted(directory.iterdir()), start=1000):
        raw = path.read_bytes()
        client.blobs[asset_id] = raw
        client.entries[release["id"]]["assets"].append(
            {
                "id": asset_id,
                "name": path.name,
                "size": len(raw),
                "state": "uploaded",
                "digest": sha256(raw),
            }
        )
    monkeypatch.setattr(
        client, "download_file", lambda asset_id, path: path.write_bytes(client.blobs[asset_id])
    )
    monkeypatch.setattr(guard, "GitHub", lambda: client)
    actual_run = subprocess.run
    calls = []

    def run(command, **kwargs):
        if command[0] != "gh":
            return actual_run(command, **kwargs)
        assert command[:3] == ["gh", "attestation", "verify"]
        predicate = command[command.index("--predicate-type") + 1]
        assert command[4:-2] == policy_flags(candidate, predicate)
        assert command[-2] == "--bundle"
        calls.append(predicate)
        return subprocess.CompletedProcess(command, 0, json.dumps(statements[predicate]))

    monkeypatch.setattr(subprocess, "run", run)
    return history, client, statements, calls, candidate, at


@pytest.mark.parametrize("outcome", [None, "vulnerable", "unavailable", "clean"])
def test_authenticated_vulnerable_original_and_stale_monitoring_allow_package(
    repo, authenticated_release, outcome
):
    history, _, _, calls, candidate, at = authenticated_release
    root, source = repo
    if outcome is not None:
        history.append(
            lambda catalogue: observe(
                catalogue,
                "shell/0.1.0",
                {
                    "digest": candidate["registryDigest"],
                    "assessedAt": at,
                    "outcome": outcome,
                },
            ),
            operation="monitor",
            source=source,
            at=at,
        )
    catalogue = history.head().catalogue
    validate_catalogue(catalogue)
    if outcome == "clean":
        assert (
            assessment_status(catalogue["releases"]["shell/0.1.0"], "2026-01-04T00:00:00Z")
            == "stale"
        )
    assert guard.check(root, source, [PACKAGE])[0]["image"].endswith(candidate["registryDigest"])
    assert calls == [PROVENANCE, SPDX, COMPLETION]


@pytest.mark.parametrize("failure", ["subject", "completion-index", "asset-bytes"])
def test_authenticated_compatibility_rejects_changed_evidence(repo, authenticated_release, failure):
    root, source = repo
    _, client, statements, _, _, _ = authenticated_release
    if failure == "subject":
        statements[PROVENANCE][0]["verificationResult"]["statement"]["subject"][0]["digest"][
            "sha256"
        ] = "f" * 64
    elif failure == "completion-index":
        statements[COMPLETION][0]["verificationResult"]["statement"]["predicate"][
            "evidenceIndexSha256"
        ] = "sha256:" + "f" * 64
    else:
        client.blobs[1000] += b"tampered"
    with pytest.raises(ValueError, match="subject|index"):
        guard.check(root, source, [PACKAGE])


def test_current_entry_must_identify_the_required_profile(evidence):
    evidence[0]["releases"]["shell/0.1.0"]["profile"] = "other"
    with pytest.raises(ValueError, match="another profile"):
        guard.check(Path("."), SOURCE, [PACKAGE])


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
        ValueError("mismatched evidence index"),
    ],
)
def test_authenticated_release_failure_propagates(evidence, monkeypatch, failure):
    def fail(*args):
        raise failure

    monkeypatch.setattr(guard, "release_evidence", fail)
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
