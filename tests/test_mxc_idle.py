"""Idle retirement, durable deadlines and result retry guarantees."""

from __future__ import annotations

import importlib
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))
try:
    store = importlib.import_module("scripts.experiments.mxc_session_patch.shared_store")
    idle = importlib.import_module("scripts.experiments.mxc_session_patch.idle")
    native = importlib.import_module("scripts.experiments.mxc_session_patch.native_journal")
finally:
    sys.path.remove(str(Path(__file__).parents[1]))

PROFILE = {"runtime": "pinned", "policy": "closed"}
LIMITS = store.Limits(200_000, 500_000, checkpoint_bytes=1024, result_bytes=128, files=2)
POLICY = idle.Policy(10, 3)


class Clock:
    def __init__(self, utc=100 * store.SECOND, monotonic=100 * store.SECOND):
        self.utc = utc
        self.monotonic = monotonic

    def utc_ns(self):
        return self.utc

    def monotonic_ns(self):
        return self.monotonic

    def advance(self, seconds):
        self.utc += int(seconds * store.SECOND)
        self.monotonic += int(seconds * store.SECOND)


def open_store(root, clock, policy=POLICY, session="one"):
    return store.SharedStore(root, session, PROFILE, LIMITS, clock, idle_policy=policy)


def row(db):
    return db.db.execute("SELECT * FROM session_idle WHERE session=?", (db.session,)).fetchone()


def publish(db, path, call="a"):
    assert db.begin(call, call.encode()) is None
    path.mkdir()
    (path / "index.json").write_bytes(b"checkpoint")
    db.commit(call, path, b"saved")


def test_disabled_by_default_and_idle_schema_is_opt_in(tmp_path):
    clock = Clock()
    root = tmp_path / "db"
    with open_store(root, clock, None) as db:
        clock.advance(100000)
        assert not db.retire_idle()
        assert db._owner()["state"] == "active"
        assert db.db.execute("SELECT version FROM settings").fetchone()[0] == 4
    with open_store(root, clock, session="enabled") as db:
        assert db.db.execute("SELECT version FROM settings").fetchone()[0] == 5
    with open_store(root, clock, None) as db:
        assert not db.retire_idle()
        assert db.begin("a", b"a") is None


@pytest.mark.parametrize(
    "timeout,grace", [(0, 1), (-1, 1), (True, 1), (1.5, 1), (1, -1), (1, True), (2**63, 0)]
)
def test_invalid_policy(timeout, grace):
    with pytest.raises(store.Refused, match="idle policy"):
        idle.Policy(timeout, grace)


def test_due_idle_retires_before_admission_but_preserves_retry_and_charges(tmp_path):
    clock = Clock()
    with open_store(tmp_path / "db", clock) as db:
        publish(db, tmp_path / "a")
        before = db.usage()
        deadline = row(db)["deadline"]
        clock.advance(9)
        assert db.begin("a", b"a") == b"saved"
        assert row(db)["deadline"] == deadline
        assert not native.NativeJournal(db).expire_idle()
        clock.advance(1)
        with pytest.raises(store.Refused, match="retired"):
            db.begin("b", b"b")
        assert db._owner()["state"] == "retired"
        assert db.usage() == before
        assert db.begin("a", b"a") == b"saved"
        assert db.collect_checkpoints() == 1
        assert db.begin("a", b"a") == b"saved"
        assert native.NativeJournal(db).expire_idle()
        assert db.db.execute("SELECT count(*) FROM calls").fetchone()[0] == 1


def test_running_call_gets_full_idle_interval_after_completion(tmp_path):
    clock = Clock()
    with open_store(tmp_path / "db", clock) as db:
        db.begin("a", b"a")
        clock.advance(100)
        assert not db.retire_idle()
        assert row(db)["deadline"] is None
        candidate = tmp_path / "candidate"
        candidate.mkdir()
        (candidate / "index.json").write_bytes(b"checkpoint")
        db.commit("a", candidate, b"saved")
        clock.advance(9)
        assert not db.retire_idle()
        clock.advance(1)
        assert db.retire_idle()


def test_reopen_and_retries_preserve_deadline_and_policy(tmp_path):
    clock = Clock()
    root = tmp_path / "db"
    with open_store(root, clock) as db:
        publish(db, tmp_path / "a")
        deadline = row(db)["deadline"]
    clock.advance(8)
    with open_store(root, clock) as db:
        assert row(db)["deadline"] == deadline
        assert db.begin("a", b"a") == b"saved"
        assert row(db)["deadline"] == deadline
    for policy in (None, idle.Policy(20, 3), idle.Policy(10, 4)):
        with pytest.raises(store.Refused, match="configuration differs"):
            open_store(root, clock, policy)
    clock.advance(5)
    with open_store(root, clock) as db:
        assert db.retire_idle()
        assert db.begin("a", b"a") == b"saved"


def test_existing_session_cannot_invent_historical_idle_time(tmp_path):
    root = tmp_path / "db"
    with open_store(root, Clock(), None):
        pass
    with pytest.raises(store.Refused, match="configuration differs"):
        open_store(root, Clock())


@pytest.mark.parametrize("action", ["retire", "admit", "restore"])
def test_idle_retirement_is_applied_on_owner_access(tmp_path, action):
    clock = Clock()
    with open_store(tmp_path / "db", clock) as db:
        clock.advance(10)
        if action == "retire":
            assert native.NativeJournal(db).expire_idle()
        else:
            with pytest.raises(store.Refused, match="retired"):
                if action == "admit":
                    db.begin("a", b"a")
                else:
                    db.restore(tmp_path / "out")
        assert db._owner()["state"] == "retired"


def test_restart_forfeits_grants_without_replenishing_budget(tmp_path):
    root = tmp_path / "db"
    clock = Clock()
    with open_store(root, clock):
        pass
    clock.advance(10)
    for remaining in (2, 1, 0):
        with open_store(root, clock) as db:
            assert not db.retire_idle()
            assert row(db)["remaining"] == remaining * store.SECOND
            assert not db.retire_idle()
            assert row(db)["remaining"] == remaining * store.SECOND
    with open_store(root, clock) as db:
        assert db.retire_idle()


@pytest.mark.parametrize("jump", [-50, 11, 100])
def test_clock_jumps_use_bounded_forgiveness(tmp_path, jump):
    clock = Clock()
    with open_store(tmp_path / "db", clock) as db:
        clock.utc += jump * store.SECOND
        if jump >= 13:
            assert db.retire_idle()
        elif jump < 0:
            assert not db.retire_idle()
            assert row(db)["uncertain"] == 1
            assert row(db)["remaining"] == 3 * store.SECOND
        else:
            assert not db.retire_idle()
            assert row(db)["remaining"] == 2 * store.SECOND
            clock.monotonic += store.SECOND
            assert not db.retire_idle()
            clock.monotonic += store.SECOND
            assert not db.retire_idle()
            clock.monotonic += store.SECOND
            assert db.retire_idle()


def test_failed_grant_transaction_restores_in_memory_allowance(tmp_path):
    clock = Clock()
    with open_store(tmp_path / "db", clock) as db:
        clock.utc += 11 * store.SECOND

        def fail(boundary):
            if boundary == "before_idle_grant_commit":
                raise OSError("disk full")

        with pytest.raises(OSError, match="disk full"):
            db.retire_idle(fail)
        assert db.idle_grant == 0
        assert row(db)["remaining"] == 3 * store.SECOND
        assert not db.retire_idle()
        assert row(db)["remaining"] == 2 * store.SECOND


@pytest.mark.parametrize(
    "boundary",
    [
        "before_retire_commit",
        "after_retire_commit",
        "before_idle_grant_commit",
        "after_idle_grant_commit",
    ],
)
def test_process_death_at_idle_transactions_is_recoverable(tmp_path, boundary):
    root = tmp_path / "db"
    with open_store(root, Clock()) as db:
        publish(db, tmp_path / "a")
    utc = 110 if "grant" in boundary else 113
    program = f"""
import os
from pathlib import Path
from scripts.experiments.mxc_session_patch.shared_store import SharedStore, Limits, SECOND
from scripts.experiments.mxc_session_patch.idle import Policy
class Clock:
    def utc_ns(self): return {utc} * SECOND
    def monotonic_ns(self): return 200 * SECOND
with SharedStore(Path({str(root)!r}), 'one', {PROFILE!r}, {LIMITS!r}, Clock(), idle_policy=Policy(10, 3)) as db:
    def crash(point):
        if point == {boundary!r}: os._exit(74)
    db.retire_idle(crash)
"""
    result = subprocess.run([sys.executable, "-c", program], capture_output=True, timeout=20)
    assert result.returncode == 74, result.stderr.decode()
    with open_store(root, Clock(utc * store.SECOND)) as db:
        assert db._owner()["state"] == (
            "retired" if boundary == "after_retire_commit" else "active"
        )
        assert (
            row(db)["remaining"]
            == (2 if boundary == "after_idle_grant_commit" else 3) * store.SECOND
        )
        db.clock.utc = 113 * store.SECOND
        assert native.NativeJournal(db).expire_idle()
        assert db.begin("a", b"a") == b"saved"


@pytest.mark.parametrize("damage", ["record", "schema"])
def test_missing_idle_metadata_is_not_recreated_on_reopen(tmp_path, damage):
    root = tmp_path / "db"
    with open_store(root, Clock()) as db:
        db.db.execute(
            "DELETE FROM session_idle" if damage == "record" else "DROP TABLE session_idle"
        )
    before = (root / "shared.sqlite").read_bytes()
    with pytest.raises(store.Refused, match="idle|store format"):
        open_store(root, Clock())
    assert (root / "shared.sqlite").read_bytes() == before


def test_another_session_keeps_its_own_deadline(tmp_path):
    root = tmp_path / "db"
    clock = Clock()
    with open_store(root, clock) as one:
        clock.advance(5)
        with open_store(root, clock, session="two") as two:
            clock.advance(5)
            assert one.retire_idle()
            assert not two.retire_idle()
            two.begin("a", b"a")
            clock.advance(100)
            assert not two.retire_idle()


@pytest.mark.parametrize("family", ["mxc_session_patch", "mxc_streams_patch", "mxc_files_patch"])
def test_supervisors_preserve_retry_and_refuse_new_native_launch_after_idle(tmp_path, family):
    module = importlib.import_module(f"scripts.experiments.{family}.shared_call")
    from dataclasses import replace

    limits = replace(
        LIMITS, session_quota=200_000_000, store_quota=400_000_000, result_bytes=32_000_000
    )
    scratch = store.ScratchLimits(100_000_000, 10000, 200_000_000)
    clock = Clock()
    request = b"a"
    code = request
    if family == "mxc_files_patch":
        requests = importlib.import_module("scripts.experiments.mxc_files_patch.request")
        request = requests.Request(code, (), (), requests.FileLimits())
        code = request.identity()
    with store.SharedStore(
        tmp_path / "db", "one", PROFILE, limits, clock, idle_policy=POLICY
    ) as db:
        db.begin("a", code)
        candidate = tmp_path / "candidate"
        candidate.mkdir()
        (candidate / "index.json").write_bytes(b"checkpoint")
        db.commit("a", candidate, b"saved")
        clock.advance(10)
        args = (request, tmp_path / "absent", tmp_path / "absent", scratch)
        if family != "mxc_files_patch":
            args += (100,)
        assert module.call(db, "a", *args) == b"saved"
        with pytest.raises(store.Refused, match="retired"):
            module.call(db, "b", *args)
        assert db.db.execute("SELECT count(*) FROM launches").fetchone()[0] == 0


def test_native_recovery_waits_for_cleanup_before_starting_idle(tmp_path):
    clock = Clock()
    root = tmp_path / "db"
    scratch = store.ScratchLimits(40_000, 20, 80_000)
    child = subprocess.Popen(
        [sys.executable, "-c", "import sys; sys.stdin.buffer.read()"], stdin=subprocess.PIPE
    )
    try:
        with open_store(root, clock) as db:
            db.begin("a", b"a", scratch=scratch)
            journal = native.NativeJournal(db)
            work = journal.prepare("a")
            journal.arm("a", child)
        clock.advance(100)
        with open_store(root, clock) as db:
            journal = native.NativeJournal(db)
            assert not journal.expire_idle()
            assert child.poll() is None
            assert row(db)["deadline"] is None
            with pytest.raises(store.Refused, match="still alive"):
                journal.reclaim("a")
            child.kill()
            child.wait(timeout=10)
            journal.reclaim("a")
            assert not work.exists()
            assert row(db)["deadline"] == clock.utc + 10 * store.SECOND
            clock.advance(9)
            assert not journal.expire_idle()
            clock.advance(1)
            assert journal.expire_idle()
    finally:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=10)
        assert child.stdin is not None
        child.stdin.close()


def test_unknown_launch_remains_charged_and_blocks_idle_cleanup(tmp_path):
    clock = Clock()
    with open_store(tmp_path / "db", clock) as db:
        db.begin("a", b"a", scratch=store.ScratchLimits(40_000, 20, 80_000))
        journal = native.NativeJournal(db)
        work = journal.prepare("a")
        before = db.usage()
        clock.advance(100)
        assert not journal.expire_idle()
        assert work.exists()
        assert db.usage() == before
        assert db._owner()["state"] == "active"
        with pytest.raises(store.Refused, match="unidentified"):
            journal.delete()
        assert db.usage() == before
