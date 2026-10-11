"""Qualify native publication and recovery after actual disposable-volume exhaustion."""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import sqlite3
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path

from scripts.experiments.mxc_files_patch import physical_probe as native
from scripts.experiments.mxc_session_patch import volume_probe as volume_control
from scripts.experiments.mxc_session_patch.host_call import atomic_report, digest
from scripts.experiments.mxc_session_patch.physical import Policy
from scripts.experiments.mxc_session_patch.shared_store import SharedStore

VOLUME_BYTES = 8 * 1024**3
POLICY = Policy(1024**3, 128 * 1024**2, 64 * 1024**2)


def worker(helper: Path, startup: Path, state: Path, volume: Path, stage: str) -> None:
    """Reuse native recovery assertions with an actual post-admission filesystem race."""

    class ExhaustingStore(SharedStore):
        def _capture(self, call_id: str, root: Path) -> int:
            if call_id == "overflow":
                # commit has verified helper exit and real headroom before capture.
                evidence = volume_control.fill(volume, maximum_bytes=VOLUME_BYTES)
                assert self.db.in_transaction
                atomic_report(state / "exhaustion.json", {**evidence})
            return super()._capture(call_id, root)

    native.worker(helper, startup, state, stage, root=volume / "store", store_type=ExhaustingStore)
    record = json.loads((state / f"{stage}.json").read_bytes())
    if stage == "overflow":
        assert record["after"]["database_bytes"] < POLICY.database_bytes
        record["filesystem"] = json.loads((state / "exhaustion.json").read_bytes())
    atomic_report(state / f"{stage}.json", record)


def launch(helper: Path, startup: Path, state: Path, volume: Path, token: str, stage: str) -> dict:
    """Require a fresh supervisor to complete all native and filesystem assertions."""
    with (state / f"{stage}.log").open("wb") as log:
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "scripts.experiments.mxc_files_patch.volume_probe",
                "--helper",
                str(helper),
                "--startup",
                str(startup),
                "--state-dir",
                str(state),
                "--volume",
                str(volume),
                "--token",
                token,
                "--worker",
                stage,
            ],
            stdout=log,
            stderr=subprocess.STDOUT,
            timeout=600,
        )
    assert result.returncode == 0, (stage, result.returncode)
    return json.loads((state / f"{stage}.json").read_bytes())


def qualify(helper: Path, startup: Path, state: Path, volume: Path, token: str) -> dict:
    """Keep diagnostic state outside the bounded filesystem and recover after freeing it."""
    state.mkdir(parents=True, exist_ok=False)
    volume_control.validate_volume(volume, state, token, maximum_bytes=VOLUME_BYTES)
    filler = volume / "filler.bin"
    if filler.exists():
        raise ValueError("volume already contains a filler")
    (state / "payload.bin").write_bytes(os.urandom(native.PAYLOAD_BYTES))
    atomic_report(state / "policy.json", asdict(POLICY))
    records: dict[str, dict] = {}
    report = {
        "qualified": False,
        "scope": "native publication after export and helper termination",
        "source_commit": os.environ.get("GITHUB_SHA"),
        "run_id": os.environ.get("GITHUB_RUN_ID"),
        "run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT"),
        "platform": platform.system(),
        "python": platform.python_version(),
        "sqlite": sqlite3.sqlite_version,
        "helper_sha256": digest(helper),
        "volume_bytes": shutil.disk_usage(volume).total,
        "policy": asdict(POLICY),
        "supervisors": records,
    }
    try:
        records["seed"] = launch(helper, startup, state, volume, token, "seed")
        try:
            records["overflow"] = launch(helper, startup, state, volume, token, "overflow")
        finally:
            volume_control.validate_volume(volume, state, token, maximum_bytes=VOLUME_BYTES)
            filler.unlink(missing_ok=True)
        records["recover"] = launch(helper, startup, state, volume, token, "recover")
        assert (
            records["seed"]["seed_sha256"] == records["recover"]["replay_after_collection_sha256"]
        )
        report["qualified"] = True
    finally:
        atomic_report(state / "result.json", report)
    return report


def main() -> int:
    """Run the bounded-volume matrix or one isolated native supervisor."""
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("helper", "startup", "state-dir", "volume"):
        parser.add_argument(f"--{name}", type=Path, required=True)
    parser.add_argument("--token", required=True)
    parser.add_argument("--worker", choices=("seed", "overflow", "recover"))
    args = parser.parse_args()
    if args.worker:
        volume_control.validate_volume(
            args.volume, args.state_dir, args.token, maximum_bytes=VOLUME_BYTES
        )
        worker(args.helper, args.startup, args.state_dir, args.volume, args.worker)
    else:
        qualify(args.helper, args.startup, args.state_dir, args.volume, args.token)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
