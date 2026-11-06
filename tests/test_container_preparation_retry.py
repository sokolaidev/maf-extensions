"""Only failed, unreserved preparation can rebuild before the approval boundary."""

from __future__ import annotations

import copy
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import prepare_container_release_dispatch as dispatcher  # noqa: E402
from container_release import empty_catalogue, read, reserve, write  # noqa: E402
from container_release_history import GitHub, encode  # noqa: E402

pytestmark = pytest.mark.workflow
SOURCE = "a" * 40
CANDIDATE = {
    "profile": "bicep",
    "version": "0.1.0",
    "sourceCommit": SOURCE,
    "sourceRef": "refs/heads/main",
    "registryDigest": "sha256:" + "b" * 64,
    "assessedManifestDigest": "sha256:" + "b" * 64,
    "imageId": "sha256:" + "c" * 64,
    "attemptId": "123",
}


def job(attempt=1, **changes):
    return {
        "id": attempt,
        "run_id": 123,
        "head_sha": SOURCE,
        "run_attempt": attempt,
        "name": "prepare",
        "status": "completed",
        "conclusion": "failure",
    } | changes


@pytest.fixture
def retry(monkeypatch):
    state = {"jobs": [job()], "artifacts": [], "catalogue": empty_catalogue()}

    def request(self, endpoint):
        kind = "jobs" if "/jobs?filter=all&" in endpoint else "artifacts"
        values = state[kind]
        return encode({"total_count": len(values), kind: values})

    monkeypatch.setattr(GitHub, "request", request)
    monkeypatch.setattr(dispatcher, "context", lambda: (SOURCE, "123", "2"))
    monkeypatch.setattr(dispatcher, "preflight", lambda *args: state["catalogue"])
    return state


@pytest.mark.parametrize("conclusion", ["failure", "cancelled", "timed_out", "skipped"])
@pytest.mark.parametrize("plan_only", [False, True])
@pytest.mark.parametrize("expired", [False, True])
def test_failed_preparation_can_build_and_plan_installs_tools(
    monkeypatch, tmp_path, retry, conclusion, plan_only, expired
):
    retry["jobs"] = [job(conclusion=conclusion)]
    if expired:
        retry["artifacts"] = [{"name": "retained-image-123", "expired": True}]
    builds = []
    outputs = []

    def prepare(*args, **kwargs):
        builds.append((args, kwargs))
        return copy.deepcopy(CANDIDATE)

    monkeypatch.setattr(dispatcher, "prepare", prepare)
    monkeypatch.setattr(dispatcher, "outputs", outputs.append)
    dispatcher.dispatch(
        "bicep", "0.1.0", tmp_path / "build", tmp_path / "selection", plan_only=plan_only
    )
    assert outputs[-1]["mode"] == "built"
    assert all("lost" not in output for output in outputs)
    if plan_only:
        assert builds == []
        assert not (tmp_path / "selection").exists()
    else:
        assert builds == [
            (("bicep", "0.1.0", SOURCE, "123", tmp_path / "build"), {"bicep_route": "upstream"})
        ]
        assert read(tmp_path / "selection/candidate.json") == CANDIDATE


@pytest.mark.parametrize("plan_only", [False, True])
@pytest.mark.parametrize("earliest", ["success", "in_progress"])
def test_earlier_success_or_uncertainty_blocks_rebuild_even_after_later_failure(
    monkeypatch, tmp_path, retry, plan_only, earliest
):
    retry["jobs"] = [job(conclusion=earliest), job(2)]
    monkeypatch.setattr(dispatcher, "context", lambda: (SOURCE, "123", "3"))
    monkeypatch.setattr(dispatcher, "prepare", lambda *a, **kw: pytest.fail("must not rebuild"))
    with pytest.raises(ValueError, match="may have reached approval"):
        dispatcher.dispatch("bicep", "0.1.0", tmp_path, tmp_path, plan_only=plan_only)


@pytest.mark.parametrize(
    "changes",
    [
        {"head_sha": "d" * 40},
        {"run_id": 456},
        {"id": None},
        {"run_attempt": True},
        {"run_attempt": 3},
        {"name": "unrelated"},
        {"status": "in_progress"},
        {"conclusion": None},
    ],
)
def test_untrusted_or_incomplete_job_history_cannot_authorize_build(retry, changes):
    retry["jobs"] = [job(**changes)]
    with pytest.raises(ValueError):
        dispatcher.require_failed_preparations(SOURCE, "123", "2")


@pytest.mark.parametrize("jobs", [[], [job(), job()], [job(), job(id=2)]])
def test_missing_or_duplicate_preparation_is_rejected(retry, jobs):
    retry["jobs"] = jobs
    with pytest.raises(ValueError):
        dispatcher.require_failed_preparations(SOURCE, "123", "2")


@pytest.mark.parametrize("failure", ["timeout", "truncated", "malformed", "changed"])
def test_history_pagination_fails_closed(monkeypatch, failure):
    def request(self, endpoint):
        if failure == "timeout":
            raise TimeoutError("GitHub unavailable")
        if failure == "truncated":
            return encode({"total_count": 2, "jobs": [job()]})
        if failure == "malformed":
            return encode({"total_count": 1, "jobs": [None]})
        if endpoint.endswith("page=1"):
            return encode({"total_count": 101, "jobs": [job(id=n) for n in range(100)]})
        return encode({"total_count": 100, "jobs": []})

    monkeypatch.setattr(GitHub, "request", request)
    with pytest.raises((TimeoutError, ValueError)):
        dispatcher.require_failed_preparations(SOURCE, "123", "2")


def test_pagination_checks_older_success_on_later_page(monkeypatch):
    endpoints = []

    def request(self, endpoint):
        endpoints.append(endpoint)
        jobs = (
            [job(2, id=n, name="other") for n in range(100)]
            if endpoint.endswith("page=1")
            else [job(conclusion="success", id=100), job(2, id=101)]
        )
        return encode({"total_count": 102, "jobs": jobs})

    monkeypatch.setattr(GitHub, "request", request)
    with pytest.raises(ValueError, match="may have reached approval"):
        dispatcher.require_failed_preparations(SOURCE, "123", "3")
    assert len(endpoints) == 2


def test_retained_unreserved_candidate_is_recovered_without_build(monkeypatch, tmp_path, retry):
    retry["artifacts"] = [{"name": "retained-image-123", "expired": False}]
    retry["jobs"] = [job(conclusion="success")]
    write(tmp_path / "candidate.json", CANDIDATE)
    monkeypatch.setattr(dispatcher, "prepare", lambda *a, **kw: pytest.fail("must recover"))
    monkeypatch.setattr(dispatcher, "run", lambda *a, **kw: None)
    monkeypatch.setattr(dispatcher, "validate_preparation", lambda *a: None)
    monkeypatch.setattr(dispatcher, "validate_layout", lambda *a: {"imageId": CANDIDATE["imageId"]})
    outputs = []
    monkeypatch.setattr(dispatcher, "outputs", outputs.append)
    dispatcher.dispatch("bicep", "0.1.0", tmp_path, tmp_path / "selection")
    assert outputs[-1]["mode"] == "retained"


def test_reservation_appearing_after_plan_prevents_build(monkeypatch, tmp_path, retry):
    outputs = []
    monkeypatch.setattr(dispatcher, "outputs", outputs.append)
    dispatcher.dispatch("bicep", "0.1.0", tmp_path, tmp_path, plan_only=True)
    retry["catalogue"] = reserve(empty_catalogue(), CANDIDATE, "2026-01-01T00:00:00Z")
    monkeypatch.setattr(dispatcher, "prepare", lambda *a, **kw: pytest.fail("must not rebuild"))
    with pytest.raises(ValueError, match="reserved"):
        dispatcher.dispatch("bicep", "0.1.0", tmp_path, tmp_path)
    assert outputs == [{"mode": "built"}, {"lost": "true"}]


def test_artifact_loss_after_recovery_plan_cannot_build_without_tools(monkeypatch, tmp_path, retry):
    retry["artifacts"] = [{"name": "retained-image-123", "expired": False}]
    outputs = []
    monkeypatch.setattr(dispatcher, "outputs", outputs.append)
    dispatcher.dispatch("bicep", "0.1.0", tmp_path, tmp_path, plan_only=True)
    assert outputs == [{"mode": "retained"}]
    retry["artifacts"] = []
    monkeypatch.setattr(dispatcher, "prepare", lambda *a, **kw: pytest.fail("tools not installed"))
    with pytest.raises(ValueError, match="mode changed"):
        dispatcher.dispatch("bicep", "0.1.0", tmp_path, tmp_path, expected_mode="retained")


def test_failed_rebuild_does_not_emit_candidate_selection(monkeypatch, tmp_path, retry):
    def prepare(*args, **kwargs):
        raise ValueError("Critical assessment failed")

    outputs = []
    monkeypatch.setattr(dispatcher, "outputs", outputs.append)
    monkeypatch.setattr(dispatcher, "prepare", prepare)
    with pytest.raises(ValueError, match="Critical"):
        dispatcher.dispatch("bicep", "0.1.0", tmp_path, tmp_path / "selection")
    assert outputs == []
    assert not (tmp_path / "selection").exists()


def test_workflow_build_setup_and_approval_follow_preparation_result():
    workflow = yaml.safe_load((ROOT / ".github/workflows/container-image-release.yml").read_text())
    jobs = workflow["jobs"]
    steps = jobs["prepare"]["steps"]
    plan = next(s for s in steps if s.get("id") == "plan")
    assert "--plan-only" in plan["run"]
    candidate = next(s for s in steps if s.get("id") == "candidate")
    assert candidate["env"]["PREPARATION_MODE"] == "${{ steps.plan.outputs.mode }}"
    assert '--expected-mode "$PREPARATION_MODE"' in candidate["run"]
    retained = next(s for s in steps if s.get("name") == "Retain the original OCI candidate")
    assert retained["if"] == "steps.candidate.outputs.mode == 'built'"
    assert retained["with"]["overwrite"] is True
    for name in (
        "Prepare Docker Sandboxes consumer host",
        "Make room for the AVM provider toolchain and build cache",
        "Install preparation tools for a new candidate",
        "Prepare reusable provider packages",
    ):
        setup = next(s for s in steps if s.get("name") == name)
        assert "steps.plan.outputs.mode == 'built'" in setup["if"]
        assert "run_attempt" not in setup["if"]
        assert steps.index(plan) < steps.index(setup)
    assert jobs["prepare"]["outputs"]["lost"] == (
        "${{ steps.candidate.outputs.lost || steps.plan.outputs.lost }}"
    )
    assert jobs["approve"]["needs"] == "prepare"
    assert jobs["approve"]["if"] == "needs.prepare.outputs.mode != 'delivery'"
    assert jobs["approve"]["environment"]["name"] == "container-release"
