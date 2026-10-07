"""Independent charge reconciliation across admission, recovery and schema upgrade."""

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

PROFILE = {"runtime": "pinned"}
LIMITS = store.Limits(
    500_000,
    1_000_000,
    checkpoint_bytes=1024,
    result_bytes=128,
    files=2,
    retention_seconds=10,
    grace_seconds=0,
)


class Clock:
    value = 100 * store.SECOND

    def utc_ns(self):
        return self.value

    def monotonic_ns(self):
        return self.value


def reconcile(db):
    expected_store = store.STORE_METADATA
    for (session,) in db.db.execute("SELECT id FROM sessions"):
        count, payloads = db.db.execute(
            "SELECT count(*),coalesce(sum(result_charge+checkpoint_charge),0) FROM calls WHERE session=?",
            (session,),
        ).fetchone()
        reserved = db.db.execute(
            "SELECT coalesce(sum(charge),0) FROM reservations WHERE session=?", (session,)
        ).fetchone()[0]
        expected = store.SESSION_METADATA + count * store.CALL_METADATA + payloads + reserved
        assert db.usage(session) == expected
        expected_store += expected
    assert db.usage() == expected_store


def candidate(path):
    path.mkdir()
    (path / "index.json").write_bytes(b"checkpoint")
    return path


def legacy(root, tmp_path):
    with store.SharedStore(root, "one", PROFILE, LIMITS, Clock()) as db:
        db.begin("a", b"code")
        db.commit("a", candidate(tmp_path / "a"), b"saved")
        db.begin("pending", b"next")
        records = [tuple(row) for row in db.db.execute("SELECT * FROM calls ORDER BY id")]
        with db._transaction():
            names = list(
                db.db.execute(
                    "SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'logical_%'"
                )
            )
            for (name,) in names:
                db.db.execute(f"DROP TRIGGER {name}")
            db.db.execute("DROP TABLE logical_usage")
            db.db.execute("UPDATE settings SET version=3")
    return records


def test_totals_follow_each_transition_and_failed_publication(tmp_path):
    clock = Clock()
    root = tmp_path / "db"
    with store.SharedStore(root, "one", PROFILE, LIMITS, clock) as db:
        reconcile(db)
        for name in ("a", "b"):
            assert db.begin(name, name.encode()) is None
            reconcile(db)
            snapshot = candidate(tmp_path / name)

            def fail(point):
                if point == "before_commit":
                    raise RuntimeError("rollback")

            with pytest.raises(RuntimeError):
                db.commit(name, snapshot, b"saved", fail)
            reconcile(db)
            db.commit(name, snapshot, b"saved")
            reconcile(db)
        with store.SharedStore(root, "two", PROFILE, LIMITS, clock) as other:
            other.begin("pending", b"code")
            reconcile(db)
        assert db.collect_checkpoints() == 1
        reconcile(db)
        clock.value += 20 * store.SECOND
        assert db.expire() == 2
        reconcile(db)
        db.retire()
        reconcile(db)
        assert db.collect_checkpoints() == 1
        reconcile(db)
        assert db.usage("one") == store.SESSION_METADATA + 2 * store.CALL_METADATA
    with store.SharedStore(root, "one", PROFILE, LIMITS, clock) as db:
        reconcile(db)
        db.audit_usage()


@pytest.mark.parametrize(
    "operation", ["admission", "publication", "expiry", "collection", "retirement"]
)
@pytest.mark.parametrize("after_commit", [False, True])
def test_process_exit_keeps_totals_equal_to_source_rows(tmp_path, operation, after_commit):
    root = tmp_path / "db"
    with store.SharedStore(root, "one", PROFILE, LIMITS, Clock()) as db:
        db.begin("a", b"code")
        db.commit("a", candidate(tmp_path / "a"), b"saved")
        db.begin("b", b"code")
        db.commit("b", candidate(tmp_path / "b"), b"saved")
    candidate(tmp_path / "c")
    program = f"""
import os
from contextlib import contextmanager
from pathlib import Path
from scripts.experiments.mxc_session_patch.shared_store import SharedStore, Limits
with SharedStore(Path({str(root)!r}), 'one', {PROFILE!r}, {LIMITS!r}) as db:
    if {operation!r} == 'publication': db.begin('c', b'code')
    original = db._transaction
    @contextmanager
    def crash():
        with original():
            yield
            if not {after_commit!r}: os._exit(73)
        os._exit(73)
    db._transaction = crash
    if {operation!r} == 'admission': db.begin('c', b'code')
    elif {operation!r} == 'publication': db.commit('c', Path({str(tmp_path / "c")!r}), b'new')
    elif {operation!r} == 'expiry': db.expire()
    elif {operation!r} == 'collection': db.collect_checkpoints()
    else: db.retire()
"""
    result = subprocess.run([sys.executable, "-c", program], capture_output=True, timeout=20)
    assert result.returncode == 73, result.stderr.decode()
    with store.SharedStore(root, "one", PROFILE, LIMITS) as db:
        reconcile(db)
        db.audit_usage()
        if operation == "publication":
            assert db.db.execute("SELECT status FROM calls WHERE id='c'").fetchone()[0] == (
                "committed" if after_commit else "interrupted"
            )
            if after_commit:
                assert db.begin("c", b"code") == b"new"


def test_explicit_upgrade_preserves_records_and_reservations(tmp_path):
    root = tmp_path / "db"
    old = legacy(root, tmp_path)
    with pytest.raises(store.Refused, match="explicit upgrade"):
        store.SharedStore(root, "one", PROFILE, LIMITS, Clock())
    with store.SharedStore(root, "one", PROFILE, LIMITS, Clock(), upgrade_accounting=True) as db:
        assert db.db.execute("SELECT version FROM settings").fetchone()[0] == 4
        new = [tuple(row) for row in db.db.execute("SELECT * FROM calls ORDER BY id")]
        assert new[0][:13] == old[0][:13]
        assert new[1][0:3] == old[1][0:3]
        assert new[1][3] == "interrupted"
        assert db.begin("a", b"code") == b"saved"
        assert db.db.execute("SELECT count(*) FROM reservations").fetchone()[0] == 1
        reconcile(db)
    with store.SharedStore(root, "one", PROFILE, LIMITS, Clock()) as db:
        reconcile(db)


@pytest.mark.parametrize("after_commit", [False, True])
def test_upgrade_is_atomic_across_process_exit(tmp_path, after_commit):
    root = tmp_path / "db"
    legacy(root, tmp_path)
    program = f"""
import os
from pathlib import Path
from scripts.experiments.mxc_session_patch import accounting
from scripts.experiments.mxc_session_patch.shared_store import SharedStore, Limits
original = accounting.initialize
def initialize(db):
    original(db)
    if not {after_commit!r}: os._exit(74)
accounting.initialize = initialize
with SharedStore(Path({str(root)!r}), 'one', {PROFILE!r}, {LIMITS!r}, upgrade_accounting=True):
    os._exit(74)
"""
    result = subprocess.run([sys.executable, "-c", program], capture_output=True, timeout=20)
    assert result.returncode == 74, result.stderr.decode()
    with sqlite3.connect(root / "shared.sqlite") as connection:
        assert connection.execute("SELECT version FROM settings").fetchone()[0] == (
            4 if after_commit else 3
        )
        assert (
            bool(
                connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE name='logical_usage'"
                ).fetchone()
            )
            == after_commit
        )
    with store.SharedStore(root, "one", PROFILE, LIMITS, upgrade_accounting=True) as db:
        reconcile(db)


@pytest.mark.parametrize(
    "corruption",
    [
        "total",
        "missing_session",
        "missing_store",
        "missing_trigger",
        "changed_trigger",
        "orphan_total",
    ],
)
def test_audit_and_reopen_refuse_corrupt_accounting_without_repair(tmp_path, corruption):
    root = tmp_path / "db"
    with store.SharedStore(root, "one", PROFILE, LIMITS) as db:
        db.begin("a", b"code")
        if corruption == "total":
            db.db.execute("UPDATE logical_usage SET charge=charge-1 WHERE session='one'")
        elif corruption.startswith("missing_") and corruption != "missing_trigger":
            db.db.execute(
                "DELETE FROM logical_usage WHERE session=?",
                ("one" if corruption == "missing_session" else "",),
            )
        elif corruption in ("missing_trigger", "changed_trigger"):
            db.db.execute("DROP TRIGGER logical_calls_insert")
            if corruption == "changed_trigger":
                db.db.execute(
                    "CREATE TRIGGER logical_calls_insert AFTER INSERT ON calls BEGIN SELECT 1; END"
                )
        else:
            db.db.execute("INSERT INTO logical_usage VALUES('extra',0)")
        before = list(db.db.execute("SELECT * FROM logical_usage"))
        with pytest.raises(store.Refused, match="accounting"):
            db.audit_usage()
        assert list(db.db.execute("SELECT * FROM logical_usage")) == before
    with pytest.raises(store.Refused, match="accounting"):
        store.SharedStore(root, "one", PROFILE, LIMITS)


@pytest.mark.parametrize(
    ("table", "column"),
    [
        ("sessions", "id"),
        ("calls", "session"),
        ("calls", "id"),
        ("reservations", "session"),
        ("reservations", "call"),
    ],
)
@pytest.mark.parametrize("change", ["null", "rename", "unchanged"])
def test_accounted_identities_are_immutable(tmp_path, table, column, change):
    with store.SharedStore(tmp_path / "db", "one", PROFILE, LIMITS) as db:
        if table != "sessions":
            db.begin("a", b"code")
        before = list(db.db.execute(f"SELECT * FROM {table}"))
        totals = list(db.db.execute("SELECT * FROM logical_usage ORDER BY session"))
        original = db.db.execute(f"SELECT {column} FROM {table}").fetchone()[0]
        value = {"null": None, "rename": "other", "unchanged": original}[change]
        statement = f"UPDATE {table} SET {column}=?"
        if change == "unchanged":
            db.db.execute(statement, (value,))
        else:
            with pytest.raises(sqlite3.IntegrityError, match="accounted identity is immutable"):
                db.db.execute(statement, (value,))
        assert list(db.db.execute(f"SELECT * FROM {table}")) == before
        assert list(db.db.execute("SELECT * FROM logical_usage ORDER BY session")) == totals
        db.audit_usage()
        reconcile(db)


@pytest.mark.parametrize("value", [-1, 1.5, "invalid", 2**63 - 1])
def test_invalid_source_charge_cannot_partially_update_totals(tmp_path, value):
    with store.SharedStore(tmp_path / "db", "one", PROFILE, LIMITS) as db:
        db.begin("a", b"code")
        before = db.usage()
        with pytest.raises(sqlite3.IntegrityError):
            db.db.execute("UPDATE reservations SET charge=?", (value,))
        assert db.usage() == before
        reconcile(db)


def test_underflow_refuses_the_source_update(tmp_path):
    with store.SharedStore(tmp_path / "db", "one", PROFILE, LIMITS) as db:
        db.begin("a", b"code")
        db.db.execute("UPDATE logical_usage SET charge=0 WHERE session='one'")
        with pytest.raises(sqlite3.IntegrityError):
            db.db.execute("DELETE FROM reservations")
        assert db.db.execute("SELECT charge FROM reservations").fetchone()[0] == LIMITS.reservation


def test_admission_has_no_history_aggregate_or_linear_vm_work(tmp_path):
    steps = []
    for size in (0, 5000):
        limits = replace(LIMITS, session_quota=100_000_000, store_quota=200_000_000)
        with store.SharedStore(tmp_path / str(size), "one", PROFILE, limits, Clock()) as db:
            with db._transaction():
                db.db.executemany(
                    "INSERT INTO calls(session,id,request,status,generation) VALUES('one',?,'digest','interrupted',1)",
                    ((f"old-{i}",) for i in range(size)),
                )
            reconcile(db)
            statements = []
            instructions = []
            db.db.set_trace_callback(statements.append)
            db.db.set_progress_handler(lambda: instructions.append(1) or 0, 1)
            db.begin("new", b"code")
            db.db.set_progress_handler(None, 0)
            db.db.set_trace_callback(None)
            assert not any("sum(" in sql.lower() or "count(" in sql.lower() for sql in statements)
            steps.append(len(instructions))
            reconcile(db)
    assert steps[1] <= steps[0] + 50


@pytest.mark.parametrize("point", ["before_cleanup_release", "after_cleanup_release"])
def test_recovery_release_reconciles_after_rollback_or_lost_ack(tmp_path, point):
    native = importlib.import_module("scripts.experiments.mxc_session_patch.native_journal")
    root = tmp_path / "db"
    child = subprocess.Popen(
        [sys.executable, "-c", "import sys; sys.stdin.buffer.read()"], stdin=subprocess.PIPE
    )
    try:
        with store.SharedStore(root, "one", PROFILE, LIMITS) as db:
            db.begin("a", b"code", scratch=store.ScratchLimits(40_000, 20, 80_000))
            journal = native.NativeJournal(db)
            journal.prepare("a")
            journal.arm("a", child)
            reconcile(db)
        child.kill()
        child.wait(timeout=10)
        with store.SharedStore(root, "one", PROFILE, LIMITS) as db:
            reconcile(db)

            def fail(boundary):
                if boundary == point:
                    raise RuntimeError("lost cleanup acknowledgement")

            with pytest.raises(RuntimeError):
                native.NativeJournal(db).reclaim("a", fail)
            reconcile(db)
            if point == "before_cleanup_release":
                native.NativeJournal(db).reclaim("a")
            else:
                with pytest.raises(store.Refused, match="no scratch reservation"):
                    native.NativeJournal(db).reclaim("a")
            reconcile(db)
            assert db.usage("one") == store.SESSION_METADATA + store.CALL_METADATA
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=10)
        assert child.stdin is not None
        child.stdin.close()


@pytest.mark.parametrize("corruption", ["real_charge", "quota", "inventory"])
def test_upgrade_refuses_invalid_source_without_installing_totals(tmp_path, corruption):
    root = tmp_path / "db"
    legacy(root, tmp_path)
    with sqlite3.connect(root / "shared.sqlite") as db:
        if corruption == "real_charge":
            db.execute("UPDATE calls SET result_charge=1.5 WHERE id='a'")
        elif corruption == "quota":
            db.execute("UPDATE reservations SET charge=100000000")
        else:
            db.execute("DELETE FROM file_chunks")
    with pytest.raises(store.Refused):
        store.SharedStore(root, "one", PROFILE, LIMITS, upgrade_accounting=True)
    with sqlite3.connect(root / "shared.sqlite") as db:
        assert db.execute("SELECT version FROM settings").fetchone()[0] == 3
        assert (
            db.execute("SELECT 1 FROM sqlite_master WHERE name='logical_usage'").fetchone() is None
        )


def test_benchmark_preserves_identity_charges_and_separates_measurements(tmp_path):
    benchmark = importlib.import_module(
        "scripts.experiments.mxc_session_patch.accounting_benchmark"
    )
    for scanning in (False, True):
        result = benchmark.measure(tmp_path / str(scanning), 20, 3, scanning=scanning)
        assert result["retained_identities_after"] == 23
        assert result["samples"] == 3
        assert result["admission_median_seconds"] >= result["write_lock_median_seconds"] > 0
        assert result["accounting_audit_seconds"] > 0
        assert result["collection_zero_payload_seconds"] > 0
