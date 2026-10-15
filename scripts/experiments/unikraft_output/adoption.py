"""Assess upstream capture against retained MXC checkpoints and host publication rules."""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import sys
from pathlib import Path

from scripts.experiments.mxc_native_state_probe import digest
from scripts.experiments.mxc_session_patch.host_store import Refused
from scripts.experiments.mxc_session_patch.shared_store import Limits, SharedStore
from scripts.experiments.mxc_streams_patch.transport import FORMAT, STREAM_LIMIT, read_result
from scripts.experiments.mxc_v1_patch import qualification_runner as retained
from scripts.experiments.unikraft_output.qualify import PIN, _execute

ROOT = Path(__file__).resolve().parent


def inventory(root: Path) -> dict[str, str]:
    """Fingerprint every checkpoint file before and after a refused load."""
    return {p.relative_to(root).as_posix(): digest(p) for p in root.rglob("*") if p.is_file()}


def prepare(root: Path) -> Path:
    """Build the retained MXC 1.0 helper and produce a real rich-state checkpoint."""
    root.mkdir()
    opts = retained.sources(root, retained.metadata())
    tool = [sys.executable, "-m", "scripts.experiments.mxc_v1_patch.patch"]
    for layer in ("session", "output", "storage"):
        retained.run(root, f"apply-{layer}", *tool, "apply", "--layer", layer, *opts)
    retained.run(root, "configure", *tool, "configure", *opts, "--build-dir", str(root / "build"))
    previous_target = os.environ.get("CARGO_TARGET_DIR")
    try:
        retained.build(root)
    finally:
        if previous_target is None:
            os.environ.pop("CARGO_TARGET_DIR", None)
        else:
            os.environ["CARGO_TARGET_DIR"] = previous_target
    retained.run(
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
    retained.run(
        root,
        "native-state",
        sys.executable,
        "-m",
        "scripts.experiments.mxc_v1_patch.native_probe",
        "--helper",
        str(retained.helper(root)),
        "--initrd",
        str(root / "images/agent/initrd.cpio"),
        "--image-home",
        str(root / "images"),
        "--state-dir",
        str(root / "native"),
    )
    return root / "native/checkpoint"


def publication(root: Path, observation: dict) -> dict:
    """Test host refusal/replay with native-measured metadata and synthetic checkpoint bytes."""
    if observation != {
        "case": "byte_prefix",
        "retained_bytes": STREAM_LIMIT - 1,
        "omitted_bytes": 3,
        "limit_bytes": STREAM_LIMIT,
    }:
        raise ValueError("unexpected native byte-prefix observation")
    root.mkdir()
    checkpoint = root / "checkpoint"
    checkpoint.mkdir()
    (checkpoint / "index.json").write_bytes(b"synthetic checkpoint fixture")
    profile = {"runtime": "host-boundary-fixture", "policy": "closed", "machine": "local"}
    limits = Limits(1_000_000, 2_000_000, checkpoint_bytes=1024, result_bytes=128, files=2)
    control = root / "native.json"
    metadata = {
        "retained_bytes": observation["retained_bytes"],
        "omitted_bytes": observation["omitted_bytes"],
        "omitted_bytes_saturated": False,
    }
    cases = {
        "guest-control-text": {"completed": True},
        "unicode-byte-prefix": {
            "format": FORMAT,
            "completed": True,
            "checkpoint": True,
            "limit_bytes": STREAM_LIMIT,
            "streams": {"stdout": metadata, "stderr": metadata},
        },
    }
    refusals = {}
    for name, data in cases.items():
        with SharedStore(root / "store", name, profile, limits) as store:
            assert store.begin("saved", b"saved") is None
            store.commit("saved", checkpoint, b"retained result bytes")
            assert store.begin(name, name.encode()) is None
            control.write_text(json.dumps(data), encoding="utf-8")
            try:
                result = read_result(control, True)
            except Refused as error:
                refusals[name] = str(error)
            else:
                store.commit(name, checkpoint, result)
                raise AssertionError("incompatible completion reached publication")
        with SharedStore(root / "store", name, profile, limits) as store:
            assert store.begin("saved", b"saved") == b"retained result bytes"
            restored = root / f"restored-{name}"
            assert store.restore(restored) == "saved"
            assert inventory(restored) == inventory(checkpoint)
            try:
                store.begin(name, name.encode())
            except Refused:
                pass
            else:
                raise AssertionError("interrupted identity allowed re-execution")
    return {
        "scope": "host-boundary controls; synthetic checkpoint bytes; native-measured metadata",
        "refusals": refusals,
        "saved_result_replayed": True,
        "previous_checkpoint_preserved": True,
        "interrupted_retry_refused": True,
    }


def assess(source: Path, output: Path, root: Path, report: dict) -> None:
    """Require same-run upstream qualification before testing the retained checkpoint."""
    prior = json.loads((output / "result.json").read_text(encoding="utf-8"))
    if any(
        not report[key] or prior.get(key) != report[key]
        for key in ("source_commit", "run_id", "run_attempt")
    ) or (prior.get("status") != "measured" or prior.get("upstream_commit") != PIN):
        raise ValueError("missing same-run upstream measurements")
    report["upstream_report_sha256"] = digest(output / "result.json")
    report["hypervisor"] = retained.hypervisor()
    checkpoint = prepare(root)
    before = inventory(checkpoint)
    report["retained"] = {
        "pins": retained.metadata(),
        "lock_sha256": digest(root / "build/Cargo.lock"),
        "kernel_sha256": digest(root / "runtime/kernel/elfloader_hyperlight-x86_64"),
        "native": json.loads((root / "native/result.json").read_text(encoding="utf-8")),
    }
    target = source / "tests/maf_adoption_assessment.rs"
    if target.exists():
        raise ValueError("assessment test already exists")
    shutil.copyfile(ROOT / "adoption.rs", target)
    report["probe_sha256"] = digest(target)
    previous_checkpoint = os.environ.get("MXC_OLD_CHECKPOINT")
    os.environ["MXC_OLD_CHECKPOINT"] = str(checkpoint)
    try:
        log = _execute(
            source,
            output,
            "adoption",
            [
                "cargo",
                "test",
                "--locked",
                "--test",
                "maf_adoption_assessment",
                "--",
                "--nocapture",
                "--test-threads=1",
            ],
            3,
        )
    finally:
        target.unlink()
        if previous_checkpoint is None:
            os.environ.pop("MXC_OLD_CHECKPOINT", None)
        else:
            os.environ["MXC_OLD_CHECKPOINT"] = previous_checkpoint
    observations = [
        json.loads(line.split("OBSERVATION ", 1)[1])
        for line in log.splitlines()
        if "OBSERVATION " in line
    ]
    if len(observations) != 3 or {item["case"] for item in observations} != {
        "old_checkpoint",
        "guest_failure",
        "byte_prefix",
    }:
        raise ValueError("missing or duplicate native observations")
    report["observations"] = observations
    if before != inventory(checkpoint):
        raise ValueError("upstream changed the retained checkpoint")
    retained.run(
        root,
        "old-helper-recovery",
        str(retained.helper(root)),
        "restore",
        str(checkpoint),
        str(root / "recovery.json"),
    )
    recovery = json.loads((root / "recovery.json").read_text(encoding="utf-8"))
    if recovery.get("restored_state_verified") is not True or before != inventory(checkpoint):
        raise ValueError("retained helper failed to recover unchanged state")
    report["compatibility"] = {"old_checkpoint_unchanged": True, "old_helper_recovery": recovery}
    report["publication"] = publication(
        root / "publication", next(item for item in observations if item["case"] == "byte_prefix")
    )
    report["test_binaries"] = {
        p.name: digest(p)
        for p in (source / "target/debug/deps").glob("maf_adoption_assessment-*")
        if p.suffix == (".exe" if os.name == "nt" else "")
    }
    if not report["test_binaries"] or digest(source / "Cargo.lock") != prior["cargo_lock_sha256"]:
        raise ValueError("missing executable or changed dependency lock")
    report.update(
        status="measured",
        adoption="defer-overlay-removal",
        not_qualified=[
            "migrated MXC helper",
            "native publication/crash matrix on the new runtime",
            "timeout/cancellation/owner-loss on the new runtime",
            "checkpoint conversion",
            "production acceptance",
        ],
    )


def main() -> None:
    """Retain diagnostics on failure and exclude guest checkpoint payloads from evidence."""
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("source", "output", "root"):
        parser.add_argument(f"--{name}", type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve(strict=True)
    root = args.root.resolve()
    report = {
        "status": "unqualified",
        "platform": platform.system(),
        **{
            key: os.environ.get(env)
            for key, env in (
                ("source_commit", "GITHUB_SHA"),
                ("run_id", "GITHUB_RUN_ID"),
                ("run_attempt", "GITHUB_RUN_ATTEMPT"),
            )
        },
    }
    try:
        assess(args.source.resolve(strict=True), output, root, report)
    except Exception as error:
        report["error"] = str(error)
        raise
    finally:
        if root.exists():
            for path in root.glob("*.log"):
                shutil.copyfile(path, output / f"retained-{path.name}")
        (output / "adoption-result.json").write_text(
            json.dumps(report, indent=2) + "\n", encoding="utf-8"
        )


if __name__ == "__main__":
    main()
