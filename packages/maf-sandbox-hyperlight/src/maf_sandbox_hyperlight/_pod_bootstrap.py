"""Install a digest-verified wheel bundle before the scoped application starts."""

from __future__ import annotations

import base64
import gzip
import hashlib
import io
import json
import os
import re
import subprocess
import sys
from pathlib import Path


def main() -> None:
    """Bootstrap a pinned Python image without a private registry or executable download script."""
    archive = Path("/bundle/bundle.json.gz").read_bytes()
    if len(archive) > 700_000 or hashlib.sha256(archive).hexdigest() != sys.argv[1]:
        raise ValueError("application bundle digest or size differs")
    with gzip.GzipFile(fileobj=io.BytesIO(archive)) as source:
        expanded = source.read(5 * 1024**2 + 1)
    if len(expanded) > 5 * 1024**2:
        raise ValueError("expanded application bundle exceeds its limit")
    bundle = json.loads(expanded)
    work = Path("/work")
    temporary = work / "tmp"
    temporary.mkdir(mode=0o700)
    os.environ["TMPDIR"] = str(temporary)
    wheels = work / "wheels"
    wheels.mkdir(mode=0o700)
    if set(bundle["files"]) != {"requirements.txt", "probe.py"}:
        raise ValueError("unexpected application bundle files")
    for name, content in bundle["files"].items():
        (work / name).write_text(content, encoding="utf-8")
    if not 1 <= len(bundle["wheels"]) <= 16:
        raise ValueError("invalid wheel count")
    for name, content in bundle["wheels"].items():
        if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*\.whl", name):
            raise ValueError("invalid wheel filename")
        (wheels / name).write_bytes(base64.b64decode(content, validate=True))
    subprocess.run([sys.executable, "-m", "venv", str(work / "runtime")], check=True)
    python = str(work / "runtime/bin/python")
    subprocess.run(
        [
            python,
            "-m",
            "pip",
            "install",
            "--no-cache-dir",
            "--require-hashes",
            "-r",
            str(work / "requirements.txt"),
        ],
        check=True,
    )
    subprocess.run(
        [
            python,
            "-m",
            "pip",
            "install",
            "--no-cache-dir",
            "--no-deps",
            *(str(path) for path in sorted(wheels.glob("*.whl"))),
        ],
        check=True,
    )
    subprocess.run([python, "-m", "pip", "check"], check=True)


if __name__ == "__main__":
    main()
