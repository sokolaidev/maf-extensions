"""Qualify the MXC 1.0 overlays and old-checkpoint refusal on GitHub native runners."""

from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import sys
from pathlib import Path

from scripts.experiments.mxc_session_patch.host_call import digest
from scripts.experiments.mxc_session_patch.qualification_runner import hypervisor
from scripts.experiments.mxc_v1_patch.patch import BASELINE, ROOT, metadata


def run(root: Path, stage: str, *command: str, expected: int = 0) -> None:
    """Keep each bounded command's diagnostics separate and require its expected outcome."""
    print(f"MXC 1.0: {stage}", flush=True)
    with (root / f"{stage}.log").open("wb") as log:
        child = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, timeout=1800)
    if child.returncode != expected:
        raise RuntimeError(f"{stage}: expected exit {expected}, got {child.returncode}; see log")


def sources(root: Path, pins: dict) -> list[str]:
    """Clone exact public upstream revisions into a fresh experiment directory."""
    options = []
    for name, repo, pin in (
        ("source", "microsoft/mxc", "mxc_base"),
        ("runtime", "hyperlight-dev/hyperlight-unikraft", "runtime_base"),
        ("host", "hyperlight-dev/hyperlight", "host_base"),
    ):
        run(
            root,
            f"clone-{name}",
            "git",
            "clone",
            "--no-checkout",
            f"https://github.com/{repo}.git",
            str(root / name),
        )
        run(root, f"checkout-{name}", "git", "-C", str(root / name), "checkout", pins[pin])
        options.extend((f"--{name}", str(root / name)))
    return options


def helper(root: Path) -> Path:
    """Name the current platform's isolated native helper."""
    return (
        root
        / "target/debug"
        / ("mxc-session-state-probe.exe" if sys.platform == "win32" else "mxc-session-state-probe")
    )


def build(root: Path) -> None:
    """Require the retained lockfile without modifying registry sources."""
    os.environ["CARGO_TARGET_DIR"] = str(root / "target")
    for action in ("test", "build"):
        run(
            root,
            f"cargo-{action}",
            "cargo",
            "+1.98.0",
            action,
            "--locked",
            "--manifest-path",
            str(root / "build/Cargo.toml"),
        )


def baseline(root: Path) -> Path:
    """Produce an old checkpoint and retain the old helper as a recovery control."""
    root.mkdir()
    pins = json.loads((BASELINE / "storage-patch.json").read_text(encoding="utf-8"))
    opts = sources(root, pins)
    for module, args in (("patch", opts[:2]), ("output_patch", opts[:4]), ("storage_patch", opts)):
        run(root, f"apply-{module}", sys.executable, str(BASELINE / f"{module}.py"), "apply", *args)
    run(
        root,
        "configure",
        sys.executable,
        str(BASELINE / "storage_patch.py"),
        "configure",
        *opts,
        "--build-dir",
        str(root / "build"),
    )
    build(root)
    run(
        root,
        "prepare-agent",
        sys.executable,
        str(BASELINE / "prepare_agent.py"),
        "--destination",
        str(root / "images/agent"),
    )
    run(
        root,
        "native-state",
        sys.executable,
        str(BASELINE.parent / "mxc_native_state_probe.py"),
        "--helper",
        str(helper(root)),
        "--initrd",
        str(root / "images/agent/initrd.cpio"),
        "--image-home",
        str(root / "images"),
        "--state-dir",
        str(root / "native"),
    )
    return helper(root)


def qualify(root: Path, report: dict, mode: str) -> None:
    """Keep dependency resolution separate from locked native qualification."""
    opts = sources(root, metadata())
    tool = [sys.executable, "-m", "scripts.experiments.mxc_v1_patch.patch"]
    for layer in ("session", "output", "storage"):
        run(root, f"apply-{layer}", *tool, "apply", "--layer", layer, *opts)
    run(root, "configure", *tool, "configure", *opts, "--build-dir", str(root / "build"))
    if mode == "resolve-lock":
        run(
            root,
            "resolve-lock",
            "cargo",
            "+1.98.0",
            "update",
            "--workspace",
            "--manifest-path",
            str(root / "build/Cargo.toml"),
        )
        report["status"] = "dependencies-resolved"
        return
    build(root)
    report["helper_sha256"] = digest(helper(root))
    report["lock_sha256"] = digest(ROOT / "Cargo.lock")
    for name, source in (
        ("capture", root / "runtime/src/output_capture.rs"),
        (
            "storage-budget",
            root / "host/src/hyperlight_host/src/sandbox/snapshot/file/write_budget.rs",
        ),
    ):
        executable = root / (f"{name}-tests.exe" if sys.platform == "win32" else f"{name}-tests")
        run(
            root,
            f"compile-{name}-tests",
            "rustc",
            "+1.98.0",
            "--edition",
            "2024",
            "--test",
            str(source),
            "-o",
            str(executable),
        )
        run(root, f"{name}-tests", str(executable))
    run(
        root,
        "prepare-agent",
        sys.executable,
        "-m",
        "scripts.experiments.mxc_v1_patch.prepare_agent",
        "--destination",
        str(root / "images/agent"),
    )
    # MXC uses the stamp to prevent mixing a release's kernel and rootfs.
    (root / "images/agent/VERSION").write_text("initrd-v0.17.0", encoding="utf-8")
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
    startup = root / "images/agent/snapshot"
    for module in ("bounded_probe", "storage_probe", "durability_probe"):
        entry = (
            [str(BASELINE / "bounded_probe.py")]
            if module == "bounded_probe"
            else ["-m", f"scripts.experiments.mxc_session_patch.{module}"]
        )
        run(
            root,
            module,
            sys.executable,
            *entry,
            "--helper",
            str(helper(root)),
            "--startup",
            str(startup),
            "--state-dir",
            str(root / module),
        )
        report[module] = json.loads((root / module / "result.json").read_text(encoding="utf-8"))
    old = root / "baseline"
    old_helper = baseline(old)
    checkpoint = old / "native/checkpoint"
    before = {
        p.relative_to(checkpoint).as_posix(): digest(p)
        for p in checkpoint.rglob("*")
        if p.is_file()
    }
    refused_report = root / "old-to-new.json"
    run(
        root,
        "old-to-new",
        str(helper(root)),
        "restore",
        str(checkpoint),
        str(refused_report),
        expected=1,
    )
    diagnostic = (root / "old-to-new.log").read_text(encoding="utf-8")
    if refused_report.exists() or "kernel or host" not in diagnostic:
        raise RuntimeError("old-to-new failed without the expected compatibility refusal")
    after = {
        p.relative_to(checkpoint).as_posix(): digest(p)
        for p in checkpoint.rglob("*")
        if p.is_file()
    }
    if before != after:
        raise RuntimeError("new helper changed the old checkpoint")
    run(
        root,
        "old-helper-recovery",
        str(old_helper),
        "restore",
        str(checkpoint),
        str(root / "old-helper-recovery.json"),
    )
    report["compatibility"] = {
        "old_to_new": "refused-incompatible",
        "old_checkpoint_unchanged": True,
        "old_helper_recovery": True,
        "old_helper_sha256": digest(old_helper),
        "refusal_kind": "snapshot kernel or host contract mismatch",
    }
    for layer in ("storage", "output", "session"):
        run(root, f"remove-{layer}", *tool, "remove", "--layer", layer, *opts)
        run(root, f"check-{layer}", *tool, "check", "--layer", layer, *opts)
    report["overlays_removed"] = True
    report["status"] = "qualified"


def main() -> int:
    """Retain an unqualified report on failure; lock resolution never qualifies native behavior."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--mode", choices=("resolve-lock", "qualify"), default="qualify")
    args = parser.parse_args()
    root = args.root.resolve()
    root.mkdir(parents=True, exist_ok=False)
    report = {
        "status": "unqualified",
        "mode": args.mode,
        "platform": platform.system(),
        "source_commit": os.environ.get("GITHUB_SHA"),
        "run_id": os.environ.get("GITHUB_RUN_ID"),
        "run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT"),
        "pins": metadata(),
        "rootfs": json.loads((ROOT / "rootfs.json").read_text(encoding="utf-8")),
    }
    try:
        if args.mode == "qualify":
            report["hypervisor"] = hypervisor()
        qualify(root, report, args.mode)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        report["reason"] = str(error)
        print(str(error), file=sys.stderr)
        return 1
    finally:
        (root / "result.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
