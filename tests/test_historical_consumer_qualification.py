"""Historical consumers retain exact inputs without executing code on the signing runner."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
import test_consumer_qualification as shared
import yaml

consumer = shared.consumer
workflow = shared.workflow
repo = shared.repo


def advance(root):
    (root / "pyproject.toml").write_text("# Current main has different inputs.\n")
    return shared.legacy.commit(root)


def test_historical_subject_differs_from_main_but_signer_validates_selected_revision(
    repo, monkeypatch, tmp_path
):
    root, target = repo
    signer = advance(root)
    shared.trusted(monkeypatch, root, signer)
    subject = shared.expected(root, target)
    assert subject != shared.expected(root, signer)
    record = shared.claim(subject, signer) | {"consumerCommit": target}
    directory = tmp_path / "transfer"
    shared.write(directory / consumer.SUBJECT, subject)
    shared.write(directory / "consumer-qualification.json", record)
    workflow.validate_for_signing(
        root, directory, shared.legacy.PACKAGE, "shell", "docker", requested=target
    )
    with pytest.raises(ValueError, match="mismatched identity"):
        workflow.validate_for_signing(root, directory, shared.legacy.PACKAGE, "shell", "docker")


@pytest.mark.parametrize("field", ["sourceCommit", "consumerCommit", "runId", "runAttempt"])
def test_historical_transfer_rejects_wrong_revision_or_attempt(repo, monkeypatch, tmp_path, field):
    root, target = repo
    signer = advance(root)
    shared.trusted(monkeypatch, root, signer)
    subject = shared.expected(root, target)
    record = shared.claim(subject, signer) | {"consumerCommit": target}
    record[field] = "f" * 40 if field.endswith("Commit") else "9"
    directory = tmp_path / "transfer"
    shared.write(directory / consumer.SUBJECT, subject)
    shared.write(directory / "consumer-qualification.json", record)
    with pytest.raises(ValueError, match="workflow attempt"):
        workflow.validate_for_signing(
            root, directory, shared.legacy.PACKAGE, "shell", "docker", requested=target
        )


@pytest.mark.parametrize("requested", ["main", "HEAD~1", "--help", "a" * 39, "a" * 41])
def test_source_requires_full_commit_sha(repo, monkeypatch, requested):
    root, signer = repo
    shared.trusted(monkeypatch, root, signer)
    with pytest.raises(ValueError, match="immutable commit"):
        workflow.consumer_revision(root, requested)


def test_unmerged_source_is_refused(repo, monkeypatch):
    root, signer = repo
    unmerged = advance(root)
    subprocess.run(
        ["git", "checkout", "--detach", signer], cwd=root, check=True, capture_output=True
    )
    shared.trusted(monkeypatch, root, signer)
    with pytest.raises(subprocess.CalledProcessError):
        workflow.consumer_revision(root, unmerged)


@pytest.mark.parametrize(
    "policy",
    [
        "consumer_qualification.py",
        "select_image_security.py",
        "build_wolfi_python.py",
        "container_release.py",
        "container_release_history.py",
        "classic_docker_store.py",
    ],
)
def test_historical_fingerprint_policy_must_match(repo, monkeypatch, policy):
    root, target = repo
    directory = root / "scripts"
    directory.mkdir()
    (directory / policy).write_text("CHANGED = True\n")
    signer = shared.legacy.commit(root)
    shared.trusted(monkeypatch, root, signer)
    with pytest.raises(ValueError, match="replacement release"):
        workflow.consumer_revision(root, target)


def test_consumer_checkout_must_match_selected_revision_and_stay_clean(repo, monkeypatch, tmp_path):
    root, target = repo
    signer = advance(root)
    shared.trusted(monkeypatch, root, signer)
    checkout = tmp_path / "historical"
    subprocess.run(
        ["git", "clone", "--quiet", str(root), str(checkout)], check=True, capture_output=True
    )
    with pytest.raises(ValueError, match="differs from the selected"):
        workflow.checked_consumer(root, checkout, target)
    subprocess.run(
        ["git", "checkout", "--detach", target], cwd=checkout, check=True, capture_output=True
    )
    assert workflow.checked_consumer(root, checkout, target) == target
    (checkout / "uv.lock").write_text("dirty\n")
    with pytest.raises(ValueError, match="clean tracked"):
        workflow.checked_consumer(root, checkout, target)


def test_changed_docker_setup_action_requires_replacement(repo, monkeypatch):
    root, target = repo
    action = root / ".github/actions/setup-classic-docker/action.yml"
    action.parent.mkdir(parents=True)
    action.write_text("changed setup\n")
    signer = shared.legacy.commit(root)
    shared.trusted(monkeypatch, root, signer)
    with pytest.raises(ValueError, match="replacement release"):
        workflow.consumer_revision(root, target)


def test_historical_runtime_executes_selected_harness_and_retains_child_environment(
    tmp_path, monkeypatch
):
    checkout = tmp_path / "consumer"
    scripts = checkout / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "container_release.py").write_text(
        "import json\n"
        "def read(path): return json.loads(path.read_text())\n"
        "def write(path, value): path.write_text(json.dumps(value))\n"
    )
    (scripts / "check_container_release_image.py").write_text(
        "import os\n"
        "def check(profile, image, directory, *, published_runtime):\n"
        "    assert 'GH_TOKEN' not in os.environ\n"
        "    assert 'ACTIONS_ID_TOKEN_REQUEST_TOKEN' not in os.environ\n"
        "    assert 'UV_PROJECT_ENVIRONMENT' not in os.environ\n"
        "    return {'selectedHarness': str(os.getcwd()), 'published': published_runtime,\n"
        "            'profile': profile, 'imageId': image, 'passed': True, 'containersRemaining': []}\n"
    )
    monkeypatch.setenv("GH_TOKEN", "must-not-reach-consumer")
    monkeypatch.setenv("ACTIONS_ID_TOKEN_REQUEST_TOKEN", "must-not-reach-consumer")
    monkeypatch.setenv("UV_PROJECT_ENVIRONMENT", "wrong-environment")
    original = subprocess.run
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        assert command[:4] == ["uv", "run", "--locked", "python"]
        return original([sys.executable, *command[4:]], **kwargs)

    monkeypatch.setattr(subprocess, "run", run)
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    result = workflow.historical_runtime(
        checkout, "shell", shared.CONFIG, evidence, {"signed": True}
    )
    assert result["runtime"]["selectedHarness"] == str(checkout)
    assert result["runtime"]["published"] == {"signed": True}
    assert result["python"] and result["distributions"]
    assert len(calls) == 1


def test_historical_claim_still_authenticates_the_main_workflow_revision(repo, monkeypatch):
    root, target = repo
    signer = advance(root)
    subject = shared.expected(root, target)
    record = shared.claim(subject, signer) | {"consumerCommit": target}
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        return subprocess.CompletedProcess(
            command, 0, json.dumps(shared.attestation(Path(command[3]), record))
        )

    monkeypatch.setattr(subprocess, "run", run)
    assert consumer.verify(subject) == record
    assert commands[-1][-4:] == ["--source-digest", signer, "--signer-digest", signer]


@pytest.mark.workflow
def test_historical_checkout_executes_only_in_unsigned_job():
    root = Path(__file__).resolve().parents[1]
    value = yaml.safe_load((root / consumer.WORKFLOW).read_text("utf-8"))
    jobs = value["jobs"]
    assert jobs["select"]["outputs"]["source"] == "${{ steps.select.outputs.source }}"
    qualify = jobs["qualify"]
    historical = next(s for s in qualify["steps"] if s.get("with", {}).get("path") == ".consumer")
    assert historical["with"]["ref"] == "${{ needs.select.outputs.source }}"
    assert historical["with"]["persist-credentials"] is False
    assert historical["if"] == "needs.select.outputs.source != github.sha"
    assert any(s.get("uses") == "./.github/actions/setup-classic-docker" for s in qualify["steps"])
    signer = jobs["attest"]
    assert not any(s.get("with", {}).get("path") == ".consumer" for s in signer["steps"])
    validation = next(s for s in signer["steps"] if " validate " in s.get("run", ""))
    assert validation["env"]["SOURCE"] == "${{ needs.select.outputs.source }}"
    assert '--source "$SOURCE"' in validation["run"]
