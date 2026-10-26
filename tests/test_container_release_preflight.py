"""Ineligible versions stop before builds; reservation rechecks current state and exact bytes."""

from __future__ import annotations

import copy
import runpy
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import container_release_preflight as policy  # noqa: E402
import container_release_publish as publisher  # noqa: E402
import prepare_container_release as preparation  # noqa: E402
import prepare_container_release_dispatch as dispatcher  # noqa: E402
from container_release import (  # noqa: E402
    abandon,
    complete,
    empty_catalogue,
    read,
    reserve,
    write,
)

pytestmark = pytest.mark.workflow
AT = "2026-01-01T00:00:00Z"
LATER = "2026-01-02T00:00:00Z"
CANDIDATE = {
    "profile": "shell",
    "version": "0.1.0",
    "sourceCommit": "a" * 40,
    "sourceRef": "refs/heads/main",
    "attemptId": "123",
    "registryDigest": "sha256:" + "b" * 64,
    "assessedManifestDigest": "sha256:" + "b" * 64,
    "imageId": "sha256:" + "c" * 64,
}


@pytest.fixture
def history(monkeypatch):
    state = SimpleNamespace(catalogue=empty_catalogue())
    monkeypatch.setattr(policy.History, "head", lambda self: state)
    monkeypatch.setattr(
        policy.History, "append", lambda *a, **kw: pytest.fail("preflight must not write history")
    )
    return state


def check(candidate=CANDIDATE):
    return policy.preflight(
        candidate["profile"],
        candidate["version"],
        candidate["sourceCommit"],
        candidate["attemptId"],
    )


@pytest.mark.parametrize("no_history", [False, True])
@pytest.mark.parametrize("version", ["0.1.0", "0.2.0"])
def test_initial_version_uses_only_validated_history(history, monkeypatch, no_history, version):
    if no_history:
        monkeypatch.setattr(policy.History, "head", lambda self: None)
    before = copy.deepcopy(history.catalogue)
    if version == "0.1.0":
        assert check() == empty_catalogue()
    else:
        with pytest.raises(ValueError, match=r"shell:0.2.0:.*first reserved version must be 0.1.0"):
            check(CANDIDATE | {"version": version})
    assert history.catalogue == before


@pytest.mark.parametrize("entry", ["abandoned", "unexpected", "source", "owner", "overtaken"])
def test_existing_ineligible_versions_fail_read_only(history, entry):
    history.catalogue = reserve(empty_catalogue(), CANDIDATE, AT)
    request = CANDIDATE.copy()
    if entry == "abandoned":
        history.catalogue = abandon(history.catalogue, "shell/0.1.0", "expired", LATER)
    elif entry == "unexpected":
        unexpected = "sha256:" + "d" * 64
        history.catalogue["releases"]["shell/0.1.0"]["unexpectedDigests"] = {
            unexpected: {"digest": unexpected, "discoveredAt": LATER}
        }
    elif entry == "source":
        request["sourceCommit"] = "d" * 40
    elif entry == "owner":
        request["attemptId"] = "456"
    else:
        newer = CANDIDATE | {"version": "0.2.0", "attemptId": "456"}
        history.catalogue = complete(
            reserve(history.catalogue, newer, LATER), newer, CANDIDATE["imageId"], LATER
        )
    before = copy.deepcopy(history.catalogue)
    with pytest.raises(ValueError, match="shell:0.1.0:"):
        check(request)
    with pytest.raises(ValueError):
        reserve(history.catalogue, request, LATER)
    assert history.catalogue == before


def test_unused_version_below_current_is_rejected_but_newer_is_eligible(history):
    history.catalogue = reserve(empty_catalogue(), CANDIDATE, AT)
    newer = CANDIDATE | {"version": "0.2.0", "attemptId": "456"}
    history.catalogue = complete(
        reserve(history.catalogue, newer, LATER), newer, CANDIDATE["imageId"], LATER
    )
    with pytest.raises(ValueError, match="shell:0.1.1:.*overtaken"):
        check(CANDIDATE | {"version": "0.1.1"})
    check(CANDIDATE | {"version": "0.2.1"})


def test_abandoned_first_reservation_allows_a_new_version(history):
    history.catalogue = abandon(
        reserve(empty_catalogue(), CANDIDATE, AT), "shell/0.1.0", "expired", LATER
    )
    check(CANDIDATE | {"version": "0.1.1", "attemptId": "456"})


@pytest.mark.parametrize("completed", [False, True])
def test_retained_retry_preserves_candidate_checks(history, completed):
    candidate = CANDIDATE | {"preparationEvidenceSha256": "sha256:" + "e" * 64}
    history.catalogue = reserve(empty_catalogue(), candidate, AT)
    if completed:
        history.catalogue = complete(history.catalogue, candidate, CANDIDATE["imageId"], AT)
        newer = CANDIDATE | {"version": "0.2.0", "attemptId": "456"}
        history.catalogue = complete(
            reserve(history.catalogue, newer, LATER), newer, CANDIDATE["imageId"], LATER
        )
    before = copy.deepcopy(history.catalogue)
    check(candidate)
    assert reserve(history.catalogue, candidate, LATER) == before
    for field in (
        "registryDigest",
        "assessedManifestDigest",
        "imageId",
        "preparationEvidenceSha256",
    ):
        with pytest.raises(ValueError, match="identity|evidence"):
            reserve(history.catalogue, candidate | {field: "sha256:" + "f" * 64}, LATER)
    assert history.catalogue == before


@pytest.mark.parametrize(
    "error",
    [
        TimeoutError("GitHub unavailable"),
        subprocess.CalledProcessError(1, "gh"),
        ValueError("bad asset"),
    ],
)
def test_unavailable_history_is_not_an_empty_catalogue(history, monkeypatch, error):
    def unavailable(self):
        raise error

    monkeypatch.setattr(policy.History, "head", unavailable)
    with pytest.raises(
        ValueError, match="shell:0.1.0: Cannot validate release catalogue.*Check GitHub"
    ):
        check()


@pytest.mark.parametrize(
    "invalid", [{}, {"schemaVersion": 1, "releases": {}, "current": {"shell": "missing"}}]
)
def test_invalid_catalogue_cannot_authorize_preparation(history, invalid):
    history.catalogue = invalid
    with pytest.raises(ValueError, match="Cannot validate release catalogue"):
        check()


@pytest.mark.parametrize("entrypoint", ["dispatch", "prepare"])
def test_invalid_first_version_cannot_build_or_scan(history, monkeypatch, tmp_path, entrypoint):
    monkeypatch.setattr(dispatcher, "context", lambda: (CANDIDATE["sourceCommit"], "123", "1"))
    monkeypatch.setattr(preparation, "require_source", lambda source: None)
    monkeypatch.setattr(preparation, "run", lambda *a, **kw: pytest.fail("must not build"))
    monkeypatch.setattr(preparation, "assess", lambda *a, **kw: pytest.fail("must not scan"))
    output = tmp_path / "candidate"
    with pytest.raises(ValueError, match="shell:0.2.0:.*0.1.0"):
        if entrypoint == "dispatch":
            dispatcher.dispatch("shell", "0.2.0", output, tmp_path / "selection")
        else:
            preparation.prepare("shell", "0.2.0", CANDIDATE["sourceCommit"], "123", output)
    assert not output.exists()
    assert not (tmp_path / "selection").exists()


def test_retained_candidate_retry_reaches_selection_without_rebuilding(
    history, monkeypatch, tmp_path
):
    history.catalogue = reserve(empty_catalogue(), CANDIDATE, AT)
    monkeypatch.setattr(dispatcher, "context", lambda: (CANDIDATE["sourceCommit"], "123", "2"))
    monkeypatch.setattr(dispatcher, "retained_available", lambda run_id: True)
    monkeypatch.setattr(dispatcher, "run", lambda *a, **kw: None)
    monkeypatch.setattr(dispatcher, "validate_preparation", lambda *a: None)
    monkeypatch.setattr(dispatcher, "validate_layout", lambda *a: {"imageId": CANDIDATE["imageId"]})
    monkeypatch.setattr(dispatcher, "prepare", lambda *a, **kw: pytest.fail("must not rebuild"))
    outputs = []
    monkeypatch.setattr(dispatcher, "outputs", outputs.append)
    write(tmp_path / "candidate/candidate.json", CANDIDATE)
    dispatcher.dispatch("shell", "0.1.0", tmp_path / "candidate", tmp_path / "selection")
    assert read(tmp_path / "selection/candidate.json") == CANDIDATE
    assert outputs[-1]["mode"] == "retained"


@pytest.mark.parametrize("change", ["owner", "abandoned", "overtaken"])
def test_reservation_rechecks_state_changed_after_preflight(history, monkeypatch, tmp_path, change):
    check()
    candidate = CANDIDATE | ({"attemptId": "456"} if change == "owner" else {})
    history.catalogue = reserve(history.catalogue, candidate, AT)
    if change == "abandoned":
        history.catalogue = abandon(history.catalogue, "shell/0.1.0", "expired", LATER)
    elif change == "overtaken":
        newer = CANDIDATE | {"version": "0.2.0", "attemptId": "456"}
        history.catalogue = complete(
            reserve(history.catalogue, newer, LATER), newer, CANDIDATE["imageId"], LATER
        )
    before = copy.deepcopy(history.catalogue)

    def append(self, transform, **kwargs):
        history.catalogue = transform(copy.deepcopy(history.catalogue))
        pytest.fail("must reject before committing history")

    monkeypatch.setattr(publisher.History, "append", append)
    monkeypatch.setattr(publisher, "selected", lambda directory: CANDIDATE)
    monkeypatch.setattr(publisher, "context", lambda: (CANDIDATE["sourceCommit"], "123", "1"))
    with pytest.raises(ValueError, match="owner|retired|overtaken"):
        publisher.reserve_version(tmp_path)
    assert history.catalogue == before


@pytest.mark.parametrize("version", ["0.1.0", "0.2.0"])
def test_workflow_policy_gates_preparation_and_protected_approval(
    history, monkeypatch, tmp_path, version
):
    workflow = yaml.safe_load((ROOT / ".github/workflows/container-image-release.yml").read_text())
    jobs = workflow["jobs"]
    policy_step = next(
        step for step in jobs["policy"]["steps"] if "context()" in step.get("run", "")
    )
    code = policy_step["run"].split("<<'PY'\n", 1)[1].rsplit("\nPY", 1)[0]
    script = tmp_path / "policy.py"
    script.write_text(code, encoding="utf-8")
    monkeypatch.setattr(publisher, "context", lambda: (CANDIDATE["sourceCommit"], "123", "1"))
    for name, value in {
        "PROFILE": "shell",
        "VERSION": version,
        "OPERATION": "release",
        "BICEP_ROUTE": "upstream",
    }.items():
        monkeypatch.setenv(name, value)
    if version == "0.2.0":
        with pytest.raises(ValueError, match="shell:0.2.0:.*0.1.0"):
            runpy.run_path(str(script))
    else:
        runpy.run_path(str(script))
    assert jobs["prepare"]["needs"] == "policy"
    assert jobs["prepare"]["if"] == "inputs.operation == 'release'"
    assert jobs["approve"]["needs"] == "prepare"
    assert jobs["approve"]["if"] == "needs.prepare.outputs.mode != 'delivery'"
    assert jobs["approve"]["environment"]["name"] == "container-release"
    assert "environment" not in jobs["policy"]
    assert workflow["permissions"] == {"contents": "read", "actions": "read"}
    assert "permissions" not in jobs["policy"]
    assert jobs["reserve"]["concurrency"]["group"] == "container-security-catalogue"
    assert not jobs["reserve"]["concurrency"]["cancel-in-progress"]
