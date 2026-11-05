"""Pull an authenticated published image compatible with the live sample's committed consumer."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from check_kind_release_images import KIND_IMAGES, compatibility, qualified, revision
from container_release import PREFIX
from container_release_history import GitHub, History
from image_release_needed import release_evidence
from verify_container_release import pull_image

PROFILE_ENV = {
    "shell": "SHELL_SANDBOX_IMAGE",
    "terraform-random": "TERRAFORM_SANDBOX_IMAGE",
    "opentofu-random": "OPENTOFU_SANDBOX_IMAGE",
}
PROFILE_PACKAGE = {
    profile: package
    for package, profiles in KIND_IMAGES.items()
    for profile in profiles
    if profile in PROFILE_ENV
}


def resolve(root: Path, head: str, profile: str) -> dict[str, Any]:
    """Authenticate current release evidence without substituting SDK compatibility for a live test."""
    client = GitHub()
    history = History(client)
    snapshot = history.head()
    if snapshot is None:
        raise ValueError("No completed image catalogue is available")
    name = snapshot.catalogue["current"].get(profile)
    if name is None:
        raise ValueError(f"No completed {profile} image; publish and qualify it first")
    record = snapshot.catalogue["releases"][name]
    if record["profile"] != profile:
        raise ValueError("Current catalogue entry identifies another profile")
    consumer_evidence = compatibility(root, head, PROFILE_PACKAGE[profile], profile, record)
    with tempfile.TemporaryDirectory(prefix="live-image-") as temporary:
        directory = Path(temporary)
        evidence = release_evidence(client, record, directory)
        qualified(directory, record["candidate"])
        manifest_size = (directory / "manifest.json").stat().st_size
    latest = history.head()
    if latest is None or latest.catalogue["current"].get(profile) != name:
        raise ValueError(f"Current {profile} image changed during verification; retry")
    return {
        "profile": profile,
        "image": f"{PREFIX}/{profile}@{record['registryDigest']}",
        "imageId": record["candidate"]["imageId"],
        "registryDigest": record["registryDigest"],
        "manifestSize": manifest_size,
        "sourceCommit": record["sourceCommit"],
        "sampleCommit": head,
        "evidence": evidence,
        "consumerQualification": consumer_evidence,
    }


def main() -> None:
    """Export the immutable reference only after evidence verification and a successful pull."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", choices=PROFILE_ENV, required=True)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--head", default="HEAD")
    args = parser.parse_args()
    try:
        selected = resolve(args.root, revision(args.root, args.head), args.profile)
        print(json.dumps(selected, indent=2), flush=True)
        pull_image(selected, selected["manifestSize"])
        if environment := os.environ.get("GITHUB_ENV"):
            with Path(environment).open("a", encoding="utf-8") as stream:
                stream.write(f"{PROFILE_ENV[args.profile]}={selected['image']}\n")
        if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
            with Path(summary).open("a", encoding="utf-8") as stream:
                stream.write(f"\n```json\n{json.dumps(selected, indent=2)}\n```\n")
    except (ValueError, KeyError, TypeError, OSError, subprocess.SubprocessError) as error:
        message = (
            str(error) if isinstance(error, ValueError) else "Image evidence or pull unavailable"
        )
        parser.exit(1, f"Published live image unavailable: {message}\n")


if __name__ == "__main__":
    main()
