"""Require real WSLC execution, file transfer and removal on a disposable CI runner."""

from __future__ import annotations

import argparse
import json
import subprocess
import tempfile
import uuid
from pathlib import Path


def command(*args: str) -> str:
    """Run one bounded CLI command, preserving diagnostics on failure."""
    result = subprocess.run(
        ["wslc", *args], capture_output=True, text=True, encoding="utf-8", timeout=180, check=False
    )
    print(result.stdout, end="", flush=True)
    print(result.stderr, end="", flush=True)
    result.check_returncode()
    return result.stdout


def qualify(image: str) -> None:
    """Prove the engine contract before pytest's optional live-test gates are evaluated."""
    name = "maf-ci-probe-" + uuid.uuid4().hex
    marker = uuid.uuid4().hex
    try:
        command(
            "container",
            "run",
            "-d",
            "--name",
            name,
            "--network",
            "none",
            image,
            "sleep",
            "infinity",
        )
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "probe.txt"
            source.write_text(marker, encoding="utf-8")
            command("container", "cp", str(source), f"{name}:/tmp/maf-ci-probe.txt")
        output = command("container", "exec", name, "cat", "/tmp/maf-ci-probe.txt")
        if output.strip() != marker:
            raise RuntimeError("WSLC did not return the transferred file contents")
    finally:
        command("container", "remove", "-f", name)
    payload = command("container", "list", "-a", "--format", "json").strip()
    inventory = (
        json.loads(payload)
        if payload.startswith("[")
        else [json.loads(line) for line in payload.splitlines() if line.strip()]
    )
    if not isinstance(inventory, list) or any(not isinstance(item, dict) for item in inventory):
        raise RuntimeError("WSLC returned an unexpected inventory")
    if any(name in str(item) for item in inventory):
        raise RuntimeError("WSLC retained the qualification container after removal")
    print("WSLC qualification passed: execution, file transfer and confirmed removal", flush=True)


def main() -> None:
    """CLI entry."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image")
    qualify(parser.parse_args().image)


if __name__ == "__main__":
    main()
