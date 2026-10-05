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
        verify_inventory(
            json.loads((directory / "sbom.syft.json").read_text()), evidence["local_image_id"]
        )
        return
    details = json.loads((directory / "image-inspect.json").read_text())
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    evidence = record(details, revision, os.environ["PROFILE"])
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
