"""Measure pinned upstream output behavior without replacing an MXC profile."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
from pathlib import Path

PIN = "cf458a07ed847f06d78284998a5beb13dc9fb426"


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _execute(source: Path, output: Path, name: str, args: list[str], count: int) -> str:
    result = subprocess.run(
        args,
        cwd=source,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=1800,
        check=False,
    )
    log = result.stdout + result.stderr
    (output / f"{name}.log").write_text(log, encoding="utf-8")
    if result.returncode or not re.search(
        rf"test result: ok\. {count} passed; 0 failed; 0 ignored;", log
    ):
        raise RuntimeError(f"{name} failed or did not execute exactly {count} tests")
    return log


def _qualify(source: Path, output: Path, report: dict) -> None:
    actual = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=source, text=True).strip()
    if actual != PIN:
        raise ValueError("upstream source commit differs")
    changes = subprocess.check_output(
        ["git", "status", "--porcelain", "--untracked-files=no"], cwd=source, text=True
    )
    if changes:
        raise ValueError("upstream tracked source is modified")
    rootfs = source / "build-elfloader/python-rootfs.cpio"
    build = json.loads((rootfs.parent / "rootfs-build.json").read_text("utf-8"))
    if (
        build["upstream_commit"] != PIN
        or build["rootfs_sha256"] != _digest(rootfs)
        or build["run_id"] != os.environ.get("GITHUB_RUN_ID")
        or build["run_attempt"] != os.environ.get("GITHUB_RUN_ATTEMPT")
    ):
        raise ValueError("rootfs identity or producing workflow run differs")
    probe = Path(__file__).with_name("probe.rs")
    target = source / "tests/maf_output_qualification.rs"
    if target.exists():
        raise ValueError("qualification test already exists")
    report.update(
        upstream_commit=actual,
        rootfs=build,
        kernel_sha256=_digest(source / "kernel/elfloader_hyperlight-x86_64"),
        cargo_lock_sha256=_digest(source / "Cargo.lock"),
        probe_sha256=_digest(probe),
        rustc=subprocess.check_output(["rustc", "--version"], cwd=source, text=True).strip(),
    )
    shutil.copyfile(probe, target)
    try:
        stages = [
            ("capture", ["--lib", "capture_"], 5),
            ("limits", ["--test", "python", "python_output_limit"], 3),
            ("echo", ["--test", "python", "python_echo_output"], 1),
            ("comparison", ["--test", "maf_output_qualification"], 7),
        ]
        for name, args, count in stages:
            log = _execute(
                source,
                output,
                name,
                ["cargo", "test", "--locked", *args, "--", "--nocapture", "--test-threads=1"],
                count,
            )
            report.setdefault("stages", {})[name] = {
                "passed": count,
                "log_sha256": _digest(output / f"{name}.log"),
            }
            if name == "comparison":
                report["observations"] = [
                    json.loads(line.split("OBSERVATION ", 1)[1])
                    for line in log.splitlines()
                    if "OBSERVATION " in line
                ]
        binaries = source / "target/debug/deps"
        report["test_binaries"] = {
            path.name: _digest(path)
            for path in binaries.glob("maf_output_qualification-*")
            if path.suffix == (".exe" if os.name == "nt" else "")
        }
        if not report["test_binaries"]:
            raise ValueError("native comparison executable not found")
        if _digest(source / "Cargo.lock") != report["cargo_lock_sha256"]:
            raise ValueError("dependency lock changed")
        report["status"] = "measured"
        report["adoption"] = "defer-overlay-removal"
        report["not_qualified"] = [
            "MXC integration",
            "durable publication and replay on this runtime",
            "old checkpoint restore",
            "timeout/cancellation/owner-loss matrix",
            "production acceptance",
        ]
    finally:
        report["test_binaries"] = {
            path.name: _digest(path)
            for prefix in ("maf_output_qualification-", "python-", "hyperlight_unikraft-")
            for path in (source / "target/debug/deps").glob(prefix + "*")
            if path.suffix == (".exe" if os.name == "nt" else "")
        }
        shutil.copyfile(source / "Cargo.lock", output / "Cargo.lock")
        target.unlink()


def main() -> None:
    """Retain an explicit unqualified result on every failed stage."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    report = {
        "status": "unqualified",
        "platform": platform.system(),
        "source_commit": os.environ.get("GITHUB_SHA"),
        "run_id": os.environ.get("GITHUB_RUN_ID"),
        "run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT"),
    }
    try:
        _qualify(args.source.resolve(), output, report)
    except Exception as exc:
        report["error"] = str(exc)
        raise
    finally:
        (output / "result.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
