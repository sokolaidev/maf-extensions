"""Build fixed Python APKs and supply them to a Wolfi consumer image build."""

from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
PLATFORM = "linux/amd64"
PYTHON_PROFILES = (
    "document",
    "drawio",
    "drawio-export",
    "shell",
    "terraform-random",
    "opentofu-random",
    "terraform-avm",
    "opentofu-avm",
    "hyperlight-aks-probe",
)


def build_packages(destination: Path) -> None:
    """Export signed APKs from a disposable Melange container without host mounts."""
    destination.mkdir(parents=True, exist_ok=True)
    if any(destination.iterdir()):
        raise ValueError("Python package output must be empty")
    with tempfile.TemporaryDirectory(prefix="maf-python-builder-") as temporary:
        iid = Path(temporary) / "image-id"
        subprocess.run(
            [
                "docker",
                "build",
                "--platform",
                PLATFORM,
                "--iidfile",
                str(iid),
                str(ROOT / "images/wolfi-python"),
            ],
            check=True,
        )
        name = f"maf-python-build-{uuid4().hex}"
        try:
            subprocess.run(
                [
                    "docker",
                    "create",
                    "--name",
                    name,
                    "--privileged",
                    "--platform",
                    PLATFORM,
                    iid.read_text(encoding="utf-8").strip(),
                ],
                check=True,
            )
            subprocess.run(["docker", "start", "--attach", name], check=True)
            subprocess.run(["docker", "cp", f"{name}:/out/.", str(destination)], check=True)
        finally:
            subprocess.run(["docker", "rm", "--force", name], check=True)


def build_image(command: list[str]) -> None:
    """Build an AMD64 consumer with fresh Python packages; reject conflicting platforms."""
    if command[:2] != ["docker", "build"]:
        raise ValueError("Expected a Docker build command")
    arguments = iter(command[2:])
    consumer_arguments = []
    for argument in arguments:
        if argument == "--platform":
            platform = next(arguments, "")
        elif argument.startswith("--platform="):
            platform = argument.partition("=")[2]
        else:
            consumer_arguments.append(argument)
            continue
        if platform != PLATFORM:
            raise ValueError(f"Python APKs require --platform {PLATFORM}")
    with tempfile.TemporaryDirectory(prefix="maf-python-packages-") as temporary:
        build_packages(Path(temporary))
        subprocess.run(
            command[:2]
            + ["--build-context", f"python-packages={temporary}", "--platform", PLATFORM]
            + consumer_arguments,
            check=True,
        )


def main() -> None:
    """Accept Docker build arguments, or export the packages for a local test build."""
    if sys.argv[1:2] == ["--output"]:
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument("--output", required=True, type=Path)
        build_packages(parser.parse_args().output)
    else:
        build_image(["docker", "build", *sys.argv[1:]])


if __name__ == "__main__":
    main()
