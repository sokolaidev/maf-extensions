"""Build the prepared Bicep image with a manifest fingerprint in its tag and label."""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

from bicep_dependencies import load_policy, sha256

ROOT = Path(__file__).resolve().parents[1]
CONTEXT = ROOT / "images/bicep-sandbox"


def main() -> None:
    """Use a caller-selected base built from this checkout's pinned Bicep Dockerfile."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-image", required=True)
    parser.add_argument("--tag")
    args = parser.parse_args()
    fingerprint = sha256((CONTEXT / "dependencies.bicep-avm.json").read_bytes())
    policy = load_policy(CONTEXT / "dependencies.bicep-avm.policy.json")
    tag = args.tag or f"bicep-sandbox:{policy['bicep_version']}-prepared-1-{fingerprint[:12]}"
    subprocess.run(
        [
            "docker",
            "build",
            "-f",
            str(CONTEXT / "prepared.Dockerfile"),
            "--build-arg",
            f"BASE_IMAGE={args.base_image}",
            "--build-arg",
            f"MANIFEST_SHA256={fingerprint}",
            "-t",
            tag,
            str(ROOT),
        ],
        check=True,
    )
    print(tag)


if __name__ == "__main__":
    main()
