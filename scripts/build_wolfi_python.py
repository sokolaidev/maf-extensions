"""Build fixed Python APKs and supply them to a Wolfi consumer image build."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from datetime import UTC, datetime, timedelta
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


def bundle_key() -> str:
    """Bind reusable APKs to the complete recipe context, helper and target platform."""
    context = ROOT / "images/wolfi-python"
    for name in ("Dockerfile", "configure.py", "build.sh"):
        if not (context / name).is_file():
            raise ValueError(f"Missing Python build input: {name}")
    inputs = [Path(__file__).resolve(), *sorted(context.rglob("*"))]
    digest = hashlib.sha256(PLATFORM.encode())
    for path in inputs:
        if path.is_symlink():
            raise ValueError("Python build inputs must not contain symlinks")
        if path.is_file():
            digest.update(path.relative_to(ROOT).as_posix().encode() + b"\0")
            digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def bundle_files(directory: Path) -> dict[str, str]:
    """Inventory only public package outputs; private signing keys must never be retained."""
    files = {}
    if directory.is_symlink():
        raise ValueError("Python package bundle must not contain symlinks")
    for path in sorted(directory.rglob("*")):
        if path.is_symlink():
            raise ValueError("Python package bundle must not contain symlinks")
        if path.is_dir():
            continue
        name = path.relative_to(directory).as_posix()
        if name == "bundle.json":
            continue
        if name not in {
            "maf-python.rsa.pub",
            "python-3.13.yaml",
            "x86_64/APKINDEX.tar.gz",
        } and not (
            path.parent == directory / "x86_64"
            and path.name.startswith("python-3.13")
            and path.suffix == ".apk"
        ):
            raise ValueError(f"Unexpected Python package output: {name}")
        files[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    required = {
        "maf-python.rsa.pub",
        "python-3.13.yaml",
        "x86_64/APKINDEX.tar.gz",
        "x86_64/python-3.13-3.13.18-r0.apk",
        "x86_64/python-3.13-base-3.13.18-r0.apk",
    }
    if not required <= files.keys():
        raise ValueError("Python package bundle is incomplete")
    return files


def seal_bundle(directory: Path) -> None:
    """Record preparation identity for transfer within CI; this is not release attestation."""
    record = {
        "schemaVersion": 1,
        "inputs": bundle_key(),
        "platform": PLATFORM,
        "createdAt": datetime.now(UTC).isoformat(),
        "files": bundle_files(directory),
    }
    (directory / "bundle.json").write_text(json.dumps(record, sort_keys=True), encoding="utf-8")


def verify_bundle(directory: Path) -> None:
    """Refuse changed, stale or incompatible cached preparation before a consumer build."""
    files = bundle_files(directory)
    record = json.loads((directory / "bundle.json").read_text(encoding="utf-8"))
    if (
        record.get("schemaVersion") != 1
        or record.get("inputs") != bundle_key()
        or record.get("platform") != PLATFORM
        or record.get("files") != files
    ):
        raise ValueError("Python package bundle differs from its inputs or recorded bytes")
    created = datetime.fromisoformat(record["createdAt"])
    if created.tzinfo is None or not timedelta(minutes=-5) <= datetime.now(
        UTC
    ) - created <= timedelta(hours=48):
        raise ValueError("Python package bundle is stale or has an invalid preparation time")


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
    """Build an AMD64 consumer with fresh or verified shared Python packages."""
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
        if supplied := os.environ.get("MAF_WOLFI_PYTHON_PACKAGES"):
            directory = Path(supplied).absolute()
            verify_bundle(directory)
        else:
            directory = Path(temporary)
            build_packages(directory)
        subprocess.run(
            command[:2]
            + ["--build-context", f"python-packages={directory}", "--platform", PLATFORM]
            + consumer_arguments,
            check=True,
        )


def main() -> None:
    """Accept Docker build arguments, or export the packages for a local test build."""
    if sys.argv[1:] == ["--bundle-key"]:
        print(bundle_key() + "-" + datetime.now(UTC).strftime("%Y%m%d"))
    elif sys.argv[1:2] in (["--prepare-bundle"], ["--verify-bundle"]):
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument(sys.argv[1], required=True, type=Path, dest="directory")
        directory = parser.parse_args().directory
        if sys.argv[1] == "--prepare-bundle":
            build_packages(directory)
            seal_bundle(directory)
        verify_bundle(directory)
    elif sys.argv[1:2] == ["--output"]:
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument("--output", required=True, type=Path)
        build_packages(parser.parse_args().output)
    else:
        build_image(["docker", "build", *sys.argv[1:]])


if __name__ == "__main__":
    main()
