"""Propose verified Bicep compiler upgrades without changing the AVM module selection."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import urllib.request
from pathlib import Path
from typing import Any

from bicep_dependencies import load_policy, sha256
from bicep_image_pin import VERSION, read_pin
from update_bicep_catalog import fetch

ROOT = Path(__file__).resolve().parents[1] / "images/bicep-sandbox"
FILES = ("Dockerfile", "dependencies.bicep-avm.policy.json", "dependencies.bicep-avm.json")
LIMIT = 256 * 1024 * 1024


def release_asset(release: str) -> tuple[str, dict[str, Any]]:
    """Require one stable upstream Linux x64 asset with a published SHA-256 digest."""
    if release != "latest" and not re.fullmatch("v" + VERSION, release):
        raise ValueError("expected a stable vMAJOR.MINOR.PATCH release")
    endpoint = "latest" if release == "latest" else f"tags/{release}"
    metadata = json.loads(fetch(f"https://api.github.com/repos/Azure/bicep/releases/{endpoint}"))
    tag = metadata["tag_name"]
    if (
        not isinstance(tag, str)
        or not re.fullmatch("v" + VERSION, tag)
        or metadata.get("draft") is not False
        or metadata.get("prerelease") is not False
        or (release != "latest" and release != tag)
    ):
        raise ValueError("expected the requested stable Bicep release")
    assets = [asset for asset in metadata["assets"] if asset.get("name") == "bicep-linux-x64"]
    if len(assets) != 1:
        raise ValueError("expected exactly one Linux x64 Bicep asset")
    asset = assets[0]
    if (
        asset.get("browser_download_url")
        != f"https://github.com/Azure/bicep/releases/download/{tag}/bicep-linux-x64"
        or not isinstance(asset.get("digest"), str)
        or not re.fullmatch(r"sha256:[0-9a-f]{64}", asset["digest"])
        or type(asset.get("size")) is not int
        or not 0 < asset["size"] <= LIMIT
    ):
        raise ValueError("invalid Bicep asset URL, digest or size")
    return tag[1:], asset


def verify_binary(asset: dict[str, Any]) -> None:
    """Hash the bounded download without executing the upstream binary."""
    digest = hashlib.sha256()
    size = 0
    request = urllib.request.Request(
        asset["browser_download_url"], headers={"User-Agent": "maf-extensions-bicep-images"}
    )
    with urllib.request.urlopen(request, timeout=90) as response:
        while chunk := response.read(1024 * 1024):
            size += len(chunk)
            if size > asset["size"] or size > LIMIT:
                raise ValueError("Bicep asset exceeds its declared size")
            digest.update(chunk)
    if size != asset["size"] or "sha256:" + digest.hexdigest() != asset["digest"]:
        raise ValueError("Bicep asset size or SHA-256 mismatch")


def updates(root: Path, release: str) -> tuple[dict[str, bytes], str]:
    """Validate the existing pins and return a complete proposal before writing any file."""
    dockerfile = (root / FILES[0]).read_text(encoding="utf-8")
    tag, previous_checksum = read_pin(root / FILES[0])
    current = tag.removeprefix("v")
    policy_path = root / FILES[1]
    policy = load_policy(policy_path)
    manifest = json.loads((root / FILES[2]).read_bytes())
    if (
        manifest.get("schema") != 1
        or policy["bicep_version"] != current
        or manifest["bicep_version"] != current
        or manifest["policy_sha256"] != sha256(policy_path.read_bytes())
        or manifest["excluded"] != policy["excluded"]
        or [entry["reference"] for entry in manifest["modules"]] != policy["modules"]
    ):
        raise ValueError("existing Bicep image policy and manifest disagree")
    version, asset = release_asset(release)
    checksum = asset["digest"].removeprefix("sha256:")
    if tuple(map(int, version.split("."))) < tuple(map(int, current.split("."))):
        raise ValueError("refusing to downgrade the Bicep compiler")
    if version == current:
        if checksum != previous_checksum:
            raise ValueError("published checksum changed for the pinned Bicep release")
        return {}, "The Bicep image compiler pin is current.\n"
    verify_binary(asset)
    dockerfile = dockerfile.replace(
        f"ARG BICEP_VERSION=v{current}", f"ARG BICEP_VERSION=v{version}"
    )
    dockerfile = dockerfile.replace(
        f"ARG BICEP_SHA256={previous_checksum}", f"ARG BICEP_SHA256={checksum}"
    )
    policy["bicep_version"] = version
    policy_bytes = (json.dumps(policy, indent=2) + "\n").encode()
    manifest["bicep_version"] = version
    manifest["policy_sha256"] = sha256(policy_bytes)
    changes = {
        FILES[0]: dockerfile.encode(),
        FILES[1]: policy_bytes,
        FILES[2]: (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode(),
    }
    body = f"""## Summary

Update the compiler used by `bicep`, `bicep-avm` and `sbx-bicep` from `{current}` to `{version}`.

## Changes

- Pin the [upstream Linux x64 binary]({asset["browser_download_url"]}) to SHA-256 `{checksum}`.
- Align the AVM compiler requirement and policy fingerprint; preserve every module version, module digest and exclusion.

## Verification

The downloaded binary's size and SHA-256 match upstream release metadata. The binary was not executed. Image builds, offline compatibility and vulnerability scanning remain release gates, including refusal of every High/Critical finding.

## Publication

This PR changes build inputs only. After merge, Image release tracking detects the affected profiles during its next scheduled reconciliation or manual dispatch. Prepare and qualify each candidate through Container image release, then obtain maintainer approval before publication. Existing published digests remain unchanged.
"""
    return changes, body


def main() -> int:
    """Write a reviewed-input proposal, or report available changes without editing files."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release", default="latest")
    parser.add_argument("--output", type=Path, default=ROOT)
    parser.add_argument("--body-file", type=Path)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    changes, body = updates(args.output, args.release)
    if args.check:
        return int(bool(changes))
    for name, content in changes.items():
        (args.output / name).write_bytes(content)
    if args.body_file:
        args.body_file.write_text(body, encoding="utf-8", newline="\n")
    print("Updated: " + (", ".join(changes) or "nothing"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
