"""Record a scan target's local image identity separately from a registry manifest digest."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

PROVIDER_MANIFESTS = {
    "terraform-random": "dependencies.terraform.json",
    "opentofu-random": "dependencies.opentofu.json",
    "terraform-avm": "dependencies.terraform-avm.json",
}


def verify_providers(sbom: dict[str, Any], manifest: dict[str, Any]) -> None:
    """Require each approved provider's Go inventory at its unpacked executable path."""
    providers = manifest["providers"]
    if not isinstance(providers, list) or not providers:
        raise ValueError("Expected provider inventory is empty")
    for provider in providers:
        name = provider["source"].rsplit("/", 1)[-1]
        version = provider["version"]
        guest_directory = (
            f"/opt/maf-terraform/mirror/{provider['source']}/{version}/{provider['platform']}/"
        )
        executable = re.compile(
            re.escape(guest_directory + "terraform-provider-" + name)
            + rf"(?:_v{re.escape(version)}(?:_x\d+)?)?"
        )
        by_guest_path: dict[str, list[dict[str, Any]]] = {}
        for artifact in sbom["artifacts"]:
            if artifact.get("type") != "go-module":
                continue
            for location in artifact.get("locations", []):
                guest_path = location.get("path", "")
                if executable.fullmatch(guest_path):
                    by_guest_path.setdefault(guest_path, []).append(artifact)
        if len(by_guest_path) != 1:
            raise ValueError(
                f"Provider inventory omits or ambiguously identifies {guest_directory}"
            )
        artifacts = next(iter(by_guest_path.values()))
        main_modules = [
            artifact
            for artifact in artifacts
            if artifact.get("name") == artifact.get("metadata", {}).get("mainModule")
            and re.search(
                rf"/terraform-provider-{re.escape(name)}(?:/v\d+)?$", artifact.get("name", "")
            )
        ]
        versions = {
            artifact.get("version") for artifact in artifacts if artifact.get("name") == "stdlib"
        }
        expected_go = provider.get("source_build", {}).get("go_version")
        if (
            not main_modules
            or len(versions) != 1
            or not all(
                isinstance(value, str) and re.fullmatch(r"go\d+\.\d+\.\d+", value)
                for value in versions
            )
            or (expected_go is not None and versions != {expected_go})
        ):
            raise ValueError(
                f"Provider Go inventory is incomplete or contradictory: {guest_directory}"
            )


def verify_profile_inventory(sbom: dict[str, Any], profile: str) -> None:
    """Use the scanned checkout's provider manifest rather than inferring coverage from the SBOM."""
    if profile in PROVIDER_MANIFESTS:
        context = Path(__file__).resolve().parents[1] / "images/terraform-sandbox"
        manifest = json.loads((context / PROVIDER_MANIFESTS[profile]).read_text(encoding="utf-8"))
        verify_providers(sbom, manifest)


def record(details: list[dict[str, Any]], revision: str, profile: str) -> dict[str, str]:
    """Refuse ambiguous targets and unsupported platforms before assigning scan evidence."""
    if len(details) != 1:
        raise ValueError("Expected exactly one image")
    image = details[0]
    image_id = image["Id"]
    if not isinstance(image_id, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id):
        raise ValueError("Expected an immutable local image ID")
    if (image["Os"], image["Architecture"]) != ("linux", "amd64"):
        raise ValueError("Image scan coverage is limited to linux/amd64")
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("Expected a source commit")
    return {
        "profile": profile,
        "source_commit": revision,
        "platform": "linux/amd64",
        "local_image_id": image_id,
        "collected_at": datetime.now(UTC).isoformat(),
    }


def verify_inventory(sbom: dict[str, Any], image_id: str) -> None:
    """Require components from the intended image before evaluating vulnerability results."""
    if not isinstance(sbom["artifacts"], list) or not sbom["artifacts"]:
        raise ValueError("No components were inventoried")
    source = sbom["source"]
    if source["type"] != "image" or source["metadata"]["imageID"] != image_id:
        raise ValueError("Inventory does not identify the built image")


def main() -> None:
    """Retain build identity and pass that exact image ID to the inventory step."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--verify-inventory", action="store_true")
    args = parser.parse_args()
    directory = args.directory
    if args.verify_inventory:
        evidence = json.loads((directory / "build.json").read_text())
        sbom = json.loads((directory / "sbom.syft.json").read_text())
        verify_inventory(sbom, evidence["local_image_id"])
        verify_profile_inventory(sbom, evidence["profile"])
        if (directory / "runtime.json").exists():
            from bicep_source import verify_inventory as verify_bicep_inventory

            runtime = json.loads((directory / "runtime.json").read_text())
            if runtime.get("imageId") != evidence["local_image_id"]:
                raise ValueError("Bicep runtime evidence identifies another image")
            verify_bicep_inventory(sbom, runtime)
        return
    details = json.loads((directory / "image-inspect.json").read_text())
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    evidence = record(details, revision, os.environ["PROFILE"])
    evidence["run_id"] = os.environ["GITHUB_RUN_ID"]
    evidence["run_attempt"] = os.environ["GITHUB_RUN_ATTEMPT"]
    evidence["run_url"] = (
        f"{os.environ['GITHUB_SERVER_URL']}/{os.environ['GITHUB_REPOSITORY']}"
        f"/actions/runs/{os.environ['GITHUB_RUN_ID']}"
    )
    (directory / "build.json").write_text(json.dumps(evidence, indent=2) + "\n")
    with Path(os.environ["GITHUB_OUTPUT"]).open("a") as output:
        output.write(f"image_id={evidence['local_image_id']}\n")
    with Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a") as summary:
        summary.write(
            f"### Image security: {evidence['profile']}\n\n"
            f"Source: `{revision}`; platform: `linux/amd64`.\n\n"
            f"Local image ID (not a registry manifest digest): `{evidence['local_image_id']}`.\n\n"
            "Policy: fail on High/Critical findings, including unfixed findings. "
            "A build or scanner failure also fails the workflow. "
            "Inventory and scan results are retained as workflow artifacts for 30 days.\n"
        )


if __name__ == "__main__":
    main()
