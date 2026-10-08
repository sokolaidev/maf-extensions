"""Recover release storage from authenticated artifacts without replacing the original signer."""

from __future__ import annotations

import argparse
import re
from pathlib import Path
from typing import Any

from container_release import REPOSITORY, digest, key, read, timestamp, validate_identity, write
from container_release_history import GitHub, History, decode, positive_id
from container_release_publish import complete_candidate, context, deliver_candidate, outputs

WORKFLOW = "container-image-recover.yml"
ARTIFACTS = {
    "complete": ("qualified-image-evidence", "qualify"),
    "deliver": ("completion-delivery", "sign-completion"),
}


def selection(run_id: str, stage: str) -> dict[str, Any]:
    """Bind one retained artifact to a failed protected-main run and its durable reservation."""
    if not re.fullmatch(r"[1-9][0-9]*", run_id) or stage not in ARTIFACTS:
        raise ValueError("Expected an original release run ID and a storage recovery stage")
    github = GitHub()
    base = f"repos/{REPOSITORY}/actions/runs/{run_id}"
    run = decode(github.request(base))
    if (
        run.get("id") != int(run_id)
        or run.get("repository", {}).get("full_name") != REPOSITORY
        or run.get("head_repository", {}).get("full_name") != REPOSITORY
        or run.get("path") != ".github/workflows/container-image-release.yml"
        or run.get("event") != "workflow_dispatch"
        or run.get("head_branch") != "main"
        or run.get("status") != "completed"
        or run.get("conclusion") != "failure"
    ):
        raise ValueError("Recovery requires a failed original protected-main release run")
    attempt = positive_id(run.get("run_attempt"))
    head = History(github).head()
    if head is None:
        raise ValueError("Recovery requires committed release history")
    records = [
        record
        for record in head.catalogue["releases"].values()
        if record.get("attemptId") == run_id
    ]
    if len(records) != 1:
        raise ValueError("Original run must identify one durable reservation")
    record = records[0]
    candidate = record.get("candidate")
    if not isinstance(candidate, dict):
        raise ValueError("Reservation does not retain its original candidate")
    validate_identity(candidate)
    if (
        candidate["sourceCommit"] != run.get("head_sha")
        or candidate["attemptId"] != run_id
        or record.get("state") not in {"incomplete", "completed"}
        or (stage == "deliver" and record["state"] != "completed")
        or record.get("delivery") == "complete"
        or record.get("unexpectedDigests")
    ):
        raise ValueError("Reservation does not permit this recovery")
    artifact_name, producer = ARTIFACTS[stage]
    jobs = decode(github.request(base + "/jobs?filter=latest&per_page=100"))
    if not isinstance(jobs.get("jobs"), list) or jobs.get("total_count") != len(jobs["jobs"]):
        raise ValueError("Original job listing is incomplete")
    approved = [job for job in jobs["jobs"] if job.get("name") == "approve"]
    if len(approved) != 1 or approved[0].get("conclusion") != "success":
        raise ValueError("Original protected approval job did not succeed")
    producers = [job for job in jobs["jobs"] if job.get("name") == producer]
    if len(producers) != 1 or producers[0].get("conclusion") != "success":
        raise ValueError("Required original evidence-producing job did not succeed")
    artifacts = decode(github.request(base + f"/artifacts?name={artifact_name}&per_page=100"))
    if artifacts.get("total_count") != 1 or len(artifacts.get("artifacts", [])) != 1:
        raise ValueError("Expected one retained original evidence artifact")
    artifact = artifacts["artifacts"][0]
    origin = artifact.get("workflow_run", {})
    if (
        artifact.get("name") != artifact_name
        or artifact.get("expired") is not False
        or origin.get("id") != int(run_id)
        or origin.get("head_sha") != candidate["sourceCommit"]
        or origin.get("head_branch") != "main"
        or origin.get("repository_id") != positive_id(run["repository"].get("id"))
        or origin.get("head_repository_id") != positive_id(run["head_repository"].get("id"))
        or not (
            timestamp(producers[0]["started_at"])
            <= timestamp(artifact["created_at"])
            <= timestamp(artifact["updated_at"])
            <= timestamp(producers[0]["completed_at"])
        )
    ):
        raise ValueError("Artifact does not belong to the successful original producer")
    return {
        "runId": run_id,
        "runAttempt": attempt,
        "stage": stage,
        "artifactId": positive_id(artifact.get("id")),
        "candidate": candidate,
    }


def recover(planned: dict[str, Any], directory: Path) -> None:
    """Revalidate the selected run and preserve source identity while committing storage."""
    source, run_id, attempt = context(WORKFLOW)
    if selection(planned["runId"], planned["stage"]) != planned:
        raise ValueError("Recovery selection changed before the write")
    candidate = read(directory / "candidate.json")
    if candidate != planned["candidate"]:
        raise ValueError("Downloaded candidate differs from the durable reservation")
    approval = read(directory / "approval.json")
    reviews = approval.get("reviews")
    displayed = directory / "approval-assessment.json"
    if not displayed.exists():
        displayed = directory / "assessment.json"
    if (
        approval.get("candidate") != candidate
        or approval.get("sameBytesRefreshAuthorized") is not True
        or approval.get("displayedAssessmentSha256") != digest(displayed)
        or not isinstance(reviews, list)
        or not any(
            review.get("state") == "approved"
            and any(
                env.get("name") == "container-release" for env in review.get("environments", [])
            )
            for review in reviews
        )
    ):
        raise ValueError("Retained protected approval does not match the selected candidate")
    writer = complete_candidate if planned["stage"] == "complete" else deliver_candidate
    writer(directory, candidate, source=source, run_id=run_id, attempt=attempt)


def main() -> None:
    """Inspect before protected approval, then write under the shared catalogue lock."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("inspect", "write"))
    parser.add_argument("--selection", type=Path, required=True)
    parser.add_argument("--run-id")
    parser.add_argument("--stage", choices=tuple(ARTIFACTS))
    parser.add_argument("--directory", type=Path)
    args = parser.parse_args()
    context(WORKFLOW)
    if args.operation == "inspect":
        if not args.run_id or not args.stage:
            parser.error("inspect requires --run-id and --stage")
        planned = selection(args.run_id, args.stage)
        write(args.selection, planned)
        outputs({"artifact_id": planned["artifactId"]})
        print(
            f"Original run {planned['runId']}, attempt {planned['runAttempt']}; source {planned['candidate']['sourceCommit']}"
        )
        print(
            f"Configuration: {planned['candidate']['imageId']}; retained artifact {planned['artifactId']}"
        )
        print(
            f"Recover {planned['stage']} for {key(planned['candidate'])}: {planned['candidate']['registryDigest']}"
        )
    else:
        if args.directory is None:
            parser.error("write requires --directory")
        recover(read(args.selection), args.directory)


if __name__ == "__main__":
    main()
