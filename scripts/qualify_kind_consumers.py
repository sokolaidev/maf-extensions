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


def verified_source_commit(root: Path) -> str:
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


def consumer_revision(root: Path, requested: str = "") -> str:
    """Restrict historical execution to merged commits with the same fingerprint policy."""
    signer = verified_source_commit(root)
    source = requested or signer
    if not re.fullmatch(r"[0-9a-f]{40}", source):
        raise ValueError("Consumer source must be a full immutable commit SHA")
    consumer.git(root, "merge-base", "--is-ancestor", source, signer)
    policy = (
        "scripts/consumer_qualification.py",
        "scripts/select_image_security.py",
        "scripts/build_wolfi_python.py",
        "scripts/container_release.py",
        "scripts/container_release_history.py",
        "scripts/classic_docker_store.py",
        ".github/actions/setup-classic-docker",
    )
    if consumer.git(root, "diff", "--name-only", source, signer, "--", *policy):
        raise ValueError(
            "Historical consumer fingerprint policy differs; prepare a replacement release"
        )
    return source


def checked_consumer(root: Path, checkout: Path, requested: str) -> str:
    """Bind a clean consumer checkout to the revision selected by the trusted workflow."""
    source = consumer_revision(root, requested)
    if consumer.git(checkout, "rev-parse", "HEAD") != source:
        raise ValueError("Consumer checkout differs from the selected source")
    if consumer.git(checkout, "status", "--porcelain", "--untracked-files=no"):
        raise ValueError("Consumer evidence requires clean tracked inputs")
    return source


def selection(root: Path, before: str, requested: str, source: str = "") -> list[dict[str, str]]:
    """Select affected declared consumers with completed images; dispatch can refresh all."""
    snapshot = History().head()
    if snapshot is None:
        return []
    source = source or consumer.git(root, "rev-parse", "HEAD")
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


def historical_runtime(
    checkout: Path, profile: str, image_id: str, evidence: Path, published: dict
) -> dict:
    """Execute the selected checkout's harness in its locked environment without signing access."""
    request = evidence / "consumer-execution-input.json"
    result = evidence / "consumer-execution.json"
    write(request, {"profile": profile, "imageId": image_id, "runtime": published})
    program = """
import importlib.metadata
import platform
import sys
from pathlib import Path
sys.path.insert(0, str(Path.cwd() / "scripts"))
from check_container_release_image import check
from container_release import read, write
request = read(Path(sys.argv[1]))
runtime = check(request["profile"], request["imageId"], Path(sys.argv[1]).parent,
                published_runtime=request["runtime"])
write(Path(sys.argv[2]), {"runtime": runtime, "python": platform.python_version(),
    "distributions": sorted({(d.metadata["Name"], d.version)
                             for d in importlib.metadata.distributions()})})
"""
    environment = {
        key: value
        for key, value in os.environ.items()
        if key
        not in {
            "PYTHONPATH",
            "VIRTUAL_ENV",
            "UV_PROJECT_ENVIRONMENT",
            "GH_TOKEN",
            "GITHUB_TOKEN",
            "ACTIONS_ID_TOKEN_REQUEST_TOKEN",
            "ACTIONS_ID_TOKEN_REQUEST_URL",
        }
    }
    subprocess.run(
        ["uv", "run", "--locked", "python", "-c", program, str(request), str(result)],
        cwd=checkout,
        env=environment,
        check=True,
        timeout=2400,
    )
    return read(result)


def qualify(
    root: Path,
    package: str,
    profile: str,
    backend: str,
    output: Path,
    *,
    checkout: Path | None = None,
    requested: str = "",
) -> None:
    """Authenticate published bytes before executing the profile's existing runtime checks."""
    from check_container_release_image import check
    from check_kind_release_images import qualified
    from container_release_registry import anonymous_pull

    if backend != "docker":
        raise ValueError("No qualification executor for this backend")
    source = verified_source_commit(root)
    checkout = checkout or root
    tested = checked_consumer(root, checkout, requested)
    client = GitHub()
    snapshot = History(client).head()
    if snapshot is None or profile not in snapshot.catalogue["current"]:
        raise ValueError("No completed image is available for consumer qualification")
    record = snapshot.catalogue["releases"][snapshot.catalogue["current"][profile]]
    expected = consumer.inputs(root, tested, package, profile, backend, record)
    evidence = output / "image-release"
    evidence.mkdir(parents=True)
    release_url = release_evidence(client, record, evidence)
    published_runtime = qualified(evidence, record["candidate"])
    image_id = anonymous_pull(record["candidate"])
    if tested == source:
        runtime = check(profile, image_id, evidence, published_runtime=published_runtime)
        distributions = sorted(
            {(d.metadata["Name"], d.version) for d in importlib.metadata.distributions()}
        )
        python = platform.python_version()
    else:
        execution = historical_runtime(checkout, profile, image_id, evidence, published_runtime)
        runtime = execution["runtime"]
        distributions = execution["distributions"]
        python = execution["python"]
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
        "consumerCommit": tested,
        "runId": os.environ["GITHUB_RUN_ID"],
        "runAttempt": os.environ["GITHUB_RUN_ATTEMPT"],
        "checkedAt": now(),
        "anonymousPullVerified": True,
        "releaseEvidence": release_url,
        "environment": {
            "python": python,
            "docker": docker,
            "distributions": distributions,
        },
        "runtime": runtime,
    }
    consumer.validate_record(value, expected)
    verified_source_commit(root)
    checked_consumer(root, checkout, requested)
    write(output / consumer.SUBJECT, expected)
    write(output / "consumer-qualification.json", value)


def validate_for_signing(
    root: Path,
    directory: Path,
    package: str,
    profile: str,
    backend: str,
    *,
    requested: str = "",
) -> None:
    """Recompute the input subject on a fresh runner before granting it a signature."""
    source = verified_source_commit(root)
    tested = consumer_revision(root, requested)
    value = read(directory / "consumer-qualification.json")
    subject = read(directory / consumer.SUBJECT)
    expected = consumer.inputs(root, tested, package, profile, backend, subject)
    consumer.validate_record(value, expected)
    if (
        subject != expected
        or value["sourceCommit"] != source
        or value.get("consumerCommit", source) != tested
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
    parser.add_argument("--source", default="")
    parser.add_argument("--consumer-root", type=Path)
    args = parser.parse_args()
    if args.operation == "select":
        import json

        source = consumer_revision(ROOT, args.source)
        matrix = selection(ROOT, args.before, args.profile, source)
        with Path(os.environ["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as output:
            output.write(f"matrix={json.dumps(matrix)}\n")
            output.write(f"source={source}\n")
    else:
        if args.directory is None:
            parser.error("--directory is required")
        if args.operation == "qualify":
            qualify(
                ROOT,
                args.package,
                args.profile,
                args.backend,
                args.directory.resolve(),
                checkout=args.consumer_root.resolve() if args.consumer_root else None,
                requested=args.source,
            )
        else:
            validate_for_signing(
                ROOT,
                args.directory,
                args.package,
                args.profile,
                args.backend,
                requested=args.source,
            )


if __name__ == "__main__":
    main()
