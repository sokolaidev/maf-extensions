"""Recovery accepts only retained evidence from an approved original publisher run."""

from __future__ import annotations

import copy
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import recover_container_release as recovery  # noqa: E402
from container_release import digest, write  # noqa: E402
from container_release_history import encode  # noqa: E402

SOURCE = "a" * 40
CANDIDATE = {
    "profile": "diagram",
    "version": "0.1.0",
    "sourceCommit": SOURCE,
    "sourceRef": "refs/heads/main",
    "registryDigest": "sha256:" + "b" * 64,
    "assessedManifestDigest": "sha256:" + "b" * 64,
    "imageId": "sha256:" + "c" * 64,
    "attemptId": "123",
}


@pytest.fixture
def original(monkeypatch):
    repo = {"full_name": "sokolaidev/maf-extensions", "id": 42}
    run = {
        "id": 123,
        "repository": repo,
        "head_repository": copy.deepcopy(repo),
        "path": ".github/workflows/container-image-release.yml",
        "event": "workflow_dispatch",
        "head_branch": "main",
        "head_sha": SOURCE,
        "status": "completed",
        "conclusion": "failure",
        "run_attempt": 2,
    }
    record = dict(
        CANDIDATE, candidate=copy.deepcopy(CANDIDATE), state="incomplete", delivery="pending"
    )
    job = {
        "name": "qualify",
        "conclusion": "success",
        "started_at": "2026-01-01T00:00:00Z",
        "completed_at": "2026-01-01T00:01:00Z",
    }
    artifact = {
        "name": "qualified-image-evidence",
        "id": 456,
        "expired": False,
        "created_at": "2026-01-01T00:00:30Z",
        "updated_at": "2026-01-01T00:00:31Z",
        "workflow_run": {
            "id": 123,
            "head_sha": SOURCE,
            "head_branch": "main",
            "repository_id": 42,
            "head_repository_id": 42,
        },
    }
    responses = {
        "": run,
        "/approvals": [{"state": "approved", "environments": [{"name": "container-release"}]}],
        "/jobs?filter=latest&per_page=100": {
            "total_count": 2,
            "jobs": [job, {"name": "approve", "conclusion": "success"}],
        },
        "/artifacts?name=qualified-image-evidence&per_page=100": {
            "total_count": 1,
            "artifacts": [artifact],
        },
    }

    def request(self, endpoint):
        return encode(
            responses[endpoint.removeprefix("repos/sokolaidev/maf-extensions/actions/runs/123")]
        )

    monkeypatch.setattr(recovery.GitHub, "request", request)
    monkeypatch.setattr(
        recovery,
        "History",
        lambda _github: SimpleNamespace(
            head=lambda: SimpleNamespace(catalogue={"releases": {"diagram/0.1.0": record}})
        ),
    )
    return SimpleNamespace(run=run, record=record, job=job, artifact=artifact, responses=responses)


def test_original_selection_binds_the_retained_artifact_to_the_reservation(original):
    result = recovery.selection("123", "complete")
    assert result == {
        "runId": "123",
        "runAttempt": 2,
        "stage": "complete",
        "artifactId": 456,
        "candidate": CANDIDATE,
    }


@pytest.mark.parametrize(
    "field,value",
    [
        ("path", ".github/workflows/other.yml"),
        ("event", "pull_request"),
        ("head_branch", "feature"),
        ("head_sha", "d" * 40),
        ("status", "in_progress"),
        ("conclusion", "success"),
    ],
)
def test_untrusted_or_changing_original_run_is_refused(original, field, value):
    original.run[field] = value
    with pytest.raises(ValueError):
        recovery.selection("123", "complete")


@pytest.mark.parametrize(
    "change",
    [
        lambda o: o.run["head_repository"].update(full_name="fork/repo"),
        lambda o: o.responses["/jobs?filter=latest&per_page=100"]["jobs"][1].update(
            conclusion="failure"
        ),
        lambda o: o.job.update(conclusion="failure"),
        lambda o: o.artifact.update(expired=True),
        lambda o: o.artifact["workflow_run"].update(id=999),
        lambda o: o.artifact["workflow_run"].update(head_sha="e" * 40),
        lambda o: o.artifact["workflow_run"].update(head_repository_id=99),
        lambda o: o.artifact.update(updated_at="2026-01-01T00:02:00Z"),
        lambda o: o.responses["/jobs?filter=latest&per_page=100"].update(total_count=3),
        lambda o: o.responses["/artifacts?name=qualified-image-evidence&per_page=100"].update(
            total_count=2
        ),
        lambda o: o.record.update(state="abandoned"),
        lambda o: o.record.update(delivery="complete"),
        lambda o: o.record.update(unexpectedDigests={"unapproved": {}}),
    ],
)
def test_missing_approval_or_unbound_evidence_never_recovers(original, change):
    change(original)
    with pytest.raises(ValueError):
        recovery.selection("123", "complete")


def test_delivery_uses_original_signing_artifact_and_requires_committed_completion(original):
    original.artifact["name"] = "completion-delivery"
    original.job["name"] = "sign-completion"
    original.responses["/artifacts?name=completion-delivery&per_page=100"] = original.responses.pop(
        "/artifacts?name=qualified-image-evidence&per_page=100"
    )
    with pytest.raises(ValueError, match="Reservation"):
        recovery.selection("123", "deliver")
    original.record["state"] = "completed"
    assert recovery.selection("123", "deliver")["artifactId"] == 456


@pytest.mark.parametrize("stage", ["complete", "deliver"])
def test_recovery_records_new_writer_but_keeps_original_candidate(monkeypatch, tmp_path, stage):
    planned = {"runId": "123", "stage": stage, "candidate": CANDIDATE}
    monkeypatch.setattr(recovery, "selection", lambda *a: planned)
    monkeypatch.setattr(recovery, "context", lambda workflow: ("d" * 40, "789", "1"))
    write(tmp_path / "candidate.json", CANDIDATE)
    write(tmp_path / "assessment.json", {})
    write(
        tmp_path / "approval.json",
        {
            "candidate": CANDIDATE,
            "sameBytesRefreshAuthorized": True,
            "displayedAssessmentSha256": digest(tmp_path / "assessment.json"),
            "reviews": [{"state": "approved", "environments": [{"name": "container-release"}]}],
        },
    )
    calls = []

    def writer(directory, candidate, **kwargs):
        calls.append((directory, candidate, kwargs))

    monkeypatch.setattr(recovery, "complete_candidate", writer)
    monkeypatch.setattr(recovery, "deliver_candidate", writer)
    recovery.recover(planned, tmp_path)
    assert calls == [(tmp_path, CANDIDATE, {"source": "d" * 40, "run_id": "789", "attempt": "1"})]


@pytest.mark.parametrize("drift", ["selection", "candidate"])
def test_recovery_rechecks_selection_and_downloaded_bytes_before_writing(
    monkeypatch, tmp_path, drift
):
    planned = {"runId": "123", "stage": "complete", "candidate": CANDIDATE}
    monkeypatch.setattr(
        recovery,
        "selection",
        lambda *a: planned | ({"runAttempt": 3} if drift == "selection" else {}),
    )
    monkeypatch.setattr(recovery, "context", lambda workflow: ("d" * 40, "789", "1"))
    write(
        tmp_path / "candidate.json",
        CANDIDATE | ({"sourceCommit": "e" * 40} if drift == "candidate" else {}),
    )
    monkeypatch.setattr(
        recovery, "complete_candidate", lambda *a, **k: pytest.fail("must not write")
    )
    with pytest.raises(ValueError, match="changed|differs"):
        recovery.recover(planned, tmp_path)


@pytest.mark.parametrize(
    "change",
    [
        lambda a: a.update(candidate=CANDIDATE | {"imageId": "sha256:" + "e" * 64}),
        lambda a: a.update(reviews=[]),
        lambda a: a.update(sameBytesRefreshAuthorized=False),
        lambda a: a.update(displayedAssessmentSha256="sha256:" + "f" * 64),
    ],
)
def test_retained_approval_must_bind_candidate_and_displayed_assessment(
    monkeypatch, tmp_path, change
):
    planned = {"runId": "123", "stage": "complete", "candidate": CANDIDATE}
    monkeypatch.setattr(recovery, "selection", lambda *a: planned)
    monkeypatch.setattr(recovery, "context", lambda workflow: ("d" * 40, "789", "1"))
    monkeypatch.setattr(
        recovery, "complete_candidate", lambda *a, **k: pytest.fail("must not write")
    )
    write(tmp_path / "candidate.json", CANDIDATE)
    write(tmp_path / "assessment.json", {})
    approval = {
        "candidate": CANDIDATE,
        "sameBytesRefreshAuthorized": True,
        "displayedAssessmentSha256": digest(tmp_path / "assessment.json"),
        "reviews": [{"state": "approved", "environments": [{"name": "container-release"}]}],
    }
    change(approval)
    write(tmp_path / "approval.json", approval)
    with pytest.raises(ValueError, match="Retained protected approval"):
        recovery.recover(planned, tmp_path)
