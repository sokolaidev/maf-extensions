"""Transactional quota and clock controls for the next MXC store format."""

from __future__ import annotations

import importlib
import sqlite3
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))
try:
    store = importlib.import_module("scripts.experiments.mxc_session_patch.shared_store")
finally:
    sys.path.remove(str(Path(__file__).parents[1]))

PROFILE = {"runtime": "pinned", "policy": "closed", "machine": "local"}
LIMITS = store.Limits(
    200_000,
    500_000,
    checkpoint_bytes=1024,
    result_bytes=128,
    files=2,
    retention_seconds=10,
    grace_seconds=3,
)


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


def checkpoint(path, data=b"checkpoint"):
    path.mkdir()
    (path / "index.json").write_bytes(data)
    return path


def publish(db, path, call="a", code=b"code", result=b"result"):
    assert db.begin(call, code) is None
    db.commit(call, checkpoint(path), result)


def row(db, call="a"):
    return db.db.execute(
        "SELECT * FROM calls WHERE session=? AND id=?", (db.session, call)
    ).fetchone()


def test_sessions_share_quota_but_isolate_calls_and_profiles(tmp_path):
    with store.SharedStore(tmp_path / "db", "one", PROFILE, LIMITS, Clock()) as one:
        with store.SharedStore(
            tmp_path / "db", "two", PROFILE | {"runtime": "other"}, LIMITS, Clock()
        ) as two:
            publish(one, tmp_path / "one", result=b"one")
            publish(two, tmp_path / "two", result=b"two")
            assert one.begin("a", b"code") == b"one"
            assert two.begin("a", b"code") == b"two"
            assert one.usage() == store.STORE_METADATA + one.usage("one") + two.usage("two")
            with pytest.raises(store.Refused, match="owner"):
                store.SharedStore(tmp_path / "db", "one", PROFILE, LIMITS)
            assert one.begin("a", b"code") == b"one"


def test_exact_quota_and_full_store_replay(tmp_path):
    exact = store.SESSION_METADATA + store.CALL_METADATA + LIMITS.reservation
    limits = replace(LIMITS, session_quota=exact, store_quota=exact + store.STORE_METADATA)
    with store.SharedStore(tmp_path / "db", "one", PROFILE, limits, Clock()) as db:
        db.begin("a", b"code")
        assert db.usage() == exact + store.STORE_METADATA
        db.commit("a", checkpoint(tmp_path / "a"), b"result")
        with pytest.raises(store.Refused, match="quota"):
            db.begin("b", b"next")
        assert db.begin("a", b"code") == b"result"
        assert db.db.execute("SELECT count(*) FROM calls").fetchone()[0] == 1


@pytest.mark.parametrize("quota", ["session_quota", "store_quota"])
def test_either_quota_refuses_before_persisting_intent(tmp_path, quota):
    exact = store.SESSION_METADATA + store.CALL_METADATA + LIMITS.reservation
    limits = replace(
        LIMITS, **{quota: exact - 1 + (store.STORE_METADATA if quota == "store_quota" else 0)}
    )
    with store.SharedStore(tmp_path / "db", "one", PROFILE, limits, Clock()) as db:
        with pytest.raises(store.Refused, match="quota"):
            db.begin("a", b"code")
        assert db.db.execute("SELECT count(*) FROM calls").fetchone()[0] == 0
        assert db.usage("one") == store.SESSION_METADATA


@pytest.mark.parametrize("value", [0, -1, True, 1.5, 2**63])
def test_invalid_quotas(value):
    with pytest.raises(store.Refused):
        replace(LIMITS, store_quota=value)


def test_failed_capture_keeps_reservation_and_previous_checkpoint(tmp_path):
    with store.SharedStore(tmp_path / "db", "one", PROFILE, LIMITS, Clock()) as db:
        publish(db, tmp_path / "a")
        db.begin("b", b"new")
        usage = db.usage()
        with pytest.raises(store.Refused, match="limit"):
            db.commit("b", checkpoint(tmp_path / "b", b"x" * 1025), b"new")
        assert db.usage() == usage
        assert db.restore(tmp_path / "restored") == "a"
        assert db.begin("a", b"code") == b"result"
    with store.SharedStore(tmp_path / "db", "one", PROFILE, LIMITS, Clock()) as db:
        assert row(db, "b")["status"] == "interrupted"
        assert db.usage() == usage
        with pytest.raises(store.Refused, match="recovery"):
            db.begin("c", b"next")
        with pytest.raises(store.Refused, match="reserved"):
            db.commit("b", tmp_path / "a", b"new")


def test_result_expiry_retains_identity_and_current_checkpoint(tmp_path):
    clock = Clock()
    with store.SharedStore(tmp_path / "db", "one", PROFILE, LIMITS, clock) as db:
        publish(db, tmp_path / "a")
        before = db.usage()
        clock.advance(10)
        with pytest.raises(store.Refused, match="result_expired"):
            db.begin("a", b"code")
        assert db.usage() == before - len(b"result")
        assert row(db)["status"] == "committed"
        assert db.restore(tmp_path / "restored") == "a"
        clock.utc -= 100 * store.SECOND
        with pytest.raises(store.Refused, match="result_expired"):
            db.begin("a", b"code")
        with pytest.raises(store.Refused, match="different request"):
            db.begin("a", b"different")


def test_retry_does_not_move_deadline_or_need_a_new_reservation(tmp_path):
    clock = Clock()
    with store.SharedStore(tmp_path / "db", "one", PROFILE, LIMITS, clock) as db:
        publish(db, tmp_path / "a")
        expires = row(db)["expires"]
        clock.advance(9)
        assert db.begin("a", b"code") == b"result"
        assert row(db)["expires"] == expires
        assert db.db.execute("SELECT count(*) FROM reservations").fetchone()[0] == 0


def test_forward_jump_cannot_start_late_grace(tmp_path):
    clock = Clock()
    with store.SharedStore(tmp_path / "db", "one", PROFILE, LIMITS, clock) as db:
        publish(db, tmp_path / "a")
        clock.utc += 100 * store.SECOND
        with pytest.raises(store.Refused, match="result_expired"):
            db.begin("a", b"code")


def test_prepaid_grant_shared_and_budget_survives_owner_changes(tmp_path):
    clock = Clock()
    root = tmp_path / "db"
    with store.SharedStore(root, "one", PROFILE, LIMITS, clock) as db:
        publish(db, tmp_path / "a")
        clock.utc += 10 * store.SECOND
        assert db.begin("a", b"code") == b"result"
        assert row(db)["remaining"] == 2 * store.SECOND
        assert db.begin("a", b"code") == b"result"
        assert row(db)["remaining"] == 2 * store.SECOND
    for remaining in (store.SECOND, 0):
        with store.SharedStore(root, "one", PROFILE, LIMITS, clock) as db:
            assert db.begin("a", b"code") == b"result"
            assert row(db)["remaining"] == remaining
    with store.SharedStore(root, "one", PROFILE, LIMITS, clock) as db:
        with pytest.raises(store.Refused, match="result_expired"):
            db.begin("a", b"code")


def test_monotonic_grant_expires_with_frozen_utc(tmp_path):
    clock = Clock()
    with store.SharedStore(tmp_path / "db", "one", PROFILE, LIMITS, clock) as db:
        publish(db, tmp_path / "a")
        clock.utc += 10 * store.SECOND
        for _ in range(3):
            assert db.begin("a", b"code") == b"result"
            clock.monotonic += store.SECOND
        assert db.expire() == 1
        assert db.expire() == 0


def test_old_policy_survives_configuration_change_and_later_checkpoint(tmp_path):
    root = tmp_path / "db"
    clock = Clock()
    with store.SharedStore(root, "one", PROFILE, LIMITS, clock) as db:
        publish(db, tmp_path / "a")
    with store.SharedStore(
        root, "one", PROFILE, replace(LIMITS, retention_seconds=1, grace_seconds=0), clock
    ) as db:
        publish(db, tmp_path / "b", call="b")
        clock.advance(2)
        assert db.expire() == 1
        assert db.begin("a", b"code") == b"result"
        assert row(db)["expires"] == 110 * store.SECOND
        assert row(db)["grace"] == 3 * store.SECOND
        assert db.restore(tmp_path / "restored") == "b"


def test_stale_generation_cannot_reserve_publish_or_expire(tmp_path):
    with store.SharedStore(tmp_path / "db", "one", PROFILE, LIMITS, Clock()) as db:
        publish(db, tmp_path / "a")
        db.db.execute("UPDATE sessions SET generation=generation+1")
        for operation in (
            lambda: db.begin("b", b"x"),
            lambda: db.commit("b", tmp_path / "a", b"x"),
            db.expire,
        ):
            with pytest.raises(store.Refused, match="generation"):
                operation()


@pytest.mark.parametrize("legacy_name", ["state.sqlite", "shared.sqlite"])
def test_unknown_formats_refuse_without_modifying_database(tmp_path, legacy_name):
    root = tmp_path / "db"
    root.mkdir()
    path = root / legacy_name
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE old_format(value)")
    before = path.read_bytes()
    with pytest.raises(store.Refused, match="migration|format"):
        store.SharedStore(root, "one", PROFILE, LIMITS, Clock())
    assert path.read_bytes() == before
    if legacy_name == "state.sqlite":
        assert not (root / "shared.sqlite").exists()


def test_corrupt_chunk_replaced_transactionally(tmp_path):
    with store.SharedStore(tmp_path / "db", "one", PROFILE, LIMITS, Clock()) as db:
        publish(db, tmp_path / "a")
        db.db.execute("UPDATE chunks SET data=?", (b"bad",))
        db.begin("b", b"new")

        def stop(point):
            if point == "before_commit":
                raise RuntimeError("stop")

        with pytest.raises(RuntimeError):
            db.commit("b", checkpoint(tmp_path / "b"), b"new", stop)
        assert db.db.execute("SELECT data FROM chunks").fetchone()[0] == b"bad"
        db.commit("b", tmp_path / "b", b"new")
        assert db.restore(tmp_path / "restored") == "b"


@pytest.mark.parametrize("point", ["checkpoint_stored", "before_commit", "after_commit"])
def test_process_death_keeps_publication_and_accounting_atomic(tmp_path, point):
    root = tmp_path / "db"
    with store.SharedStore(root, "one", PROFILE, LIMITS) as db:
        publish(db, tmp_path / "a")
        old_usage = db.usage()
    new = checkpoint(tmp_path / "new", b"new-state")
    program = """
import os, sys
from pathlib import Path
from scripts.experiments.mxc_session_patch.shared_store import Limits, SharedStore
limits = Limits(200000, 500000, checkpoint_bytes=1024, result_bytes=128, files=2, retention_seconds=3600, grace_seconds=3)
with SharedStore(Path(sys.argv[1]), 'one', {'runtime':'pinned','policy':'closed','machine':'local'}, limits) as db:
    db.begin('b', b'new')
    def crash(point):
        if point == sys.argv[3]: os._exit(73)
    db.commit('b', Path(sys.argv[2]), b'new-result', crash)
"""
    child = subprocess.run(
        [sys.executable, "-c", program, str(root), str(new), point], timeout=20, check=False
    )
    assert child.returncode == 73
    with store.SharedStore(root, "one", PROFILE, LIMITS) as db:
        committed = point == "after_commit"
        assert db.restore(tmp_path / "restored") == ("b" if committed else "a")
        if committed:
            assert db.begin("b", b"new") == b"new-result"
            assert db.db.execute("SELECT count(*) FROM reservations").fetchone()[0] == 0
        else:
            assert db.usage() == old_usage + store.CALL_METADATA + LIMITS.reservation
            with pytest.raises(store.Refused, match="recovery"):
                db.begin("b", b"new")


def test_competing_processes_cannot_overbook_shared_quota(tmp_path):
    root = tmp_path / "db"
    quota = (
        store.STORE_METADATA + 2 * store.SESSION_METADATA + store.CALL_METADATA + LIMITS.reservation
    )
    limits = replace(LIMITS, store_quota=quota)
    for session in ("one", "two"):
        with store.SharedStore(root, session, PROFILE, limits):
            pass
    program = """
import sys, sqlite3
from pathlib import Path
from scripts.experiments.mxc_session_patch.shared_store import Limits, SharedStore, Refused
limits = Limits(200000, int(sys.argv[3]), checkpoint_bytes=1024, result_bytes=128, files=2, retention_seconds=10, grace_seconds=3)
try:
    with SharedStore(Path(sys.argv[1]), sys.argv[2], {'runtime':'pinned','policy':'closed','machine':'local'}, limits) as db:
        print('ready', flush=True)
        sys.stdin.readline()
        db.begin('a', b'code')
    print('reserved')
except (Refused, sqlite3.OperationalError, OSError):
    print('refused')
"""
    children = []
    try:
        # Initialize sequentially, then release both contenders into admission together.
        for session in ("one", "two"):
            child = subprocess.Popen(
                [sys.executable, "-c", program, str(root), session, str(quota)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            children.append(child)
            assert child.stdout is not None
            assert child.stdout.readline().strip() == "ready"
        for child in children:
            assert child.stdin is not None
            child.stdin.write("go\n")
            child.stdin.flush()
        outputs = [child.communicate(timeout=20)[0].strip() for child in children]
        assert sorted(outputs) == ["refused", "reserved"]
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=20)
    with store.SharedStore(root, "one", PROFILE, limits) as db:
        assert db.usage() == quota


@pytest.mark.parametrize("point", ["before_grace_commit", "after_grace_commit"])
def test_process_death_does_not_refund_committed_grace(tmp_path, point):
    root = tmp_path / "db"
    with store.SharedStore(root, "one", PROFILE, LIMITS, Clock()) as db:
        publish(db, tmp_path / "a")
    program = """
import os, sys
from pathlib import Path
from scripts.experiments.mxc_session_patch.shared_store import Limits, SharedStore
class Clock:
    def utc_ns(self): return 110_000_000_000
    def monotonic_ns(self): return 100_000_000_000
limits = Limits(200000, 500000, checkpoint_bytes=1024, result_bytes=128, files=2, retention_seconds=10, grace_seconds=3)
with SharedStore(Path(sys.argv[1]), 'one', {'runtime':'pinned','policy':'closed','machine':'local'}, limits, Clock()) as db:
    original = db._available
    def available(*args):
        answer = original(*args)
        if sys.argv[2] == 'before_grace_commit': os._exit(74)
        return answer
    db._available = available
    assert db.begin('a', b'code') == b'result'
    os._exit(74)
"""
    child = subprocess.run(
        [sys.executable, "-c", program, str(root), point], timeout=20, check=False
    )
    assert child.returncode == 74
    with store.SharedStore(root, "one", PROFILE, LIMITS, Clock(110 * store.SECOND)) as db:
        expected = 3 if point == "before_grace_commit" else 2
        assert row(db)["remaining"] == expected * store.SECOND
        assert db.begin("a", b"code") == b"result"
        assert row(db)["remaining"] == (expected - 1) * store.SECOND


def test_clock_rollback_does_not_reuse_or_refund_a_grant(tmp_path):
    clock = Clock()
    with store.SharedStore(tmp_path / "db", "one", PROFILE, LIMITS, clock) as db:
        publish(db, tmp_path / "a")
        clock.utc += 10 * store.SECOND
        assert db.begin("a", b"code") == b"result"
        clock.utc -= 5 * store.SECOND
        clock.monotonic += 2 * store.SECOND
        assert db.begin("a", b"code") == b"result"
        assert row(db)["remaining"] == 2 * store.SECOND
        clock.utc += 5 * store.SECOND
        assert db.begin("a", b"code") == b"result"
        assert row(db)["remaining"] == store.SECOND


def test_failed_grace_transaction_keeps_budget_and_refuses_delivery(tmp_path, monkeypatch):
    from contextlib import contextmanager

    clock = Clock()
    with store.SharedStore(tmp_path / "db", "one", PROFILE, LIMITS, clock) as db:
        publish(db, tmp_path / "a")
        clock.utc += 10 * store.SECOND
        original = db._transaction

        @contextmanager
        def failed_commit():
            with original():
                yield
                raise sqlite3.OperationalError("disk full")

        with monkeypatch.context() as patch:
            patch.setattr(db, "_transaction", failed_commit)
            with pytest.raises(sqlite3.OperationalError, match="disk full"):
                db.begin("a", b"code")
        assert row(db)["remaining"] == 3 * store.SECOND
        assert db.grants == {}
        assert db.begin("a", b"code") == b"result"
        assert row(db)["remaining"] == 2 * store.SECOND


def test_slow_grace_commit_does_not_extend_grant(tmp_path, monkeypatch):
    from contextlib import contextmanager

    clock = Clock()
    with store.SharedStore(tmp_path / "db", "one", PROFILE, LIMITS, clock) as db:
        publish(db, tmp_path / "a")
        clock.utc += 10 * store.SECOND
        original = db._transaction

        @contextmanager
        def slow_commit():
            with original():
                yield
                clock.monotonic += 2 * store.SECOND

        with monkeypatch.context() as patch:
            patch.setattr(db, "_transaction", slow_commit)
            with pytest.raises(store.Refused, match="grant elapsed"):
                db.begin("a", b"code")
        assert row(db)["remaining"] == 2 * store.SECOND


@pytest.mark.parametrize("corruption", ["result", "inventory", "chunk"])
def test_corrupt_state_refuses_without_fresh_fallback(tmp_path, corruption):
    with store.SharedStore(tmp_path / "db", "one", PROFILE, LIMITS, Clock()) as db:
        publish(db, tmp_path / "a")
        if corruption == "result":
            db.db.execute("UPDATE calls SET result=?", (b"broken",))
            with pytest.raises(store.Refused, match="result"):
                db.begin("a", b"code")
        else:
            if corruption == "inventory":
                db.db.execute("DELETE FROM file_chunks")
            else:
                db.db.execute("UPDATE chunks SET data=?", (b"broken",))
            with pytest.raises(store.Refused, match="chunk"):
                db.restore(tmp_path / "restored")
        assert row(db)["status"] == "committed"


@pytest.mark.parametrize("grace", [0, 3])
def test_exact_outer_deadline_is_terminal(tmp_path, grace):
    clock = Clock()
    limits = replace(LIMITS, grace_seconds=grace)
    with store.SharedStore(tmp_path / "db", "one", PROFILE, limits, clock) as db:
        publish(db, tmp_path / "a")
        clock.utc += (10 + grace) * store.SECOND
        with pytest.raises(store.Refused, match="result_expired"):
            db.begin("a", b"code")
        assert row(db)["expired"] == 1
        assert row(db)["result"] is None


def test_expiry_batch_is_bounded_and_preserves_latest_checkpoint(tmp_path):
    clock = Clock()
    with store.SharedStore(tmp_path / "db", "one", PROFILE, LIMITS, clock) as db:
        for call in ("a", "b", "c"):
            publish(db, tmp_path / call, call=call)
        clock.advance(10)
        assert db.expire(limit=1) == 1
        assert db.expire(limit=1) == 1
        assert db.expire(limit=1) == 1
        assert db.expire(limit=1) == 0
        assert db.restore(tmp_path / "restored") == "c"


def test_metadata_quota_refusal_creates_no_store(tmp_path):
    root = tmp_path / "db"
    with pytest.raises(store.Refused, match="metadata"):
        store.SharedStore(root, "one", PROFILE, replace(LIMITS, session_quota=1))
    assert not root.exists()


def test_collection_preserves_old_results_and_latest_state(tmp_path):
    clock = Clock()
    with store.SharedStore(tmp_path / "db", "one", PROFILE, LIMITS, clock) as db:
        publish(db, tmp_path / "a", result=b"saved chart bytes")
        db.begin("b", b"next")
        db.commit("b", checkpoint(tmp_path / "b", b"new state"), b"new result")
        old_charge = row(db)["checkpoint_charge"]
        usage = db.usage()
        assert db.collect_checkpoints() == 1
        assert db.usage() == usage - old_charge
        assert db.begin("a", b"code") == b"saved chart bytes"
        assert row(db)["checkpoint_charge"] == 0
        assert row(db)["checkpoint_hash"] is not None
        assert db.restore(tmp_path / "restored") == "b"
        assert (tmp_path / "restored/index.json").read_bytes() == b"new state"
        assert db.collect_checkpoints() == 0
        clock.advance(13)
        with pytest.raises(store.Refused, match="result_expired"):
            db.begin("a", b"code")
        assert db.restore(tmp_path / "still-current") == "b"


def test_shared_chunks_remain_until_last_checkpoint_reference_is_removed(tmp_path):
    root = tmp_path / "db"
    with store.SharedStore(root, "one", PROFILE, LIMITS, Clock()) as one:
        with store.SharedStore(root, "two", PROFILE, LIMITS, Clock()) as two:
            publish(one, tmp_path / "one-a")
            publish(two, tmp_path / "two-a")
            one.begin("b", b"next")
            one.commit("b", checkpoint(tmp_path / "one-b", b"different"), b"new")
            assert one.collect_checkpoints() == 1
            assert one.db.execute("SELECT count(*) FROM chunks").fetchone()[0] == 2
            assert two.restore(tmp_path / "two-restored") == "a"
            two.retire()
            assert two.collect_checkpoints() == 1
            assert one.db.execute("SELECT count(*) FROM chunks").fetchone()[0] == 1
            assert one.restore(tmp_path / "one-restored") == "b"
            assert two.begin("a", b"code") == b"result"


def test_retired_session_cannot_be_recreated_and_retains_delivery(tmp_path):
    root = tmp_path / "db"
    clock = Clock()
    with store.SharedStore(root, "one", PROFILE, LIMITS, clock) as db:
        publish(db, tmp_path / "a")
        db.retire()
        db.retire()
        with pytest.raises(store.Refused, match="retired"):
            db.begin("b", b"new")
        with pytest.raises(store.Refused, match="retired"):
            db.restore(tmp_path / "restored")
        assert db.collect_checkpoints() == 1
        assert (
            db.usage()
            == store.STORE_METADATA + store.SESSION_METADATA + store.CALL_METADATA + len(b"result")
        )
        assert db.begin("a", b"code") == b"result"
    with store.SharedStore(root, "one", PROFILE, LIMITS, clock) as db:
        with pytest.raises(store.Refused, match="retired"):
            db.begin("b", b"new")
        assert db.begin("a", b"code") == b"result"
        clock.advance(13)
        assert db.expire() == 1
        assert db.usage() == store.STORE_METADATA + store.SESSION_METADATA + store.CALL_METADATA
        with pytest.raises(store.Refused, match="result_expired"):
            db.begin("a", b"code")
        assert db.collect_checkpoints() == 0


def test_retirement_blocks_pending_publication_but_does_not_release_reservations(tmp_path):
    root = tmp_path / "db"
    with store.SharedStore(root, "one", PROFILE, LIMITS, Clock()) as db:
        publish(db, tmp_path / "a")
        db.begin("b", b"new")
        usage = db.usage()
        db.retire()
        with pytest.raises(store.Refused, match="retired"):
            db.commit("b", checkpoint(tmp_path / "b"), b"new")
        with pytest.raises(store.Refused, match="recovery"):
            db.collect_checkpoints()
        assert db.usage() == usage
        assert db.begin("a", b"code") == b"result"
        assert row(db)["checkpoint_charge"] > 0
    with store.SharedStore(root, "one", PROFILE, LIMITS, Clock()) as db:
        assert db.usage() == usage
        with pytest.raises(store.Refused, match="recovery"):
            db.collect_checkpoints()
        with pytest.raises(store.Refused, match="retired"):
            db.begin("c", b"new")


def test_collection_releases_logical_capacity_even_for_deduplicated_content(tmp_path):
    checkpoint_charge = len(b"checkpoint") + store.FILE_METADATA + store.REFERENCE_METADATA
    retained = 2 * (store.CALL_METADATA + checkpoint_charge + len(b"result"))
    quota = (
        store.SESSION_METADATA
        + retained
        + store.CALL_METADATA
        + LIMITS.reservation
        - checkpoint_charge
    )
    limits = replace(LIMITS, session_quota=quota, store_quota=quota + store.STORE_METADATA)
    with store.SharedStore(tmp_path / "db", "one", PROFILE, limits, Clock()) as db:
        publish(db, tmp_path / "a")
        publish(db, tmp_path / "b", call="b")
        with pytest.raises(store.Refused, match="quota"):
            db.begin("c", b"new")
        assert db.collect_checkpoints() == 1
        assert db.begin("c", b"new") is None
        assert db.usage() == limits.store_quota
        assert db.begin("a", b"code") == b"result"


@pytest.mark.parametrize(
    "corruption", ["current", "manifest", "chunk", "missing_file", "charge", "orphan_chunk"]
)
def test_corrupt_store_refuses_destructive_collection(tmp_path, corruption):
    with store.SharedStore(tmp_path / "db", "one", PROFILE, LIMITS, Clock()) as db:
        publish(db, tmp_path / "a")
        publish(db, tmp_path / "b", call="b")
        if corruption == "current":
            db.db.execute("UPDATE sessions SET current_call=NULL")
        elif corruption == "manifest":
            db.db.execute("UPDATE calls SET checkpoint_hash=? WHERE id='b'", ("0" * 64,))
        elif corruption == "chunk":
            db.db.execute("UPDATE chunks SET data=?", (b"broken",))
        elif corruption == "missing_file":
            db.db.execute("DELETE FROM file_chunks WHERE call='b'")
            db.db.execute("DELETE FROM files WHERE call='b'")
        elif corruption == "charge":
            db.db.execute("UPDATE calls SET checkpoint_charge=checkpoint_charge+1 WHERE id='b'")
        else:
            db.db.execute("INSERT INTO chunks VALUES(?,?)", ("0" * 64, b"unknown"))
        before = list(db.db.iterdump())
        with pytest.raises(store.Refused):
            db.collect_checkpoints()
        assert list(db.db.iterdump()) == before
        assert row(db)["checkpoint_charge"] > 0


def test_corrupt_root_in_another_session_blocks_collection(tmp_path):
    with store.SharedStore(tmp_path / "db", "one", PROFILE, LIMITS, Clock()) as one:
        with store.SharedStore(tmp_path / "db", "two", PROFILE, LIMITS, Clock()) as two:
            publish(one, tmp_path / "a")
            publish(one, tmp_path / "b", call="b")
            publish(two, tmp_path / "two")
            two.db.execute("UPDATE calls SET checkpoint_hash=? WHERE session='two'", ("0" * 64,))
            usage = one.usage()
            with pytest.raises(store.Refused, match="manifest"):
                one.collect_checkpoints()
            assert one.usage() == usage


def test_collection_batch_and_rollback(tmp_path):
    with store.SharedStore(tmp_path / "db", "one", PROFILE, LIMITS, Clock()) as db:
        for call in ("a", "b", "c"):
            publish(db, tmp_path / call, call=call)
        before = list(db.db.iterdump())

        def fail(point):
            if point == "before_collection_commit":
                raise sqlite3.OperationalError("disk full")

        with pytest.raises(sqlite3.OperationalError, match="disk full"):
            db.collect_checkpoints(limit=2, boundary=fail)
        assert list(db.db.iterdump()) == before
        assert db.collect_checkpoints(limit=1) == 1
        assert db.collect_checkpoints(limit=1) == 1
        assert db.collect_checkpoints(limit=1) == 0
        assert db.restore(tmp_path / "restored") == "c"


@pytest.mark.parametrize(
    "point", ["before_collection", "before_collection_commit", "after_collection_commit"]
)
def test_process_death_keeps_collection_and_accounting_atomic(tmp_path, point):
    root = tmp_path / "db"
    limits = replace(LIMITS, retention_seconds=3600)
    with store.SharedStore(root, "one", PROFILE, limits) as db:
        publish(db, tmp_path / "a")
        db.begin("b", b"new")
        db.commit("b", checkpoint(tmp_path / "b", b"different"), b"new")
        usage = db.usage()
        old_charge = row(db)["checkpoint_charge"]
    program = """
import os, sys
from pathlib import Path
from scripts.experiments.mxc_session_patch.shared_store import Limits, SharedStore
limits = Limits(200000, 500000, checkpoint_bytes=1024, result_bytes=128, files=2, retention_seconds=3600, grace_seconds=3)
with SharedStore(Path(sys.argv[1]), 'one', {'runtime':'pinned','policy':'closed','machine':'local'}, limits) as db:
    def crash(point):
        if point == sys.argv[2]: os._exit(76)
    db.collect_checkpoints(boundary=crash)
"""
    child = subprocess.run(
        [sys.executable, "-c", program, str(root), point], timeout=20, check=False
    )
    assert child.returncode == 76
    with store.SharedStore(root, "one", PROFILE, limits) as db:
        committed = point == "after_collection_commit"
        assert db.usage() == usage - (old_charge if committed else 0)
        assert db.restore(tmp_path / "restored") == "b"
        assert db.begin("a", b"code") == b"result"
        assert db.collect_checkpoints() == (0 if committed else 1)
        assert db.usage() == usage - old_charge


@pytest.mark.parametrize("point", ["before_retire_commit", "after_retire_commit"])
def test_process_death_preserves_retirement_decision(tmp_path, point):
    root = tmp_path / "db"
    limits = replace(LIMITS, retention_seconds=3600)
    with store.SharedStore(root, "one", PROFILE, limits) as db:
        publish(db, tmp_path / "a")
    program = """
import os, sys
from pathlib import Path
from scripts.experiments.mxc_session_patch.shared_store import Limits, SharedStore
limits = Limits(200000, 500000, checkpoint_bytes=1024, result_bytes=128, files=2, retention_seconds=3600, grace_seconds=3)
with SharedStore(Path(sys.argv[1]), 'one', {'runtime':'pinned','policy':'closed','machine':'local'}, limits) as db:
    def crash(point):
        if point == sys.argv[2]: os._exit(77)
    db.retire(boundary=crash)
"""
    child = subprocess.run(
        [sys.executable, "-c", program, str(root), point], timeout=20, check=False
    )
    assert child.returncode == 77
    with store.SharedStore(root, "one", PROFILE, limits) as db:
        assert db.begin("a", b"code") == b"result"
        if point == "after_retire_commit":
            with pytest.raises(store.Refused, match="retired"):
                db.begin("b", b"new")
            assert db.collect_checkpoints() == 1
        else:
            assert db.restore(tmp_path / "restored") == "a"
            assert db.begin("b", b"new") is None


@pytest.mark.parametrize("version", [1, 2])
def test_previous_experimental_format_requires_explicit_migration(tmp_path, version):
    root = tmp_path / "db"
    with store.SharedStore(root, "one", PROFILE, LIMITS, Clock()) as db:
        publish(db, tmp_path / "a")
        db.db.execute("UPDATE settings SET version=?", (version,))
    before = (root / "shared.sqlite").read_bytes()
    with pytest.raises(store.Refused, match="format"):
        store.SharedStore(root, "one", PROFILE, LIMITS, Clock())
    assert (root / "shared.sqlite").read_bytes() == before
