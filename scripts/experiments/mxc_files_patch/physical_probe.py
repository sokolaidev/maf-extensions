"""Qualify native format-6 publication rollback and recovery at the SQLite ceiling."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import sqlite3
import subprocess
import sys
import zlib
from dataclasses import asdict, replace
from pathlib import Path

from scripts.experiments.mxc_files_patch.deletion_probe import LIMITS
from scripts.experiments.mxc_files_patch.deletion_probe import SCRATCH as BASE_SCRATCH
from scripts.experiments.mxc_files_patch.durability_probe import console
from scripts.experiments.mxc_files_patch.native_probe import IO
from scripts.experiments.mxc_files_patch.request import FileLimits, Input, Request, WorkspaceLimits
from scripts.experiments.mxc_files_patch.shared_call import call
from scripts.experiments.mxc_session_patch.durability_probe import _profile_for
from scripts.experiments.mxc_session_patch.host_call import atomic_report, digest
from scripts.experiments.mxc_session_patch.host_store import Refused
from scripts.experiments.mxc_session_patch.native_journal import NativeJournal
from scripts.experiments.mxc_session_patch.physical import VERSION, Policy
from scripts.experiments.mxc_session_patch.process_identity import Identity, stopped
from scripts.experiments.mxc_session_patch.shared_store import SharedStore

MARGIN = 8 * 1024**2
PAYLOAD_BYTES = 64 * 1024**2
SCRATCH = replace(
    BASE_SCRATCH,
    bytes=BASE_SCRATCH.bytes + 48 * 1024**2,
    store_bytes=BASE_SCRATCH.store_bytes + 96 * 1024**2,
)
SEED = Request(
    IO
    + b"physical_value=1; write_file('/workspace/session/value',b'1'); write_file('result.bin',b'1'); print(1)",
    (),
    ("result.bin",),
    FileLimits(),
)
VERIFY = Request(
    IO
    + b"""assert physical_value == 1
assert read_file('/workspace/session/value') == b'1'
try: read_file('/workspace/session/bulk.bin')
except OSError: pass
else: raise AssertionError('failed call file survived')
physical_value=2
write_file('/workspace/session/value',b'2')
write_file('result.bin',b'2')
print(2)
""",
    (),
    ("result.bin",),
    FileLimits(),
)


def overflow(payload: bytes) -> Request:
    """Carry incompressible session bytes through the real guest checkpoint exporter."""
    return Request(
        IO
        + b"assert physical_value == 1; physical_value=999; write_file('/workspace/session/value',b'999'); write_file('result.bin',b'999'); print(999)",
        (Input("bulk.bin", payload, lifecycle="session"),),
        ("result.bin",),
        FileLimits(input_bytes=PAYLOAD_BYTES, file_bytes=PAYLOAD_BYTES),
        WorkspaceLimits(bytes=128 * 1024**2),
    )


def audit(store: SharedStore, policy: Policy) -> dict[str, int]:
    """Check physical limits and independent consistency after every publication boundary."""
    assert store.db.execute("SELECT version FROM settings").fetchone()[0] == VERSION
    page_size = store.db.execute("PRAGMA page_size").fetchone()[0]
    ceiling = store.db.execute("PRAGMA max_page_count").fetchone()[0] * page_size
    size = (store.root / "shared.sqlite").stat().st_size
    assert ceiling == policy.database_bytes and size <= ceiling
    assert store.db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    store.audit_usage()
    return {"database_bytes": size, "ceiling_bytes": ceiling, "logical_bytes": store.usage()}


def candidate_files(candidate: Path) -> dict[str, bytes]:
    """Read the exported workspace so failure evidence proves guest file mutation."""
    inventory = json.loads((candidate / "workspace.json").read_bytes())
    data = (candidate / "workspace.bin").read_bytes()
    return {
        item["name"]: data[item["offset"] : item["offset"] + item["bytes"]]
        for item in inventory["files"]
    }


def worker(helper: Path, startup: Path, state: Path, stage: str) -> None:
    """Run one supervisor with the immutable policy selected before root creation."""
    policy = Policy(**json.loads((state / "policy.json").read_bytes()))
    request = overflow((state / "payload.bin").read_bytes())
    with SharedStore(
        state / "store", "one", _profile_for(helper, startup), LIMITS, physical_policy=policy
    ) as store:
        before = audit(store, policy)
        if stage == "seed":
            result = call(store, "seed", SEED, helper, startup, SCRATCH)
            assert console(result).strip() == b"1"
            (state / "seed.bin").write_bytes(result)
            evidence = {"seed_sha256": hashlib.sha256(result).hexdigest()}
        elif stage == "overflow":
            try:
                call(store, "overflow", request, helper, startup, SCRATCH)
            except sqlite3.OperationalError as error:
                assert error.sqlite_errorcode == sqlite3.SQLITE_FULL
                code = error.sqlite_errorcode
            else:
                raise AssertionError("oversized native checkpoint was acknowledged")
            row = store.db.execute("SELECT * FROM launches WHERE call='overflow'").fetchone()
            assert row is not None and row["state"] == "armed"
            assert stopped(Identity.decode(row["identity"]))
            work = store.root / "scratch" / row["token"]
            assert (work / "native.json").is_file()
            files = candidate_files(work / "candidate")
            assert files["session/bulk.bin"] == request.inputs[0].data
            assert files["session/value"] == b"999"
            assert store._owner()["current_call"] == "seed"
            assert (
                store.db.execute("SELECT result FROM calls WHERE id='overflow'").fetchone()[0]
                is None
            )
            assert store.db.execute("SELECT count(*) FROM reservations").fetchone()[0] == 1
            assert not store.db.in_transaction
            evidence = {
                "sqlite_errorcode": code,
                "exported_payload_sha256": hashlib.sha256(request.inputs[0].data).hexdigest(),
                "helper_stopped": True,
                "reservation_retained": True,
                "previous_checkpoint_retained": True,
            }
        else:
            assert stage == "recover"
            assert store._owner()["current_call"] == "seed"
            assert (
                store.db.execute("SELECT status FROM calls WHERE id='overflow'").fetchone()[0]
                == "interrupted"
            )
            try:
                call(store, "overflow", request, state / "absent", state / "absent", SCRATCH)
            except Refused as error:
                assert "recovery" in str(error)
            else:
                raise AssertionError("failed call identity re-executed")
            NativeJournal(store).reclaim("overflow")
            result = call(store, "verify", VERIFY, helper, startup, SCRATCH)
            assert console(result).strip() == b"2"
            assert store.collect_checkpoints(limit=128) == 1
            retry = call(store, "seed", SEED, state / "absent", state / "absent", SCRATCH)
            assert retry == (state / "seed.bin").read_bytes()
            assert store.db.execute("SELECT count(*) FROM launches").fetchone()[0] == 0
            assert store.db.execute("SELECT count(*) FROM reservations").fetchone()[0] == 0
            assert not list((store.root / "scratch").iterdir())
            evidence = {
                "python_and_session_files_recovered": True,
                "interrupted_retry_refused": True,
                "replay_after_collection_sha256": hashlib.sha256(retry).hexdigest(),
                "scratch_reclaimed": True,
            }
        atomic_report(
            state / f"{stage}.json",
            {"pid": os.getpid(), "before": before, "after": audit(store, policy), **evidence},
        )


def launch(helper: Path, startup: Path, state: Path, stage: str) -> dict:
    """Require a fresh supervisor and its assertions to finish successfully."""
    with (state / f"{stage}.log").open("wb") as log:
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "scripts.experiments.mxc_files_patch.physical_probe",
                "--helper",
                str(helper),
                "--startup",
                str(startup),
                "--state-dir",
                str(state),
                "--worker",
                stage,
            ],
            stdout=log,
            stderr=subprocess.STDOUT,
            timeout=300,
        )
    assert result.returncode == 0, (stage, result.returncode)
    return json.loads((state / f"{stage}.json").read_bytes())


def qualify(helper: Path, startup: Path, state: Path) -> dict:
    """Calibrate separately, then qualify a new capped root without resizing its policy."""
    state.mkdir(parents=True, exist_ok=False)
    with SharedStore(state / "calibration", "one", _profile_for(helper, startup), LIMITS) as store:
        assert console(call(store, "seed", SEED, helper, startup, SCRATCH)).strip() == b"1"
        seed_bytes = (store.root / "shared.sqlite").stat().st_size
        assert console(call(store, "verify", VERIFY, helper, startup, SCRATCH)).strip() == b"2"
        baseline = (store.root / "shared.sqlite").stat().st_size
        page_size = store.db.execute("PRAGMA page_size").fetchone()[0]
        NativeJournal(store).delete()
        assert store.collect_checkpoints(limit=128) == 2
        assert not list((store.root / "scratch").iterdir())
        store.audit_usage()
    ceiling = ((baseline + MARGIN + page_size - 1) // page_size) * page_size
    policy = Policy(ceiling, ceiling, 64 * 1024**2)
    payload = os.urandom(PAYLOAD_BYTES)
    compressed = len(zlib.compress(payload, level=1))
    assert compressed > MARGIN + 1024**2
    (state / "payload.bin").write_bytes(payload)
    atomic_report(state / "policy.json", asdict(policy))
    records = {
        stage: launch(helper, startup, state, stage) for stage in ("seed", "overflow", "recover")
    }
    assert records["seed"]["seed_sha256"] == records["recover"]["replay_after_collection_sha256"]
    return {
        "qualified": True,
        "format": VERSION,
        "policy": asdict(policy),
        "source_commit": os.environ.get("GITHUB_SHA"),
        "run_id": os.environ.get("GITHUB_RUN_ID"),
        "run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT"),
        "helper_sha256": digest(helper),
        "platform": platform.system(),
        "python": platform.python_version(),
        "sqlite": sqlite3.sqlite_version,
        "calibration_database_bytes": baseline,
        "calibration_seed_bytes": seed_bytes,
        "calibration_recovery_growth_bytes": baseline - seed_bytes,
        "calibration_scratch_reclaimed": True,
        "payload_bytes": len(payload),
        "compressed_payload_bytes": compressed,
        "supervisors": records,
    }


def main() -> int:
    """Run the native matrix or an isolated publication/recovery supervisor."""
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("helper", "startup", "state-dir"):
        parser.add_argument(f"--{name}", type=Path, required=True)
    parser.add_argument("--worker", choices=("seed", "overflow", "recover"))
    args = parser.parse_args()
    helper, startup, state = args.helper.resolve(), args.startup.resolve(), args.state_dir.resolve()
    if args.worker:
        worker(helper, startup, state, args.worker)
    else:
        atomic_report(state / "result.json", qualify(helper, startup, state))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
