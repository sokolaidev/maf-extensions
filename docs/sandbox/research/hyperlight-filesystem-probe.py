# /// script
# requires-python = ">=3.12,<3.15"
# dependencies = [
#     "hyperlight-sandbox==0.7.0",
#     "hyperlight-sandbox-backend-wasm==0.7.0",
#     "hyperlight-sandbox-python-guest==0.7.0",
# ]
# ///
"""Probe Hyperlight's writable-file lifetime in a bounded, disposable worker.

Run with ``uv run --script <this-file>`` on a supported x86-64 hypervisor host.
JSON describes observed behavior; success means the probe completed, not conformance.
``--native-default`` compares Windows' default surrogate mode with one VM per process.
"""

from __future__ import annotations

import argparse
import ctypes
import importlib
import json
import os
import platform
import subprocess
import sys
import tempfile
from importlib.metadata import version
from pathlib import Path

_PACKAGES = (
    "hyperlight-sandbox",
    "hyperlight-sandbox-backend-wasm",
    "hyperlight-sandbox-python-guest",
)


def _emit(probe: str, **values: object) -> None:
    print(json.dumps({"probe": probe, **values}), flush=True)


def _worker(root: Path) -> None:
    # Keep the DLL loaded for the lifetime of the WHP guest.
    _whp = ctypes.WinDLL("WinHvPlatform.dll") if sys.platform == "win32" else None
    sdk = importlib.import_module("hyperlight_sandbox")
    input_root, output_root = root / "input", root / "output"
    input_root.mkdir()
    output_root.mkdir()
    sandbox = sdk.Sandbox(input_dir=str(input_root), output_dir=str(output_root))

    def execute(probe: str, code: str) -> None:
        result = sandbox.run(code)
        _emit(probe, stdout=result.stdout, stderr=result.stderr, exit_code=result.exit_code)

    execute("startup", 'print("hello")')
    baseline = sandbox.snapshot()
    (input_root / "source.txt").write_text("staged input", encoding="utf-8")
    (output_root / "staged.txt").write_text("staged writable", encoding="utf-8")
    execute(
        "staged-files",
        'print(open("/input/source.txt").read()); print(open("/output/staged.txt").read())',
    )
    _emit("staged-output-survived", exists=(output_root / "staged.txt").exists())
    execute("input-writable", 'open("/input/source.txt", "w").write("edit")')
    execute(
        "write-output",
        'open("/output/created.txt", "w").write("created"); x=42; print("wrote")',
    )
    _emit("collected-output", content=(output_root / "created.txt").read_text(encoding="utf-8"))
    execute("warm-reuse", 'print("x" in globals()); print(open("/output/created.txt").read())')
    sandbox.restore(baseline)
    _emit(
        "reset-files",
        input_exists=(input_root / "source.txt").exists(),
        output_names=sorted(path.name for path in output_root.iterdir()),
    )
    execute("reset-state", 'print("x" in globals())')
    execute("guest-error", 'raise ValueError("expected")')
    execute("after-error", 'print("reused")')


def main() -> int:
    """Run the native probe outside the process that owns cleanup and the deadline."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--native-default", action="store_true")
    parser.add_argument("--worker-root", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker_root is not None:
        _worker(args.worker_root)
        return 0

    environment = os.environ.copy()
    if args.native_default:
        environment.pop("HYPERLIGHT_MAX_SURROGATES", None)
    elif sys.platform == "win32":
        environment["HYPERLIGHT_MAX_SURROGATES"] = "0"
    _emit(
        "environment",
        python=platform.python_version(),
        system=platform.system(),
        release=platform.release(),
        machine=platform.machine(),
        versions={name: version(name) for name in _PACKAGES},
        max_surrogates=environment.get("HYPERLIGHT_MAX_SURROGATES"),
    )
    # The native objects hold directory handles until their worker exits.
    with tempfile.TemporaryDirectory(prefix="maf-hyperlight-probe-") as root:
        try:
            result = subprocess.run(
                [sys.executable, "-u", str(Path(__file__).resolve()), "--worker-root", root],
                env=environment,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=45,
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
            )
        except subprocess.TimeoutExpired:
            _emit("worker-timeout", seconds=45)
            return 1
        print(result.stdout, end="")
        if result.returncode:
            # Keep the failure without publishing the interpreter's host-side traceback paths.
            detail = result.stderr.strip().splitlines()
            _emit("worker-failure", exit_code=result.returncode, detail=detail[-1:] or [])
        return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
