"""Retry failed preparation or recover retained bytes without replacing an approved candidate."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from build_bicep_image import PROFILES as BICEP_PROFILES
from build_bicep_image import ROUTES, validate_route
from container_release import (
    REPOSITORY,
    decode,
    digest,
    profiles,
    read,
    validate_identity,
    version,
    write,
)
from container_release_history import GitHub
from container_release_oci import validate_layout
from container_release_preflight import preflight
from container_release_publish import context, outputs
from container_release_registry import validate_preparation
from prepare_container_release import prepare, run


def retained_available(run_id: str) -> bool:
    """Distinguish confirmed artifact loss from a transient or unauthorized GitHub response."""
    matches: list[dict[str, Any]] = []
    received = 0
    for page in range(1, 101):
        result = decode(
            GitHub().request(
                f"repos/{REPOSITORY}/actions/runs/{run_id}/artifacts?per_page=100&page={page}"
            )
        )
        artifacts = result.get("artifacts")
        if not isinstance(artifacts, list) or type(result.get("total_count")) is not int:
            raise ValueError("Incomplete artifact listing")
        received += len(artifacts)
        matches.extend(a for a in artifacts if a.get("name") == f"retained-image-{run_id}")
        if len(artifacts) < 100:
            if received != result["total_count"]:
                raise ValueError("Artifact listing changed during retrieval")
            break
    else:
        raise ValueError("Artifact listing exceeded its bound")
    if len(matches) > 1:
        raise ValueError("Ambiguous retained image artifact")
    if not matches:
        return False
    if type(matches[0].get("expired")) is not bool:
        raise ValueError("Artifact expiry could not be established")
    return not matches[0]["expired"]


def require_failed_preparations(source: str, run_id: str, attempt: str) -> None:
    """Approval requires successful preparation; prove no earlier attempt reached that boundary."""
    jobs: list[dict[str, Any]] = []
    total = None
    for page in range(1, 101):
        result = decode(
            GitHub().request(
                f"repos/{REPOSITORY}/actions/runs/{run_id}/jobs?filter=all&per_page=100&page={page}"
            )
        )
        batch = result.get("jobs")
        count = result.get("total_count")
        if (
            not isinstance(batch, list)
            or any(not isinstance(job, dict) for job in batch)
            or type(count) is not int
            or (total is not None and count != total)
        ):
            raise ValueError("Incomplete preparation job history")
        total = count
        jobs.extend(batch)
        if len(batch) < 100:
            if len(jobs) != total:
                raise ValueError("Preparation job history changed during retrieval")
            break
    else:
        raise ValueError("Preparation job history exceeded its bound")
    seen: set[int] = set()
    preparations: set[int] = set()
    for job in jobs:
        number = job.get("run_attempt")
        job_id = job.get("id")
        if (
            type(job_id) is not int
            or job_id in seen
            or job.get("run_id") != int(run_id)
            or job.get("head_sha") != source
            or type(number) is not int
            or not 1 <= number <= int(attempt)
        ):
            raise ValueError("Invalid preparation job history identity")
        seen.add(job_id)
        if job.get("name") != "prepare" or number == int(attempt):
            continue
        if (
            number in preparations
            or job.get("status") != "completed"
            or job.get("conclusion") not in {"failure", "cancelled", "timed_out", "skipped"}
        ):
            raise ValueError(
                "An earlier preparation may have reached approval; cannot rebuild its candidate. "
                "Start a new workflow dispatch instead."
            )
        preparations.add(number)
    if preparations != set(range(1, int(attempt))):
        raise ValueError("Incomplete preparation attempt history; cannot authorize a rebuild")


def dispatch(
    profile: str,
    release_version: str,
    directory: Path,
    selection: Path,
    *,
    bicep_route: str = "upstream",
    plan_only: bool = False,
    expected_mode: str | None = None,
) -> None:
    """A completed release can recover delivery even after its temporary OCI artifact expires."""
    source, run_id, attempt = context()
    if profile not in profiles():
        raise ValueError("Unknown release profile")
    version(release_version)
    validate_route(bicep_route, profile)
    catalogue = preflight(profile, release_version, source, run_id)
    record = catalogue["releases"].get(f"{profile}/{release_version}")
    if record and record["state"] == "completed":
        mode = "delivery"
    elif (attempt != "1" or record) and retained_available(run_id):
        mode = "retained"
    elif record:
        outputs({"lost": "true"})
        raise ValueError(
            f"Retained candidate is unavailable for reserved {profile}:{release_version}; "
            "a rebuild requires a new dispatch and unused version. "
            "The reserved version cannot be reused."
        )
    else:
        if attempt != "1":
            require_failed_preparations(source, run_id, attempt)
        mode = "built"
    if expected_mode is not None and mode != expected_mode:
        raise ValueError("Preparation mode changed after tool setup; retry with a fresh plan")
    if plan_only:
        outputs({"mode": mode})
        return
    if mode == "delivery" and record is not None:
        candidate = record["candidate"]
    elif mode == "retained":
        run(
            [
                "gh",
                "run",
                "download",
                run_id,
                "--repo",
                REPOSITORY,
                "--name",
                f"retained-image-{run_id}",
                "--dir",
                str(directory),
            ],
            capture_output=True,
        )
        candidate = read(directory / "candidate.json")
        validate_identity(candidate)
        validate_preparation(directory, candidate)
        identity = validate_layout(directory / "oci", candidate["imageId"])
        if any(candidate[field] != value for field, value in identity.items()):
            raise ValueError("Recovered OCI bytes differ from the retained identity")
    else:
        candidate = prepare(
            profile, release_version, source, run_id, directory, bicep_route=bicep_route
        )
    if (
        candidate["profile"] != profile
        or candidate["version"] != release_version
        or candidate["sourceCommit"] != source
        or candidate["attemptId"] != run_id
        or (record and candidate != record["candidate"])
        or (profile in BICEP_PROFILES and candidate.get("bicepRoute", "upstream") != bicep_route)
    ):
        raise ValueError("Recovered candidate differs from this dispatch's reservation")
    write(selection / "candidate.json", candidate)
    outputs(
        {
            "mode": mode,
            "candidate_sha256": digest(selection / "candidate.json"),
            "digest": candidate["registryDigest"],
            "image_id": candidate["imageId"],
        }
    )


def main() -> None:
    """Plan or execute preparation under the same release and retry policy."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True, choices=profiles())
    parser.add_argument("--version", required=True)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--selection", type=Path, required=True)
    parser.add_argument("--bicep-route", choices=ROUTES, default="upstream")
    parser.add_argument("--plan-only", action="store_true")
    parser.add_argument("--expected-mode", choices=("built", "retained", "delivery"))
    args = parser.parse_args()
    dispatch(
        args.profile,
        args.version,
        args.directory,
        args.selection,
        bicep_route=args.bicep_route,
        plan_only=args.plan_only,
        expected_mode=args.expected_mode,
    )


if __name__ == "__main__":
    main()
