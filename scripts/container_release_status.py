"""Report current image monitoring separately from immutable release verification."""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path
from typing import Any

from container_release import (
    REPOSITORY,
    assessment_status,
    decode,
    key,
    monitoring_end,
    now,
    read,
    timestamp,
    validate_identity,
)
from container_release_history import GitHub, History, positive_id

MONITOR = "container-image-monitor.yml"


def latest_monitor(github: GitHub) -> dict[str, Any]:
    """Include reruns of old workflows; creation order alone does not identify the latest attempt."""
    latest: dict[str, Any] | None = None
    latest_order: tuple[Any, int, int] | None = None
    latest_attempts: set[tuple[int, int]] = set()
    received = 0
    for page in range(1, 10_001):
        document = decode(
            github.request(
                f"repos/{REPOSITORY}/actions/workflows/{MONITOR}/runs?per_page=100&page={page}"
            )
        )
        runs = document.get("workflow_runs")
        if not isinstance(runs, list) or any(not isinstance(run, dict) for run in runs):
            raise ValueError("Invalid monitor workflow listing")
        total = document.get("total_count")
        if type(total) is not int or total < 0:
            raise ValueError("Monitor listing has no complete result count")
        received += len(runs)
        for run in runs:
            if (
                run.get("event") not in {"schedule", "workflow_dispatch", "workflow_run"}
                or run.get("head_branch") != "main"
            ):
                continue
            if (
                run.get("path") != f".github/workflows/{MONITOR}"
                or run.get("head_repository", {}).get("full_name") != REPOSITORY
            ):
                raise ValueError("Monitor run has an unexpected source")
            order = (
                timestamp(run.get("updated_at")),
                positive_id(run.get("id")),
                positive_id(run.get("run_attempt")),
            )
            if latest_order is None or order[0] > latest_order[0]:
                latest_attempts = {order[1:]}
            elif order[0] == latest_order[0]:
                latest_attempts.add(order[1:])
            if latest_order is None or order > latest_order:
                latest_order, latest = order, run
        if len(runs) < 100:
            if received != total:
                raise ValueError("Monitor listing is truncated or changed during retrieval")
            break
    else:
        raise ValueError("Monitor history exceeded its pagination bound")
    if latest is None:
        raise ValueError("No authoritative monitoring attempt exists")
    if len(latest_attempts) != 1:
        raise ValueError("Latest monitor attempts have ambiguous update times")
    return {
        field: latest.get(field)
        for field in ("id", "run_attempt", "status", "conclusion", "updated_at")
    }


def report(
    expected: dict[str, Any],
    *,
    at: str | None = None,
    github: GitHub | None = None,
) -> dict[str, Any]:
    """Fail status retrieval closed while leaving the caller's release-identity result untouched."""
    validate_identity(expected)
    checked_at = at or now()
    timestamp(checked_at)
    result: dict[str, Any] = {
        "monitoringStatus": "unavailable",
        "digest": expected["registryDigest"],
        "checkedAt": checked_at,
        "authoritativeEndpoint": f"https://api.github.com/repos/{REPOSITORY}/releases",
    }
    client = github or GitHub()
    try:
        first = latest_monitor(client)
        snapshot = History(client).head()
        if snapshot is None:
            raise ValueError("No committed release catalogue exists")
        record = snapshot.catalogue["releases"].get(key(expected))
        if not isinstance(record, dict) or any(
            record.get(field) != expected[field]
            for field in (
                "sourceCommit",
                "sourceRef",
                "registryDigest",
                "assessedManifestDigest",
                "imageId",
            )
        ):
            raise ValueError("Monitoring record differs from the selected release")
        result.update(
            catalogueSequence=snapshot.number,
            catalogueSha256=snapshot.digest,
            publicationState=record["state"],
            latestAttempt=record.get("latestAttempt"),
            lastKnownVulnerable=record.get("lastKnownVulnerable"),
            unexpectedDigests=record.get("unexpectedDigests", {}),
            registryDiscovery=record.get("registryDiscovery"),
            publicationFailure="unexpected-registry-digest"
            if record.get("unexpectedDigests")
            else None,
        )
        end = monitoring_end(record)
        result["monitoringEndsAt"] = end.isoformat() if end else None
        second = latest_monitor(client)
        if first != second:
            raise ValueError("Monitor attempt changed during retrieval")
        if timestamp(second["updated_at"]) > timestamp(checked_at):
            raise ValueError("Monitor workflow is dated in the future")
        result["monitorWorkflow"] = second
        if end and timestamp(checked_at) >= end:
            result["monitoringStatus"] = "no-longer-monitored"
            return result
        attempt = record.get("latestAttempt", {})
        if (
            attempt.get("monitorRunId") != str(second["id"])
            or attempt.get("monitorRunAttempt") != second["run_attempt"]
        ):
            raise ValueError("Catalogue does not include the latest monitor workflow attempt")
        if attempt.get("outcome") == "vulnerable":
            result["monitoringStatus"] = "vulnerable"
            return result
        if second["status"] != "completed" or second["conclusion"] != "success":
            raise ValueError("Latest monitor workflow has no confirmed successful state delivery")
        result["monitoringStatus"] = assessment_status(record, checked_at)
    except (ValueError, KeyError, TypeError, OSError, subprocess.SubprocessError) as error:
        # Subprocess output can contain local paths; keep it outside public status records.
        result["reason"] = (
            str(error) if isinstance(error, ValueError) else "Monitoring evidence unavailable"
        )
    return result


def main() -> None:
    """Read current monitoring without turning vulnerability status into an execution gate."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--policy", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(report(read(args.policy))))


if __name__ == "__main__":
    main()
