"""Build once for a dispatch, or recover its retained bytes and completed delivery state."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

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
from container_release_history import GitHub, History
from container_release_oci import validate_layout
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


def dispatch(profile: str, release_version: str, directory: Path, selection: Path) -> None:
    """A completed release can recover delivery even after its temporary OCI artifact expires."""
    source, run_id, attempt = context()
    if profile not in profiles():
        raise ValueError("Unknown release profile")
    version(release_version)
    head = History().head()
    record = head.catalogue["releases"].get(f"{profile}/{release_version}") if head else None
    if record and (record["sourceCommit"] != source or record["attemptId"] != run_id):
        raise ValueError("This version belongs to another workflow run; select an unused version")
    if record and record["state"] == "abandoned":
        raise ValueError("This version is permanently retired")
    if record and record["state"] == "completed":
        candidate = record["candidate"]
        mode = "delivery"
    elif attempt != "1":
        if not retained_available(run_id):
            outputs({"lost": str(record is not None).lower()})
            raise ValueError(
                "Retained candidate is unavailable; a rebuild requires a new dispatch and unused version"
            )
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
        mode = "retained"
    else:
        candidate = prepare(profile, release_version, source, run_id, directory)
        mode = "built"
    if (
        candidate["profile"] != profile
        or candidate["version"] != release_version
        or candidate["sourceCommit"] != source
        or candidate["attemptId"] != run_id
        or (record and candidate != record["candidate"])
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
    """Prepare the reviewed profile and version without silently rebuilding a retry."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True, choices=profiles())
    parser.add_argument("--version", required=True)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--selection", type=Path, required=True)
    args = parser.parse_args()
    dispatch(args.profile, args.version, args.directory, args.selection)


if __name__ == "__main__":
    main()
