"""Enforce the image vulnerability gate while retaining raw scanner findings."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from container_release import now, read, scan_result, write
from image_security_evidence import verify_inventory


def assess(directory: Path, *, event: str = "schedule") -> None:
    """Validate retained evidence, warning on PR findings and refusing them otherwise."""
    image_id = read(directory / "build.json")["local_image_id"]
    sbom = read(directory / "sbom.syft.json")
    verify_inventory(sbom, image_id)
    result = scan_result(read(directory / "grype.json"), image_id, now(), sbom=sbom)
    write(directory / "assessment.json", result)
    for decision in result.get("notAffected", []):
        print("Reviewed non-applicability: " + json.dumps(decision, ensure_ascii=True))
    if result["outcome"] != "clean":
        message = "Applicable High/Critical findings remain; see assessment.json and grype.json"
        if event != "pull_request":
            raise ValueError(message)
        print("::warning::" + message)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument(
        "--event", choices=("pull_request", "schedule", "workflow_dispatch"), default="schedule"
    )
    args = parser.parse_args()
    assess(args.directory, event=args.event)
