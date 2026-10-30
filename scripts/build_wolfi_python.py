"""Build fixed Python APKs and supply them to a Wolfi consumer image build."""

from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]


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
                "linux/amd64",
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
                    "linux/amd64",
                    iid.read_text(encoding="utf-8").strip(),
                ],
                check=True,
            )
            subprocess.run(["docker", "start", "--attach", name], check=True)
            subprocess.run(["docker", "cp", f"{name}:/out/.", str(destination)], check=True)
        finally:
            subprocess.run(["docker", "rm", "--force", name], check=True)


def build_image(command: list[str]) -> None:
    """Run a Docker build with freshly built Python packages as an explicit context."""
    if command[:2] != ["docker", "build"]:
        raise ValueError("Expected a Docker build command")
    with tempfile.TemporaryDirectory(prefix="maf-python-packages-") as temporary:
        build_packages(Path(temporary))
        subprocess.run(
            command[:2] + ["--build-context", f"python-packages={temporary}"] + command[2:],
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
