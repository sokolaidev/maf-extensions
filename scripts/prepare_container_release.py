"""Retain and assess a labelled image without publishing or signing it."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from check_container_release_image import check
from container_release import (
    PREFIX,
    REPOSITORY,
    ROOT,
    digest,
    now,
    profiles,
    read,
    scan_result,
    validate_identity,
    version,
    write,
)
from container_release_oci import validate_layout
from image_security_evidence import record, verify_inventory


def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
    """Run a bounded local build tool with errors propagated to the publication gate."""
    return subprocess.run(
        command, check=True, text=True, encoding="utf-8", errors="strict", timeout=2400, **kwargs
    )


def labels(profile: str, release_version: str, revision: str) -> dict[str, str]:
    """Separate suite release labels from the upstream engine's own version labels."""
    return {
        "org.opencontainers.image.source": f"https://github.com/{REPOSITORY}",
        "org.opencontainers.image.revision": revision,
        "dev.sokolai.maf.image.profile": profile,
        "dev.sokolai.maf.image.version": release_version,
    }


def require_source(revision: str) -> None:
    """Require the dispatch, checked-out source and signed source to be identical."""
    if (
        os.environ.get("GITHUB_REPOSITORY") != REPOSITORY
        or os.environ.get("GITHUB_REF") != "refs/heads/main"
        or os.environ.get("GITHUB_SHA") != revision
    ):
        raise ValueError("Release preparation requires the protected-main dispatch identity")
    if run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True).stdout.strip() != revision:
        raise ValueError("Workflow source and payload checkout differ")
    if run(
        ["git", "status", "--porcelain", "--untracked-files=normal"], cwd=ROOT, capture_output=True
    ).stdout:
        raise ValueError("Release preparation requires a clean checkout")


def assess(directory: Path, image_id: str, *, require_clean: bool = True) -> dict[str, Any]:
    """Refresh the exact image's inventory and strict High/Critical assessment."""
    directory.mkdir(parents=True, exist_ok=True)
    run(
        [
            "syft",
            "scan",
            f"docker:{image_id}",
            "-o",
            f"syft-json={directory / 'sbom.syft.json'}",
            "-o",
            f"spdx-json={directory / 'sbom.spdx.json'}",
        ]
    )
    verify_inventory(read(directory / "sbom.syft.json"), image_id)
    write(directory / "grype.yaml", {})
    environment = {k: v for k, v in os.environ.items() if not k.startswith("GRYPE_")}
    environment.update(
        GRYPE_DB_AUTO_UPDATE="true",
        GRYPE_DB_VALIDATE_AGE="true",
        GRYPE_DB_VALIDATE_BY_HASH_ON_START="true",
    )
    run(["grype", "db", "update", "--config", str(directory / "grype.yaml")], env=environment)
    scan = subprocess.run(
        [
            "grype",
            f"sbom:{directory / 'sbom.syft.json'}",
            "--config",
            str(directory / "grype.yaml"),
            "--fail-on",
            "high",
            "--output",
            "json",
            "--file",
            str(directory / "grype.json"),
        ],
        check=False,
        text=True,
        encoding="utf-8",
        timeout=1200,
        env=environment,
    )
    if scan.returncode not in (0, 2):
        raise ValueError("Vulnerability scanner did not complete")
    result = scan_result(read(directory / "grype.json"), image_id, now())
    write(directory / "assessment.json", result)
    if (scan.returncode == 0) != (result["outcome"] == "clean"):
        raise ValueError("Scanner exit status disagrees with the retained assessment")
    if require_clean and (scan.returncode or result["outcome"] != "clean"):
        raise ValueError("High/Critical assessment failed, including unfixed findings")
    return result


def retain(image_id: str, output: Path) -> dict[str, str]:
    """Create the OCI bytes once; later promotion must preserve these manifest digests."""
    layout = output / "oci"
    if layout.exists():
        raise ValueError("Candidate output already contains retained OCI bytes")
    with tempfile.TemporaryDirectory(prefix="maf-image-export-") as temporary:
        archive = Path(temporary) / "image.tar"
        run(["docker", "save", "--output", str(archive), image_id])
        run(
            [
                "skopeo",
                "copy",
                "--format",
                "oci",
                f"docker-archive:{archive}",
                f"oci:{layout}:candidate",
            ]
        )
    identity = validate_layout(layout, image_id)
    source = layout / "blobs" / "sha256" / identity["registryDigest"].removeprefix("sha256:")
    shutil.copyfile(source, output / "manifest.json")
    return identity


def prepare(
    profile: str, release_version: str, revision: str, attempt: str, output: Path
) -> dict[str, Any]:
    """Build and retain a candidate; no registry or signing credentials are required."""
    if profile not in profiles():
        raise ValueError("Unrecognized release profile")
    version(release_version)
    require_source(revision)
    if output.exists():
        raise ValueError("Use a fresh candidate directory")
    output.mkdir(parents=True)
    run(["bash", "scripts/build_scan_image.sh", profile], cwd=ROOT)
    target = "maf-image-release:candidate"
    command = ["docker", "build", "--network", "none", "--platform", "linux/amd64", "--tag", target]
    for name, value in labels(profile, release_version, revision).items():
        command.extend(["--label", f"{name}={value}"])
    run([*command, "-"], input="FROM maf-image-scan:target\n")
    details = json.loads(run(["docker", "image", "inspect", target], capture_output=True).stdout)
    local = record(details, revision, profile)
    image_id = local["local_image_id"]
    if any(
        details[0]["Config"].get("Labels", {}).get(k) != v
        for k, v in labels(profile, release_version, revision).items()
    ):
        raise ValueError("Candidate release labels are missing or inconsistent")
    write(
        output / "build.json",
        local
        | {
            "version": release_version,
            "attemptId": attempt,
            "imageRepository": f"{PREFIX}/{profile}",
        },
    )
    identity = retain(image_id, output)
    candidate = identity | {
        "profile": profile,
        "version": release_version,
        "sourceCommit": revision,
        "sourceRef": "refs/heads/main",
        "attemptId": attempt,
    }
    validate_identity(candidate)
    write(output / "candidate.json", candidate)
    inputs = {"uv.lock": digest(ROOT / "uv.lock")}
    for path in sorted((ROOT / "images").rglob("*")):
        if path.is_file() and (path.name == "Dockerfile" or path.suffix == ".json"):
            inputs[path.relative_to(ROOT).as_posix()] = digest(path)
    write(output / "build-inputs.json", inputs)
    if profile == "hyperlight":
        for name in ("source.json", "build-inputs.json", "image-verification.json"):
            shutil.copyfile(
                Path(os.environ["RUNNER_TEMP"]) / "hyperlight-image" / name,
                output / f"hyperlight-{name}",
            )
        candidate["buildInputsSha256"] = digest(output / "hyperlight-build-inputs.json")
        write(output / "candidate.json", candidate)
    write(output / "runtime.json", check(profile, image_id, output))
    assess(output, image_id)
    write(
        output / "preparation-index.json",
        {
            path.name: digest(path)
            for path in output.iterdir()
            if path.is_file() and path.name != "candidate.json"
        },
    )
    candidate["preparationEvidenceSha256"] = digest(output / "preparation-index.json")
    write(output / "candidate.json", candidate)
    return candidate


def main() -> None:
    """Prepare a candidate using the dispatch source, never an independently selected checkout."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", choices=profiles(), required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(
        json.dumps(
            prepare(
                args.profile,
                args.version,
                os.environ["GITHUB_SHA"],
                os.environ["GITHUB_RUN_ID"],
                args.output,
            )
        )
    )


if __name__ == "__main__":
    main()
