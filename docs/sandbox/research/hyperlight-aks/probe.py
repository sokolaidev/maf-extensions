"""Measure packaged SDK prerequisites and KVM execution; this is not adapter conformance."""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
import platform
import stat
from importlib.metadata import version
from pathlib import Path


def record(stage: str, **fields: object) -> None:
    """Emit one evidence record without copying the process environment."""
    print(json.dumps({"stage": stage, **fields}), flush=True)


def packages() -> None:
    """Load the native wheel and materialize the guest as the application user."""
    if platform.system() != "Linux" or platform.machine() != "x86_64":
        raise RuntimeError("this proof requires Linux x86-64")
    if os.getuid() != 65534 or os.getgid() != 65534:
        raise RuntimeError("run the proof with UID/GID 65534")
    for directory in ("/work/cache", "/work/tmp"):
        Path(directory).mkdir(parents=True, exist_ok=True, mode=0o700)
    versions = {
        name: version(name)
        for name in (
            "hyperlight-sandbox",
            "hyperlight-sandbox-backend-wasm",
            "hyperlight-sandbox-python-guest",
        )
    }
    if set(versions.values()) != {"0.7.0"}:
        raise RuntimeError(f"requires the exact 0.7.0 trio: {versions}")
    importlib.import_module("hyperlight_sandbox_backend_wasm._native_wasm")
    guest = importlib.import_module("python_guest.path")
    module = Path(guest.get_module_path())
    record(
        "packages",
        result="pass",
        python=platform.python_version(),
        kernel=platform.release(),
        libc=platform.libc_ver(),
        architecture=platform.machine(),
        uid=os.getuid(),
        gid=os.getgid(),
        versions=versions,
        guest_bytes=module.stat().st_size,
        guest_sha256=hashlib.sha256(module.read_bytes()).hexdigest(),
    )


def device() -> None:
    """Require KVM VM creation, not only a visible device node."""
    fcntl = importlib.import_module("fcntl")
    if Path("/dev/mshv").exists():
        raise RuntimeError("KVM proof requires /dev/mshv to be absent")
    info = Path("/dev/kvm").stat()
    if not stat.S_ISCHR(info.st_mode):
        raise RuntimeError("/dev/kvm is not a character device")
    with open("/dev/kvm", "rb+", buffering=0) as kvm:
        api = fcntl.ioctl(kvm.fileno(), 0xAE00, 0)  # KVM_GET_API_VERSION
        if api != 12:
            raise RuntimeError(f"unexpected KVM API version: {api}")
        vm = fcntl.ioctl(kvm.fileno(), 0xAE01, 0)  # KVM_CREATE_VM
        os.close(vm)
    record("device", result="pass", api=api, uid=info.st_uid, gid=info.st_gid)


def sdk() -> None:
    """Check execution, ordinary failure reuse and restored globals on one native thread."""
    constructor = importlib.import_module("hyperlight_sandbox").Sandbox
    sandbox = constructor(
        backend="wasm", module="python_guest.path", heap_size="400Mi", stack_size="200Mi"
    )

    def run(code: str, expected: str) -> None:
        result = sandbox.run(code)
        if result.exit_code != 0 or result.stdout.strip() != expected or result.stderr:
            raise RuntimeError("guest result did not match the expected output")

    run("pass", "")
    baseline = sandbox.snapshot()
    run("value = 42; print(value)", "42")
    run("print(value)", "42")
    result = sandbox.run("raise ValueError('proof-error')")
    if result.exit_code == 0 or "proof-error" not in result.stderr:
        raise RuntimeError("ordinary guest exception was not returned")
    run("print(value + 1)", "43")
    sandbox.restore(baseline)
    run("print('value' in globals())", "False")
    record("sdk", result="pass", hypervisor="kvm", adapter_conformance=False)


def main() -> None:
    """Stop on the first failed stage, preserving earlier stages as separate evidence."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", choices=("packages", "device", "sdk"), default="sdk")
    args = parser.parse_args()
    stage = "packages"
    try:
        packages()
        if args.stage in ("device", "sdk"):
            stage = "device"
            device()
        if args.stage == "sdk":
            stage = "sdk"
            sdk()
    except Exception as error:
        record(stage, result="fail", error_type=type(error).__name__, detail=str(error)[:1024])
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
