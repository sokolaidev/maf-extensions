"""Select the upstream binary or reviewed source build for the Bicep base image."""

from __future__ import annotations

import argparse
import os
import subprocess
from pathlib import Path

from bicep_image_pin import read_pin
from bicep_source import policy

ROOT = Path(__file__).resolve().parents[1]
CONTEXT = ROOT / "images/bicep-sandbox"
ROUTES = ("upstream", "source")
PROFILES = {"bicep", "bicep-avm", "bicep-docker-sbx"}


def validate_route(route: str, profile: str) -> None:
    """Reject unknown routes and source selection for unrelated release profiles."""
    if route not in ROUTES:
        raise ValueError(f"Unknown bicep_route={route!r}; choose 'upstream' or 'source'")
    if route != "upstream" and profile not in PROFILES:
        raise ValueError(
            f"Image profile {profile!r} does not support bicep_route='source'. "
            "Choose 'upstream' for this image. This option controls only Bicep compiler "
            "acquisition; it does not change how Terraform or other engines are built."
        )


def command(route: str, tag: str) -> list[str]:
    """Build the selected recipe with the source SDK pinned by digest."""
    validate_route(route, "bicep")
    command = ["docker", "build", "--pull", "--platform", "linux/amd64", "--tag", tag]
    if route == "source":
        data = policy(CONTEXT / "source.json")
        version, _ = read_pin(CONTEXT / "Dockerfile")
        if version != "v" + data["version"]:
            raise ValueError("Review the Bicep source policy after updating the upstream compiler")
        command += [
            "--file",
            str(CONTEXT / "source.Dockerfile"),
            "--build-context",
            f"scripts={ROOT / 'scripts'}",
            "--build-arg",
            f"SDK_IMAGE={data['sdkImage']}",
        ]
    return [*command, str(CONTEXT)]


def main() -> None:
    """Build a local image without publishing or signing it."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--route", choices=ROUTES, default=os.environ.get("BICEP_ROUTE", "upstream")
    )
    parser.add_argument("--tag", required=True)
    args = parser.parse_args()
    subprocess.run(command(args.route, args.tag), check=True, timeout=3600)


if __name__ == "__main__":
    main()
