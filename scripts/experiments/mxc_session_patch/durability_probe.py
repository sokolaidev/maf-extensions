"""Exercise format-4 publication and cleanup through real native process restarts."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import sqlite3
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

from .host_call import digest
from .host_store import CHUNK, MAX_CHECKPOINT, MAX_FILES, Refused
from .native_journal import NativeJournal
from .shared_call import call
from .shared_store import (
    CALL_METADATA,
    PATH_BYTES,
    SESSION_METADATA,
    STORE_METADATA,
    VERSION,
    Limits,
    ScratchLimits,
    SharedStore,
)
from .storage_probe import console

BOUNDARIES = (
    "before_commit",
    "after_commit",
    "before_ack",
    "before_cleanup_intent",
    "after_cleanup_intent",
    "after_scratch_removal",
    "before_cleanup_release",
    "after_cleanup_release",
)
LIMITS = Limits(8 * MAX_CHECKPOINT, 16 * MAX_CHECKPOINT)
SCRATCH = ScratchLimits(
    2 * MAX_CHECKPOINT + 5 * CHUNK,
    2 * MAX_FILES * (PATH_BYTES // 2 + 1) + 12,
    4 * MAX_CHECKPOINT + 10 * CHUNK,
)


def reconcile(store: SharedStore) -> dict[str, int]:
    """Compare persisted charges with independent aggregates of source rows."""
    assert store.db.execute("SELECT version FROM settings").fetchone()[0] == 4 == VERSION
    totals = {"": STORE_METADATA}
    for (session,) in store.db.execute("SELECT id FROM sessions"):
        calls, payload = store.db.execute(
            "SELECT count(*),coalesce(sum(result_charge+checkpoint_charge),0) FROM calls WHERE session=?",
            (session,),
        ).fetchone()
        reserved = store.db.execute(
            "SELECT coalesce(sum(charge),0) FROM reservations WHERE session=?", (session,)
        ).fetchone()[0]
        totals[session] = SESSION_METADATA + calls * CALL_METADATA + payload + reserved
        assert store.usage(session) == totals[session]
        totals[""] += totals[session]
    assert store.usage() == totals[""]
    store.audit_usage()
    return totals


def _limits_for(quota: str) -> Limits:
    allowance = SESSION_METADATA + CALL_METADATA + LIMITS.reservation
    if quota == "session":
        return replace(LIMITS, session_quota=allowance)
    if quota == "store":
        return replace(LIMITS, store_quota=STORE_METADATA + allowance)
    return LIMITS


def _program(value: int) -> bytes:
    if value == 1:
        return b"mxc_durable_value = 1; print(mxc_durable_value)"
    return (
        f"assert mxc_durable_value == {value - 1}; mxc_durable_value += 1; print(mxc_durable_value)"
    ).encode()


def _profile_for(helper: Path, startup: Path) -> dict[str, str]:
    return {
        "helper": digest(helper),
        "startup_index": digest(startup / "index.json"),
        "platform": f"{platform.system()}-{platform.machine()}",
        "policy": "closed",
    }


def _worker(
    helper: Path, startup: Path, root: Path, call_id: str, value: int, fault: str, quota: str
) -> None:
    def boundary(point: str) -> None:
        if point == fault:
            os._exit(74)

    with SharedStore(root, "one", _profile_for(helper, startup), _limits_for(quota)) as store:
        result = call(store, call_id, _program(value), helper, startup, SCRATCH, CHUNK, boundary)
        assert console(result).strip() == str(value).encode()
        reconcile(store)


def _launch(
    helper: Path,
    startup: Path,
    state: Path,
    call_id: str,
    value: int,
    fault: str = "",
    quota: str = "",
) -> None:
    command = [
        sys.executable,
        "-m",
        "scripts.experiments.mxc_session_patch.durability_probe",
        "--helper",
        str(helper),
        "--startup",
        str(startup),
        "--state-dir",
        str(state),
        "--worker",
        "--call-id",
        call_id,
        "--value",
        str(value),
        "--fault",
        fault,
        "--quota",
        quota,
    ]
    with (state / f"{call_id}.log").open("wb") as log:
        child = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, timeout=240)
    assert child.returncode == (74 if fault else 0), (call_id, child.returncode)


def qualify(helper: Path, startup: Path, state: Path) -> dict[str, object]:
    """Require recovery, preserved retries and reconciled charges at every crash boundary."""
    state.mkdir(parents=True, exist_ok=False)
    root = state / "store"
    profile = _profile_for(helper, startup)
    _launch(helper, startup, state, "seed", 1)
    records = []
    value = 1
    with SharedStore(root, "one", profile, LIMITS) as store:
        seed = call(store, "seed", _program(1), state / "absent", state / "absent", SCRATCH, CHUNK)
        reconcile(store)
    for fault in BOUNDARIES:
        candidate = value + 1
        _launch(helper, startup, state, fault, candidate, fault)
        with SharedStore(root, "one", profile, LIMITS) as store:
            before = reconcile(store)
            row = store.db.execute("SELECT * FROM calls WHERE id=?", (fault,)).fetchone()
            committed = fault != "before_commit"
            assert row["status"] == ("committed" if committed else "interrupted")
            launches = store.db.execute("SELECT count(*) FROM launches").fetchone()[0]
            reservations = store.db.execute("SELECT count(*) FROM reservations").fetchone()[0]
            assert reservations == (0 if committed else 1)
            assert launches == (0 if fault in ("before_ack", "after_cleanup_release") else 1)
            if committed:
                saved = bytes(row["result"])
                replay = call(
                    store,
                    fault,
                    _program(candidate),
                    state / "absent",
                    state / "absent",
                    SCRATCH,
                    CHUNK,
                )
                assert replay == saved and console(replay).strip() == str(candidate).encode()
                value = candidate
            else:
                try:
                    call(
                        store,
                        fault,
                        _program(candidate),
                        state / "absent",
                        state / "absent",
                        SCRATCH,
                        CHUNK,
                    )
                except Refused as error:
                    assert "recovery" in str(error)
                else:
                    raise AssertionError("interrupted call re-executed")
            if launches:
                NativeJournal(store).reclaim(fault)
            after = reconcile(store)
            assert store.db.execute("SELECT count(*) FROM launches").fetchone()[0] == 0
            assert store.db.execute("SELECT count(*) FROM reservations").fetchone()[0] == 0
            assert not list((root / "scratch").iterdir())
            records.append(
                {"boundary": fault, "committed": committed, "before": before, "after": after}
            )
    _launch(helper, startup, state, "verify", value + 1)
    with SharedStore(root, "one", profile, LIMITS) as store:
        removed = store.collect_checkpoints(limit=128)
        assert removed > 0
        assert (
            store.db.execute("SELECT checkpoint_charge FROM calls WHERE id='seed'").fetchone()[0]
            == 0
        )
        assert (
            call(store, "seed", _program(1), state / "absent", state / "absent", SCRATCH, CHUNK)
            == seed
        )
        reconcile(store)
    quotas = {}
    for scope in ("session", "store"):
        area = state / f"quota-{scope}"
        area.mkdir()
        _launch(helper, startup, area, "seed", 1, quota=scope)
        with SharedStore(area / "store", "one", profile, _limits_for(scope)) as store:
            before = reconcile(store)
            try:
                call(store, "new", _program(2), area / "absent", area / "absent", SCRATCH, CHUNK)
            except Refused as error:
                assert "quota" in str(error)
            else:
                raise AssertionError("full quota admitted a helper")
            assert reconcile(store) == before
            assert store.db.execute("SELECT count(*) FROM calls").fetchone()[0] == 1
            replay = call(
                store, "seed", _program(1), area / "absent", area / "absent", SCRATCH, CHUNK
            )
            assert console(replay).strip() == b"1"
            quotas[scope] = {
                "refused_before_launch": True,
                "replay_sha256": hashlib.sha256(replay).hexdigest(),
            }
    return {
        "format": VERSION,
        "helper_sha256": digest(helper),
        "platform": platform.system(),
        "python": platform.python_version(),
        "sqlite": sqlite3.sqlite_version,
        "source_commit": os.environ.get("GITHUB_SHA"),
        "run_id": os.environ.get("GITHUB_RUN_ID"),
        "run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT"),
        "boundaries": records,
        "quota_controls": quotas,
        "collected_checkpoints": removed,
        "replay_after_collection": True,
        "qualified": True,
    }


def main() -> int:
    """Run the qualification or one isolated crash worker."""
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("helper", "startup", "state-dir"):
        parser.add_argument(f"--{name}", type=Path, required=True)
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--call-id", default="seed", help=argparse.SUPPRESS)
    parser.add_argument("--value", type=int, default=1, help=argparse.SUPPRESS)
    parser.add_argument("--fault", choices=("", *BOUNDARIES), default="", help=argparse.SUPPRESS)
    parser.add_argument(
        "--quota", choices=("", "session", "store"), default="", help=argparse.SUPPRESS
    )
    args = parser.parse_args()
    helper, startup, state = args.helper.resolve(), args.startup.resolve(), args.state_dir.resolve()
    if args.worker:
        _worker(helper, startup, state / "store", args.call_id, args.value, args.fault, args.quota)
    else:
        result = qualify(helper, startup, state)
        (state / "result.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
