"""Qualify the same source-built bounded-file kernel on each native host."""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

from scripts.experiments.mxc_files_patch.kernel_patch import metadata as kernel_metadata
from scripts.experiments.mxc_session_patch.host_call import digest
from scripts.experiments.mxc_session_patch.qualification_runner import hypervisor
from scripts.experiments.mxc_v1_patch.patch import metadata
from scripts.experiments.mxc_v1_patch.qualification_runner import build, helper, run, sources


def qualify(root: Path, kernel_dir: Path, report: dict) -> None:
    """Build a fresh profile and preserve the original experiment's native-state controls."""
    kernel_report = json.loads((kernel_dir / "kernel-result.json").read_text(encoding="utf-8"))
    kernel = kernel_dir / "elfloader_hyperlight-x86_64"
    if (
        kernel_report["status"] != "built-not-qualified"
        or kernel_report["pins"] != kernel_metadata()
        or digest(kernel) != kernel_report["kernel_sha256"]
        or kernel.stat().st_size != kernel_report["kernel_bytes"]
    ):
        raise ValueError("kernel artifact or source identity differs")
    if os.environ.get("GITHUB_RUN_ID") and kernel_report["run_id"] != os.environ["GITHUB_RUN_ID"]:
        raise ValueError("kernel must come from this workflow run")
    report["kernel"] = kernel_report
    opts = sources(root, metadata())
    baseline = [sys.executable, "-m", "scripts.experiments.mxc_v1_patch.patch"]
    for layer in ("session", "output", "storage"):
        run(root, f"apply-{layer}", *baseline, "apply", "--layer", layer, *opts)
    file_tool = [sys.executable, "-m", "scripts.experiments.mxc_files_patch.patch"]
    run(
        root,
        "configure-files",
        *file_tool,
        "configure",
        *opts,
        "--build-dir",
        str(root / "build"),
    )
    embedded = root / "runtime/kernel/elfloader_hyperlight-x86_64"
    original = root / "original-kernel"
    shutil.copyfile(embedded, original)
    shutil.copyfile(kernel, embedded)
    build(root)
    report["helper_sha256"] = digest(helper(root))
    report["lock_sha256"] = digest(root / "build/Cargo.lock")
    unit = root / ("stream-tests.exe" if sys.platform == "win32" else "stream-tests")
    run(
        root,
        "compile-stream-tests",
        "rustc",
        "+1.98.0",
        "--edition",
        "2024",
        "--test",
        str(root / "runtime/src/stream_capture.rs"),
        "-o",
        str(unit),
    )
    run(root, "stream-tests", str(unit))
    unit = root / ("workspace-tests.exe" if sys.platform == "win32" else "workspace-tests")
    run(
        root,
        "compile-workspace-tests",
        "rustc",
        "+1.98.0",
        "--edition",
        "2024",
        "--test",
        str(root / "runtime/src/workspace.rs"),
        "-o",
        str(unit),
    )
    run(root, "workspace-tests", str(unit))
    run(
        root,
        "prepare-agent",
        sys.executable,
        "-m",
        "scripts.experiments.mxc_v1_patch.prepare_agent",
        "--destination",
        str(root / "images/agent"),
    )
    (root / "images/agent/VERSION").write_text(
        "rootfs: ghcr.io/hyperlight-dev/hyperlight-unikraft/agent:initrd-v0.17.0\n",
        encoding="utf-8",
    )
    run(
        root,
        "native-state",
        sys.executable,
        "-m",
        "scripts.experiments.mxc_v1_patch.native_probe",
        "--helper",
        str(helper(root)),
        "--initrd",
        str(root / "images/agent/initrd.cpio"),
        "--image-home",
        str(root / "images"),
        "--state-dir",
        str(root / "native"),
    )
    report["native_state"] = json.loads((root / "native/result.json").read_text(encoding="utf-8"))
    for module, flag, directory in (
        ("native_probe", "--root", "files"),
        ("durability_probe", "--state-dir", "durability"),
    ):
        run(
            root,
            module,
            sys.executable,
            "-m",
            f"scripts.experiments.mxc_files_patch.{module}",
            "--helper",
            str(helper(root)),
            "--startup",
            str(root / "images/agent/snapshot"),
            flag,
            str(root / directory),
        )
        report[directory] = json.loads(
            (root / directory / "result.json").read_text(encoding="utf-8")
        )
    shutil.copyfile(original, embedded)
    run(root, "remove-files", *file_tool, "remove", *opts)
    run(
        root,
        "remove-streams",
        sys.executable,
        "-m",
        "scripts.experiments.mxc_streams_patch.patch",
        "remove",
        *opts,
    )
    for layer in ("storage", "output", "session"):
        run(root, f"remove-{layer}", *baseline, "remove", "--layer", layer, *opts)
    report["overlays_removed"] = True
    report["status"] = "qualified"


def main() -> int:
    """Retain failures without promoting partial results to native qualification."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--kernel-dir", type=Path, required=True)
    args = parser.parse_args()
    root = args.root.resolve()
    root.mkdir(parents=True, exist_ok=False)
    report = {
        "status": "unqualified",
        "platform": platform.system(),
        "source_commit": os.environ.get("GITHUB_SHA"),
        "run_id": os.environ.get("GITHUB_RUN_ID"),
        "run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT"),
    }
    try:
        report["hypervisor"] = hypervisor()
        qualify(root, args.kernel_dir.resolve(), report)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        report["reason"] = str(error)
        print(str(error), file=sys.stderr)
        return 1
    finally:
        (root / "result.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
