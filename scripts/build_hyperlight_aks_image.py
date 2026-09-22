"""Build an AKS runtime from workspace wheels and the locked dependency graph."""

from __future__ import annotations

import argparse
import base64
import gzip
import hashlib
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def prepare(destination: Path) -> None:
    """Include only built wheels, hashed dependencies and the public probe in the build context."""
    destination.mkdir(parents=True, exist_ok=True)
    allowed = {
        "wheels",
        "Dockerfile",
        ".dockerignore",
        "probe.py",
        "requirements.txt",
        "build-inputs.json",
        "bundle.json.gz",
    }
    if any(path.name not in allowed for path in destination.iterdir()):
        raise ValueError("build output directory contains unrelated files")
    for package in ("maf-sandbox", "maf-sandbox-hyperlight", "maf-sandbox-codeact"):
        subprocess.run(
            [
                "uv",
                "build",
                "--package",
                package,
                "--wheel",
                "--out-dir",
                str(destination / "wheels"),
            ],
            cwd=ROOT,
            check=True,
        )
    subprocess.run(
        [
            "uv",
            "export",
            "--locked",
            "--no-dev",
            "--package",
            "maf-sandbox-hyperlight",
            "--package",
            "maf-sandbox-codeact",
            "--no-emit-workspace",
            "--no-header",
            "--output-file",
            str(destination / "requirements.txt"),
        ],
        cwd=ROOT,
        check=True,
        stdout=subprocess.DEVNULL,
    )
    shutil.copyfile(ROOT / "images/hyperlight-sandbox/Dockerfile", destination / "Dockerfile")
    shutil.copyfile(ROOT / "images/hyperlight-sandbox/.dockerignore", destination / ".dockerignore")
    built = sorted(path for path in (destination / "wheels").iterdir() if path.name != ".gitignore")
    if len(built) != 3 or any(path.suffix != ".whl" for path in built):
        raise ValueError("build output contains stale or unexpected wheels; use a fresh directory")
    shutil.copyfile(ROOT / "samples/experimental/hyperlight-aks/probe.py", destination / "probe.py")
    evidence = {
        path.relative_to(destination).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in destination.rglob("*")
        if path.is_file() and path.name not in {"build-inputs.json", "bundle.json.gz"}
    }
    (destination / "build-inputs.json").write_text(json.dumps(evidence, indent=2), encoding="utf-8")
    bundle = {
        "files": {
            name: (destination / name).read_text(encoding="utf-8")
            for name in ("requirements.txt", "probe.py")
        },
        "wheels": {
            path.name: base64.b64encode(path.read_bytes()).decode()
            for path in sorted((destination / "wheels").glob("*.whl"))
        },
    }
    encoded = gzip.compress(json.dumps(bundle, sort_keys=True).encode(), mtime=0)
    if len(encoded) > 700_000:
        raise ValueError("wheel bundle exceeds the ConfigMap transport bound; publish the image")
    (destination / "bundle.json.gz").write_bytes(encoded)
    print("bundle sha256:", hashlib.sha256(encoded).hexdigest())


def main() -> None:
    """Prepare a reviewable context; build locally only when an image tag is supplied."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--tag")
    args = parser.parse_args()
    destination = args.output or Path(tempfile.mkdtemp(prefix="maf-hyperlight-image-"))
    prepare(destination)
    if args.tag:
        subprocess.run(
            ["docker", "build", "--platform", "linux/amd64", "--tag", args.tag, str(destination)],
            check=True,
        )
    print(destination)


if __name__ == "__main__":
    main()
