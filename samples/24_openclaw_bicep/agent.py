"""Validate and repair Bicep through OpenClaw using the qualified published worker."""

# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///

from __future__ import annotations

import argparse
import hashlib
import http.client
import runpy
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

from _scaffold import MEASURED

ROOT = Path(__file__).resolve().parents[2]
HARNESS = ROOT / "samples/experimental/openclaw_bicep_e2e/agent.py"
WHEEL = "maf_sandbox_mcp-0.3.1-py3-none-any.whl"
WHEEL_PATH = "/packages/b9/2f/7958be1bb8a8bcfdca8b14817f6ea8bc74a0e4bd432a24eb591c1c7a0a53/" + WHEEL
WHEEL_SHA256 = "36d1af36ef6ed6fc174eeedc34a2c5f9a5e6a670134e0f6682d9c35955b079b1"
IMAGE = (
    "ghcr.io/sokolaidev/maf-extensions/bicep-avm@sha256:"
    "b3f14b266052def3f7b5a7b6ca4fb38f06acdff0e99e7df2437dd4f0c6b0b82e"
)
MAX_WHEEL_BYTES = 4 * 1024 * 1024


def download(root: Path) -> Path:
    """Verify the fixed published artifact before making it available for installation."""
    connection = http.client.HTTPSConnection("files.pythonhosted.org", timeout=30)
    try:
        connection.request("GET", WHEEL_PATH)
        response = connection.getresponse()
        if response.status != 200:
            raise RuntimeError(f"Worker download returned HTTP {response.status}")
        payload = response.read(MAX_WHEEL_BYTES + 1)
        if len(payload) > MAX_WHEEL_BYTES:
            raise RuntimeError("Worker download exceeds size limit")
        if hashlib.sha256(payload).hexdigest() != WHEEL_SHA256:
            raise RuntimeError("Published worker SHA-256 mismatch")
        wheel = root / WHEEL
        wheel.write_bytes(payload)
        return wheel
    finally:
        connection.close()


def launch(wheel: Path, output: Path, mode: str) -> None:
    """Use the existing lifecycle in this process so signal handling and cleanup stay together."""
    original_argv, original_path = sys.argv, sys.path[:]
    try:
        sys.path.insert(0, str(HARNESS.parent))
        sys.argv = [
            str(HARNESS),
            "--wheel",
            str(wheel),
            "--image",
            IMAGE,
            "--output",
            str(output),
            "--mode",
            mode,
            "--reasoning-effort",
            "none",
        ]
        runpy.run_path(str(HARNESS), run_name="__main__")
    finally:
        sys.argv, sys.path[:] = original_argv, original_path


def main() -> None:
    """Provision the published sample without a local worker build or existing Gateway."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("smoke", "model"), default="smoke")
    parser.add_argument("--output", type=Path, help="New private directory outside the checkout")
    args = parser.parse_args()
    output = (
        args.output or Path(tempfile.gettempdir()) / ("openclaw-bicep-" + uuid.uuid4().hex)
    ).resolve()
    if output.exists() or output.is_relative_to(ROOT):
        parser.error("Use a new private output directory outside the checkout")
    for program in ("uv", "node", "npm", "docker"):
        if shutil.which(program) is None:
            parser.error(f"Install {program} and place it on PATH")
    print(f"Private sample state: {output}", flush=True)
    with tempfile.TemporaryDirectory(prefix="openclaw-wheel-") as temporary:
        wheel = download(Path(temporary))
        print(f"{MEASURED}Verified published worker: {WHEEL} SHA-256 {WHEEL_SHA256}", flush=True)
        subprocess.run(
            [str(shutil.which("docker")), "pull", IMAGE],
            check=True,
            timeout=300,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        launch(wheel, output, args.mode)


if __name__ == "__main__":
    main()
