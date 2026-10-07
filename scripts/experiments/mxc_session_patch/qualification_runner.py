"""Build pinned native overlays and retain per-platform format-4 qualification evidence."""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import platform
import subprocess
import sys
from pathlib import Path

from .host_call import digest

ROOT = Path(__file__).resolve().parent


def hypervisor() -> str:
    """Check prerequisites; only the subsequent native probe establishes execution."""
    if platform.machine().lower() not in ("amd64", "x86_64"):
        raise RuntimeError("qualification requires x86-64")
    if sys.platform == "win32":
        api = ctypes.WinDLL("WinHvPlatform.dll")
        api.WHvGetCapability.argtypes = (
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_uint32),
        )
        api.WHvGetCapability.restype = ctypes.c_long
        present, written = ctypes.c_int32(), ctypes.c_uint32()
        result = api.WHvGetCapability(
            0, ctypes.byref(present), ctypes.sizeof(present), ctypes.byref(written)
        )
        if result != 0 or written.value != ctypes.sizeof(present) or present.value != 1:
            raise RuntimeError(
                f"WHP unavailable: HRESULT=0x{result & 0xFFFFFFFF:08x}, present={present.value}"
            )
        return "WHP"
    if sys.platform == "linux":
        import fcntl

        with open("/dev/kvm", "rb", buffering=0) as device:
            if fcntl.ioctl(device.fileno(), 0xAE00) != 12:
                raise RuntimeError("unsupported KVM API")
        return "KVM"
    raise RuntimeError("qualification supports only Linux/KVM and Windows/WHP")


def _run(root: Path, stage: str, *command: str) -> None:
    print(f"MXC qualification: {stage}", flush=True)
    with (root / f"{stage}.log").open("wb") as log:
        child = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, timeout=900)
    if child.returncode:
        raise RuntimeError(f"{stage} failed with exit code {child.returncode}; see {stage}.log")


def _build_and_probe(root: Path, report: dict[str, object]) -> None:
    metadata = json.loads((ROOT / "storage-patch.json").read_text(encoding="utf-8"))
    repos = {
        "source": ("microsoft/mxc", "mxc_base"),
        "runtime": ("hyperlight-dev/hyperlight-unikraft", "runtime_base"),
        "host": ("hyperlight-dev/hyperlight", "host_base"),
    }
    for name, (repo, pin) in repos.items():
        _run(
            root,
            f"clone-{name}",
            "git",
            "clone",
            "--no-checkout",
            f"https://github.com/{repo}.git",
            str(root / name),
        )
        _run(root, f"checkout-{name}", "git", "-C", str(root / name), "checkout", metadata[pin])
    source = ["--source", str(root / "source")]
    runtime = [*source, "--runtime", str(root / "runtime")]
    storage = [*runtime, "--host", str(root / "host")]
    for module, options in (
        ("patch", source),
        ("output_patch", runtime),
        ("storage_patch", storage),
    ):
        _run(root, f"apply-{module}", sys.executable, str(ROOT / f"{module}.py"), "apply", *options)
    build = root / "build"
    _run(
        root,
        "configure",
        sys.executable,
        str(ROOT / "storage_patch.py"),
        "configure",
        *storage,
        "--build-dir",
        str(build),
    )
    os.environ["CARGO_TARGET_DIR"] = str(root / "target")
    _run(
        root,
        "cargo-test",
        "cargo",
        "+1.98.0",
        "test",
        "--locked",
        "--manifest-path",
        str(build / "Cargo.toml"),
    )
    _run(
        root,
        "cargo-build",
        "cargo",
        "+1.98.0",
        "build",
        "--locked",
        "--manifest-path",
        str(build / "Cargo.toml"),
    )
    helper = (
        root
        / "target/debug"
        / ("mxc-session-state-probe.exe" if sys.platform == "win32" else "mxc-session-state-probe")
    )
    _run(
        root,
        "prepare-agent",
        sys.executable,
        str(ROOT / "prepare_agent.py"),
        "--destination",
        str(root / "images/agent"),
    )
    _run(
        root,
        "native-state",
        sys.executable,
        str(ROOT.parent / "mxc_native_state_probe.py"),
        "--helper",
        str(helper),
        "--initrd",
        str(root / "images/agent/initrd.cpio"),
        "--image-home",
        str(root / "images"),
        "--state-dir",
        str(root / "native"),
    )
    startup = root / "images/agent/snapshot"
    for module in ("storage_probe", "durability_probe"):
        _run(
            root,
            module,
            sys.executable,
            "-m",
            f"scripts.experiments.mxc_session_patch.{module}",
            "--helper",
            str(helper),
            "--startup",
            str(startup),
            "--state-dir",
            str(root / module),
        )
    report["helper_sha256"] = digest(helper)
    report["durability"] = json.loads(
        (root / "durability_probe/result.json").read_text(encoding="utf-8")
    )
    report["bounded_export"] = json.loads(
        (root / "storage_probe/result.json").read_text(encoding="utf-8")
    )
    for module, options in (
        ("storage_patch", storage),
        ("output_patch", runtime),
        ("patch", source),
    ):
        _run(
            root, f"remove-{module}", sys.executable, str(ROOT / f"{module}.py"), "remove", *options
        )
        _run(root, f"check-{module}", sys.executable, str(ROOT / f"{module}.py"), "check", *options)
    report["overlays_removed"] = True


def main() -> int:
    """Refuse unavailable platforms and retain evidence even when qualification fails."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--preflight", action="store_true")
    args = parser.parse_args()
    root = args.root.resolve()
    report: dict[str, object] = {
        "status": "unqualified",
        "platform": platform.system(),
        "architecture": platform.machine(),
        "source_commit": os.environ.get("GITHUB_SHA"),
        "run_id": os.environ.get("GITHUB_RUN_ID"),
        "run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT"),
        "pins": json.loads((ROOT / "storage-patch.json").read_text(encoding="utf-8")),
    }
    if args.preflight:
        root.mkdir(parents=True, exist_ok=False)
    elif not root.is_dir() or (root / "result.json").exists():
        parser.error("run requires an existing directory without a result")
    try:
        report["hypervisor"] = hypervisor()
        if not args.preflight:
            _build_and_probe(root, report)
            report["status"] = "qualified"
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        report["reason"] = str(error)
        print(str(error), file=sys.stderr)
        return 1
    finally:
        name = "preflight.json" if args.preflight else "result.json"
        (root / name).write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
