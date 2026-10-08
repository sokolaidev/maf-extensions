"""Qualify retirement and retry preservation with the native bounded-file helper."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path

from scripts.experiments.mxc_files_patch.native_probe import IO
from scripts.experiments.mxc_files_patch.request import FileLimits, Request
from scripts.experiments.mxc_files_patch.shared_call import call
from scripts.experiments.mxc_files_patch.transport import result_limit
from scripts.experiments.mxc_session_patch.durability_probe import (
    LIMITS as BASE_LIMITS,
)
from scripts.experiments.mxc_session_patch.durability_probe import (
    SCRATCH as BASE_SCRATCH,
)
from scripts.experiments.mxc_session_patch.durability_probe import (
    _profile_for,
    reconcile,
)
from scripts.experiments.mxc_session_patch.host_call import atomic_report, digest
from scripts.experiments.mxc_session_patch.host_store import Refused
from scripts.experiments.mxc_session_patch.native_journal import NativeJournal
from scripts.experiments.mxc_session_patch.process_identity import Identity, stopped
from scripts.experiments.mxc_session_patch.shared_store import SharedStore

SEED = Request(
    IO + b"write_file('result.bin',b'retained'); print('retained')",
    (),
    ("result.bin",),
    FileLimits(),
)
ACTIVE = Request(b"while True: pass", (), (), FileLimits())
LIMITS = replace(BASE_LIMITS, result_bytes=result_limit(SEED))
SCRATCH = replace(
    BASE_SCRATCH,
    bytes=BASE_SCRATCH.bytes + 80 * 1024**2,
    store_bytes=BASE_SCRATCH.store_bytes + 160 * 1024**2,
    entries=BASE_SCRATCH.entries + 12,
)
BOUNDARIES = (
    "before_retire_commit",
    "after_retire_commit",
    "after_helper_termination",
    "after_cleanup_intent",
    "after_scratch_removal",
    "before_cleanup_release",
    "after_cleanup_release",
)


def worker(helper: Path, startup: Path, state: Path, fault: str) -> None:
    """Wait for the native ready marker, then request deletion from the host thread."""
    requested = threading.Event()
    finished = threading.Event()

    def request_deletion() -> None:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline and not finished.is_set():
            markers = list((state / "store/scratch").glob("*/native.ready"))
            if markers and markers[0].read_bytes() == b"ready\n":
                atomic_report(state / "ready.json", {"native_ready": True})
                requested.set()
                return
            finished.wait(0.02)
        requested.set()

    def boundary(point: str) -> None:
        evidence = state / "helper.json"
        if point == "before_retire_commit" and not evidence.exists():
            row = store.db.execute("SELECT identity FROM launches WHERE call='active'").fetchone()
            assert row is not None
            identity = Identity.decode(row[0])
            assert not stopped(identity)
            atomic_report(evidence, {"identity": identity.encode(), "live_before_retirement": True})
        if point == fault:
            os._exit(74)

    with SharedStore(state / "store", "one", _profile_for(helper, startup), LIMITS) as store:
        notifier = threading.Thread(target=request_deletion)
        notifier.start()
        try:
            try:
                call(store, "active", ACTIVE, helper, startup, SCRATCH, boundary, delete=requested)
            except Refused as error:
                assert "deletion requested" in str(error), str(error)
            else:
                raise AssertionError("deleted call acknowledged success")
            assert (state / "ready.json").exists()
        finally:
            finished.set()
            notifier.join(timeout=5)
            assert not notifier.is_alive()


def qualify(helper: Path, startup: Path, state: Path) -> dict:
    """Require cleanup and byte-identical retry after each supervisor crash boundary."""
    records = []
    profile = _profile_for(helper, startup)
    for fault in ("", *BOUNDARIES):
        area = state / (fault or "active-deletion")
        area.mkdir()
        with SharedStore(area / "store", "one", profile, LIMITS) as store:
            seed = call(store, "seed", SEED, helper, startup, SCRATCH)
        with (area / "worker.log").open("wb") as log:
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    __spec__.name,
                    "--helper",
                    str(helper),
                    "--startup",
                    str(startup),
                    "--state-dir",
                    str(area),
                    "--worker",
                    "--fault",
                    fault,
                ],
                stdout=log,
                stderr=subprocess.STDOUT,
                timeout=180,
            )
        assert result.returncode == (74 if fault else 0), (fault, result.returncode)
        assert json.loads((area / "ready.json").read_bytes()) == {"native_ready": True}
        with SharedStore(area / "store", "one", profile, LIMITS) as store:
            assert store._owner()["state"] == (
                "active" if fault == "before_retire_commit" else "retired"
            )
            recorded = Identity.decode(json.loads((area / "helper.json").read_bytes())["identity"])
            identities = [
                Identity.decode(row[0]) for row in store.db.execute("SELECT identity FROM launches")
            ]
            journal = NativeJournal(store)
            journal.delete()
            assert stopped(recorded)
            assert all(stopped(value) for value in identities)
            assert store.db.execute("SELECT count(*) FROM launches").fetchone()[0] == 0
            assert store.db.execute("SELECT count(*) FROM reservations").fetchone()[0] == 0
            assert (
                store.db.execute("SELECT status FROM calls WHERE id='active'").fetchone()[0]
                == "interrupted"
            )
            assert not list((area / "store/scratch").iterdir())
            assert store.collect_checkpoints(limit=128) == 1
            assert call(store, "seed", SEED, state / "absent", state / "absent", SCRATCH) == seed
            for operation in (
                lambda: store.begin("new", b"new"),
                lambda: store.commit("active", state / "absent", b"late"),
            ):
                try:
                    operation()
                except Refused as error:
                    assert "retired" in str(error)
                else:
                    raise AssertionError("retired session accepted execution or publication")
            journal.delete()
            reconcile(store)
            records.append(
                {
                    "boundary": fault or "active-deletion",
                    "native_ready": True,
                    "helper_stopped_and_scratch_reclaimed": True,
                    "late_publication_refused": True,
                    "retry_after_collection_sha256": hashlib.sha256(seed).hexdigest(),
                }
            )
    return {"status": "qualified", "helper_sha256": digest(helper), "cases": records}


def main() -> int:
    """Use a fresh evidence directory; workers reuse only their parent's reserved store."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--helper", type=Path, required=True)
    parser.add_argument("--startup", type=Path, required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--fault", default="", choices=("", *BOUNDARIES))
    args = parser.parse_args()
    helper, startup, state = args.helper.resolve(), args.startup.resolve(), args.state_dir.resolve()
    if args.worker:
        worker(helper, startup, state, args.fault)
    else:
        state.mkdir(parents=True, exist_ok=False)
        atomic_report(state / "result.json", qualify(helper, startup, state))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
