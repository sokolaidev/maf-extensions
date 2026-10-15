"""A valid release identity must not imply a fresh or successful monitoring assessment."""

from __future__ import annotations

import copy
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from container_release import complete, empty_catalogue, observe, reserve  # noqa: E402
from container_release_history import GitHub, encode  # noqa: E402
from container_release_status import latest_monitor, report  # noqa: E402

AT = "2026-01-01T00:00:00Z"
DIGEST = "sha256:" + "b" * 64
CANDIDATE = {
    "profile": "bicep",
    "version": "0.1.0",
    "sourceCommit": "a" * 40,
    "sourceRef": "refs/heads/main",
    "registryDigest": DIGEST,
    "assessedManifestDigest": DIGEST,
    "imageId": "sha256:" + "c" * 64,
    "attemptId": "123",
}


def run(run_id=200, attempt=1, updated=AT):
    return {
        "id": run_id,
        "run_attempt": attempt,
        "updated_at": updated,
        "status": "completed",
        "conclusion": "success",
        "event": "schedule",
        "head_branch": "main",
        "path": ".github/workflows/container-image-monitor.yml",
        "head_repository": {"full_name": "sokolaidev/maf-extensions"},
    }


@pytest.fixture
def current(monkeypatch):
    assessment = {
        "digest": DIGEST,
        "outcome": "clean",
        "assessedAt": AT,
        "monitorRunId": "200",
        "monitorRunAttempt": 1,
    }
    catalogue = observe(
        complete(reserve(empty_catalogue(), CANDIDATE, AT), CANDIDATE, DIGEST, AT),
        "bicep/0.1.0",
        assessment,
    )
    snapshot = SimpleNamespace(catalogue=catalogue, number=5, digest=DIGEST)
    state = {"runs": [run()], "snapshot": snapshot, "jobs": []}

    def request(_self, _endpoint, **_kwargs):
        if "/jobs?" in _endpoint:
            assert "/runs/" in _endpoint and "/attempts/" in _endpoint
            return encode({"jobs": state["jobs"], "total_count": len(state["jobs"])})
        return encode({"workflow_runs": state["runs"], "total_count": len(state["runs"])})

    monkeypatch.setattr("container_release_status.GitHub.request", request)
    monkeypatch.setattr("container_release_status.History.head", lambda _self: state["snapshot"])
    return state


@pytest.mark.parametrize("outcome", ["clean", "vulnerable", "unavailable", "running"])
def test_status_is_separate_from_release_verification(current, outcome):
    current["snapshot"].catalogue["releases"]["bicep/0.1.0"]["latestAttempt"]["outcome"] = outcome
    result = report(CANDIDATE, at=AT)
    assert result["monitoringStatus"] == ("unavailable" if outcome == "running" else outcome)
    assert "releaseIdentityVerified" not in result


def test_status_ages_without_another_monitor_run(current):
    assert report(CANDIDATE, at="2026-01-02T23:59:59Z")["monitoringStatus"] == "clean"
    assert report(CANDIDATE, at="2026-01-03T00:00:00Z")["monitoringStatus"] == "stale"


@pytest.mark.parametrize(
    "status,conclusion",
    [
        ("completed", "failure"),
        ("completed", "cancelled"),
        ("completed", "timed_out"),
        ("in_progress", None),
        ("queued", None),
    ],
)
def test_new_failed_monitor_cannot_hide_behind_old_clean_snapshot(current, status, conclusion):
    current["runs"].insert(
        0,
        run(201, updated="2026-01-01T01:00:00Z")
        | {
            "status": status,
            "conclusion": conclusion,
        },
    )
    result = report(CANDIDATE, at="2026-01-01T01:01:00Z")
    assert result["monitoringStatus"] == "unavailable"
    assert result["latestAttempt"]["outcome"] == "clean"


def test_rerun_of_old_workflow_is_not_hidden_by_creation_order(current):
    current["runs"].append(run(190, 2, "2026-01-01T01:00:00Z"))
    assert report(CANDIDATE, at="2026-01-01T01:01:00Z")["monitoringStatus"] == "unavailable"


def test_same_second_old_rerun_cannot_hide_behind_newer_run_id(current):
    current["runs"].append(run(190, 2) | {"conclusion": "failure"})
    assert report(CANDIDATE, at=AT)["monitoringStatus"] == "unavailable"


def test_same_run_new_attempt_requires_new_monitor_record(current):
    current["runs"][0]["run_attempt"] = 2
    assert report(CANDIDATE, at=AT)["monitoringStatus"] == "unavailable"


def test_wrong_digest_is_unavailable_even_with_otherwise_clean_evidence(current):
    current["snapshot"].catalogue["releases"]["bicep/0.1.0"]["registryDigest"] = (
        "sha256:" + "d" * 64
    )
    assert report(CANDIDATE, at=AT)["monitoringStatus"] == "unavailable"


def test_current_monitor_retrieval_failure_does_not_affect_identity_result(current, monkeypatch):
    def failed(_client):
        raise OSError("offline")

    monkeypatch.setattr("container_release_status.latest_monitor", failed)
    result = {"releaseIdentityVerified": True} | report(CANDIDATE, at=AT)
    assert result["releaseIdentityVerified"] is True
    assert result["monitoringStatus"] == "unavailable"


def test_monitor_changes_during_read_report_unavailable(current, monkeypatch):
    values = iter([run(), run(201)])
    monkeypatch.setattr("container_release_status.latest_monitor", lambda _: next(values))
    assert report(CANDIDATE, at=AT)["monitoringStatus"] == "unavailable"


def test_known_findings_remain_visible_after_new_failure(current):
    record = current["snapshot"].catalogue["releases"]["bicep/0.1.0"]
    record["lastKnownVulnerable"] = copy.deepcopy(record["latestAttempt"]) | {
        "outcome": "vulnerable",
        "findings": [{"id": "CVE-example", "severity": "High"}],
    }
    current["runs"][0]["conclusion"] = "failure"
    result = report(CANDIDATE, at=AT)
    assert result["monitoringStatus"] == "unavailable"
    assert result["lastKnownVulnerable"]["findings"][0]["id"] == "CVE-example"


def test_vulnerability_is_visible_even_when_its_workflow_fails(current):
    current["snapshot"].catalogue["releases"]["bicep/0.1.0"]["latestAttempt"]["outcome"] = (
        "vulnerable"
    )
    current["runs"][0]["conclusion"] = "failure"
    assert report(CANDIDATE, at=AT)["monitoringStatus"] == "vulnerable"


def test_monitor_lists_all_pages_when_locating_old_rerun():
    class Pages:
        def request(self, endpoint):
            records = (
                [run(300 + i) for i in range(100)]
                if endpoint.endswith("&page=1")
                else [run(100, 2, "2026-01-02T00:00:00Z")]
            )
            return encode({"workflow_runs": records, "total_count": 101})

    assert latest_monitor(Pages())["id"] == 100


def delivered_jobs():
    return [
        {
            "id": n,
            "run_id": 200,
            "run_attempt": 1,
            "name": name,
            "status": "completed",
            "conclusion": "success",
        }
        for n, name in enumerate(("begin", "finish"), 1)
    ]


@pytest.mark.parametrize("outcome", ["clean", "vulnerable", "unavailable", "running"])
def test_failed_aggregate_preserves_each_delivered_assessment(current, outcome):
    current["runs"][0]["conclusion"] = "failure"
    current["jobs"] = delivered_jobs()
    current["snapshot"].catalogue["releases"]["bicep/0.1.0"]["latestAttempt"]["outcome"] = outcome
    result = report(CANDIDATE, at=AT)
    assert result["monitoringStatus"] == ("unavailable" if outcome == "running" else outcome)
    assert result["monitorWorkflow"]["stateDeliveryVerified"] is True
    if outcome == "clean":
        assert report(CANDIDATE, at="2026-01-03T00:00:00Z")["monitoringStatus"] == "stale"


@pytest.mark.parametrize(
    "change", ["missing", "duplicate", "failed", "pending", "run", "attempt", "id"]
)
def test_failed_aggregate_requires_unambiguous_delivery_from_exact_attempt(current, change):
    current["runs"][0]["conclusion"] = "failure"
    jobs = delivered_jobs()
    if change == "missing":
        jobs.pop()
    elif change == "duplicate":
        jobs.append(jobs[-1] | {"id": 3})
    elif change == "failed":
        jobs[-1]["conclusion"] = "failure"
    elif change == "pending":
        jobs[-1]["status"] = "in_progress"
    elif change == "run":
        jobs[-1]["run_id"] = 199
    elif change == "attempt":
        jobs[-1]["run_attempt"] = 2
    else:
        jobs[-1]["id"] = jobs[0]["id"]
    current["jobs"] = jobs
    assert report(CANDIDATE, at=AT)["monitoringStatus"] == "unavailable"


def test_delivery_retrieval_failure_preserves_vulnerability_evidence(current, monkeypatch):
    current["runs"][0]["conclusion"] = "failure"

    def failed(*args):
        raise OSError("offline")

    monkeypatch.setattr("container_release_status.state_delivery", failed)
    assert report(CANDIDATE, at=AT)["monitoringStatus"] == "unavailable"
    current["snapshot"].catalogue["releases"]["bicep/0.1.0"]["latestAttempt"]["outcome"] = (
        "vulnerable"
    )
    assert report(CANDIDATE, at=AT)["monitoringStatus"] == "vulnerable"


def test_delivery_listing_requires_all_pages_and_refuses_truncation(monkeypatch):
    from container_release_status import state_delivery

    jobs = [
        {
            "id": n,
            "run_id": 200,
            "run_attempt": 1,
            "name": "scan",
            "status": "completed",
            "conclusion": "success",
        }
        for n in range(3, 103)
    ]
    jobs += delivered_jobs()

    def request(self, endpoint):
        assert "/runs/200/attempts/1/jobs?" in endpoint
        page = int(endpoint.rsplit("=", 1)[1])
        return encode({"total_count": 102, "jobs": jobs[(page - 1) * 100 : page * 100]})

    monkeypatch.setattr(GitHub, "request", request)
    assert state_delivery(GitHub(), run()) is True
    jobs.pop()
    assert state_delivery(GitHub(), run()) is False
