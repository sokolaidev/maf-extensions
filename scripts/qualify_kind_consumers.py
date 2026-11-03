"""Qualify existing published images on trusted main without rebuilding or publishing them."""

from __future__ import annotations

import argparse
import importlib.metadata
import os
import platform
import re
import subprocess
from pathlib import Path

import consumer_qualification as consumer
from container_release import REPOSITORY, ROOT, decode, now, read, write
from container_release_history import GitHub, History
from image_release_needed import release_evidence


def trusted_source(root: Path) -> str:
    """Only the dedicated workflow's main checkout can produce authoritative evidence."""
    source = consumer.git(root, "rev-parse", "HEAD")
    if (
        os.environ.get("GITHUB_REPOSITORY") != REPOSITORY
        or os.environ.get("GITHUB_REF") != "refs/heads/main"
        or os.environ.get("GITHUB_EVENT_NAME") not in {"push", "workflow_dispatch", "workflow_run"}
        or os.environ.get("GITHUB_SHA") != source
        or os.environ.get("GITHUB_WORKFLOW_REF")
        != (f"{REPOSITORY}/{consumer.WORKFLOW}@refs/heads/main")
    ):
        raise ValueError("Consumer evidence requires the trusted main workflow checkout")
    if consumer.git(root, "status", "--porcelain", "--untracked-files=no"):
        raise ValueError("Consumer evidence requires clean tracked inputs")
    return source


def selection(root: Path, before: str, requested: str) -> list[dict[str, str]]:
    """Select affected declared consumers with completed images; dispatch can refresh all."""
    snapshot = History().head()
    if snapshot is None:
        return []
    source = consumer.git(root, "rev-parse", "HEAD")
    if requested != "all" and requested not in consumer.BACKENDS:
        raise ValueError("Unknown requested profile")
    result = []
    for package, profiles in consumer.KIND_IMAGES.items():
        for profile in profiles:
            if requested not in {"all", profile}:
                continue
            name = snapshot.catalogue["current"].get(profile)
            if name is None:
                print(f"{profile}: no published image to requalify")
                continue
            record = snapshot.catalogue["releases"][name]
            for backend in consumer.BACKENDS[profile]:
                expected = consumer.inputs(root, source, package, profile, backend, record)
                if re.fullmatch(r"[0-9a-f]{40}", before) and before != "0" * 40:
                    try:
                        previous = consumer.inputs(root, before, package, profile, backend, record)
                    except (ValueError, KeyError, subprocess.SubprocessError):
                        previous = None
                    if previous == expected:
                        continue
                result.append({"package": package, "profile": profile, "backend": backend})
    return result


def qualify(root: Path, package: str, profile: str, backend: str, output: Path) -> None:
    """Authenticate published bytes before executing the profile's existing runtime checks."""
    from check_container_release_image import check
    from check_kind_release_images import qualified
    from container_release_registry import anonymous_pull

    if backend != "docker":
        raise ValueError("No qualification executor for this backend")
    source = trusted_source(root)
    client = GitHub()
    snapshot = History(client).head()
    if snapshot is None or profile not in snapshot.catalogue["current"]:
        raise ValueError("No completed image is available for consumer qualification")
    record = snapshot.catalogue["releases"][snapshot.catalogue["current"][profile]]
    changed = consumer.image_changes(root, record["sourceCommit"], source, profile)
    if changed:
        raise ValueError(
            "Publish matching image inputs before qualification: " + ", ".join(changed)
        )
    expected = consumer.inputs(root, source, package, profile, backend, record)
    evidence = output / "image-release"
    evidence.mkdir(parents=True)
    release_url = release_evidence(client, record, evidence)
    qualified(evidence, record["candidate"])
    image_id = anonymous_pull(record["candidate"])
    runtime = check(profile, image_id, evidence)
    distributions = sorted(
        {(d.metadata["Name"], d.version) for d in importlib.metadata.distributions()}
    )
    docker = decode(
        subprocess.check_output(
            ["docker", "version", "--format", "{{json .Server}}"], encoding="utf-8", timeout=30
        )
    )
    if docker.get("Os") != "linux" or docker.get("Arch") != "amd64":
        raise ValueError("Consumer qualification requires a Linux/amd64 Docker engine")
    value = {
        "schemaVersion": 1,
        "inputs": expected,
        "sourceCommit": source,
        "runId": os.environ["GITHUB_RUN_ID"],
        "runAttempt": os.environ["GITHUB_RUN_ATTEMPT"],
        "checkedAt": now(),
        "anonymousPullVerified": True,
        "releaseEvidence": release_url,
        "environment": {
            "python": platform.python_version(),
            "docker": docker,
            "distributions": distributions,
        },
        "runtime": runtime,
    }
    consumer.validate_record(value, expected)
    trusted_source(root)
    write(output / consumer.SUBJECT, expected)
    write(output / "consumer-qualification.json", value)


def validate_for_signing(
    root: Path, directory: Path, package: str, profile: str, backend: str
) -> None:
    """Recompute the input subject on a fresh runner before granting it a signature."""
    source = trusted_source(root)
    value = read(directory / "consumer-qualification.json")
    subject = read(directory / consumer.SUBJECT)
    expected = consumer.inputs(root, source, package, profile, backend, subject)
    consumer.validate_record(value, expected)
    if (
        subject != expected
        or value["sourceCommit"] != source
        or value["runId"] != os.environ["GITHUB_RUN_ID"]
        or value["runAttempt"] != os.environ["GITHUB_RUN_ATTEMPT"]
    ):
        raise ValueError("Transferred consumer evidence differs from this workflow attempt")
    # Attestation lookup depends on canonical subject bytes on every platform.
    write(directory / consumer.SUBJECT, expected)


def main() -> None:
    """Select, execute or validate independent consumer qualification."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("select", "qualify", "validate"))
    parser.add_argument("--before", default="")
    parser.add_argument("--package", default="")
    parser.add_argument("--profile", default="all")
    parser.add_argument("--backend", default="docker")
    parser.add_argument("--directory", type=Path)
    args = parser.parse_args()
    if args.operation == "select":
        import json

        trusted_source(ROOT)
        matrix = selection(ROOT, args.before, args.profile)
        with Path(os.environ["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as output:
            output.write(f"matrix={json.dumps(matrix)}\n")
    else:
        if args.directory is None:
            parser.error("--directory is required")
        if args.operation == "qualify":
            qualify(ROOT, args.package, args.profile, args.backend, args.directory)
        else:
            validate_for_signing(ROOT, args.directory, args.package, args.profile, args.backend)


if __name__ == "__main__":
    main()
