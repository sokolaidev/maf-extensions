"""Physical admission and real SQLite allocation failure preserve durable retries."""

from __future__ import annotations

import errno
import importlib
import os
import sqlite3
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))
try:
    store = importlib.import_module("scripts.experiments.mxc_session_patch.shared_store")
    physical = importlib.import_module("scripts.experiments.mxc_session_patch.physical")
    idle = importlib.import_module("scripts.experiments.mxc_session_patch.idle")
    journal = importlib.import_module("scripts.experiments.mxc_session_patch.native_journal")
finally:
    sys.path.remove(str(Path(__file__).parents[1]))

PROFILE = {"runtime": "pinned", "policy": "closed"}
LIMITS = store.Limits(20_000_000, 40_000_000, checkpoint_bytes=1_000_000, result_bytes=128, files=2)
POLICY = physical.Policy(256 * 1024, 128 * 1024, 64 * 1024)


def open_store(root, session="one", policy=POLICY, **kwargs):
    return store.SharedStore(root, session, PROFILE, LIMITS, physical_policy=policy, **kwargs)


def checkpoint(path, data=b"old"):
    path.mkdir()
    (path / "state").write_bytes(data)
    return path


def seed(db, tmp_path):
    db.begin("saved", b"saved")
    db.commit("saved", checkpoint(tmp_path / "saved"), b"retained")


def available(monkeypatch, free):
    monkeypatch.setattr(physical.shutil, "disk_usage", lambda _: SimpleNamespace(free=free))


def needed(db, scratch=0):
    allocated = (
        db.db.execute("PRAGMA page_count").fetchone()[0]
        * db.db.execute("PRAGMA page_size").fetchone()[0]
    )
    return (
        POLICY.database_bytes
        - allocated
        + POLICY.transaction_bytes
        + POLICY.minimum_free_bytes
        + scratch
    )


@pytest.mark.parametrize("field", ["database_bytes", "transaction_bytes", "minimum_free_bytes"])
@pytest.mark.parametrize("value", [-1, True, 1.5, 2**63])
def test_invalid_policy(field, value):
    with pytest.raises(store.Refused, match="physical"):
        replace(POLICY, **{field: value})


@pytest.mark.parametrize("field", ["database_bytes", "transaction_bytes"])
def test_zero_ceiling_or_transaction_budget_refused(field):
    with pytest.raises(store.Refused, match="physical"):
        replace(POLICY, **{field: 0})


def test_policy_reapplied_to_each_owner_and_idle_does_not_downgrade(tmp_path):
    root = tmp_path / "db"
    for session in ("one", "two", "one"):
        with open_store(root, session, idle_policy=idle.Policy(10)) as db:
            assert db.db.execute("SELECT version FROM settings").fetchone()[0] == 6
            assert (
                db.db.execute("PRAGMA max_page_count").fetchone()[0]
                * db.db.execute("PRAGMA page_size").fetchone()[0]
                == POLICY.database_bytes
            )
            assert db.db.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
            assert db.db.execute("PRAGMA synchronous").fetchone()[0] == 2
            assert db.db.execute("PRAGMA temp_store").fetchone()[0] == 2


@pytest.mark.parametrize("policy", [None, replace(POLICY, minimum_free_bytes=1)])
def test_omitted_or_changed_root_policy_refuses_without_owner_mutation(tmp_path, policy):
    root = tmp_path / "db"
    with open_store(root):
        pass
    before = (root / "shared.sqlite").read_bytes()
    with pytest.raises(store.Refused, match="configuration differs"):
        open_store(root, policy=policy)
    assert (root / "shared.sqlite").read_bytes() == before


@pytest.mark.parametrize("with_idle", [False, True])
def test_existing_unbounded_root_cannot_enable_policy(tmp_path, with_idle):
    root = tmp_path / "db"
    with open_store(root, policy=None, idle_policy=idle.Policy(10) if with_idle else None):
        before = (root / "shared.sqlite").read_bytes()
        with pytest.raises(store.Refused, match="new store root"):
            open_store(root, "two")
        assert (root / "shared.sqlite").read_bytes() == before


def test_non_page_aligned_ceiling_refuses(tmp_path):
    with pytest.raises(store.Refused, match="multiple"):
        open_store(
            tmp_path / "db", policy=replace(POLICY, database_bytes=POLICY.database_bytes + 1)
        )


def test_new_root_low_space_refuses_before_creating_schema(tmp_path, monkeypatch):
    available(monkeypatch, 0)
    with pytest.raises(store.Refused, match="headroom"):
        open_store(tmp_path / "db")
    assert not (tmp_path / "db/shared.sqlite").exists()
    available(monkeypatch, 10**9)
    with open_store(tmp_path / "db") as db:
        assert db.db.execute("SELECT version FROM settings").fetchone()[0] == 6


def test_admission_boundary_and_retry_when_volume_is_low(tmp_path, monkeypatch):
    with open_store(tmp_path / "db") as db:
        seed(db, tmp_path)
        available(monkeypatch, needed(db) - 1)
        with pytest.raises(store.Refused, match="headroom"):
            db.begin("late", b"late")
        assert db.db.execute("SELECT 1 FROM calls WHERE id='late'").fetchone() is None
        available(monkeypatch, 0)
        assert db.begin("saved", b"saved") == b"retained"
        available(monkeypatch, needed(db))
        assert db.begin("exact", b"exact") is None


def test_other_session_scratch_allowances_are_included(tmp_path, monkeypatch):
    root = tmp_path / "db"
    scratch = store.ScratchLimits(100_000, 20, 1_000_000)
    with open_store(root, "one") as one, open_store(root, "two") as two:
        one.begin("one", b"one", scratch=scratch)
        available(monkeypatch, needed(two, 2 * scratch.bytes) - 1)
        with pytest.raises(store.Refused, match="headroom"):
            two.begin("two", b"two", scratch=scratch)
        assert two.db.execute("SELECT count(*) FROM launches").fetchone()[0] == 1
        available(monkeypatch, needed(two, 2 * scratch.bytes))
        two.begin("two", b"two", scratch=scratch)


def test_prepare_rechecks_headroom_and_preserves_allowance(tmp_path, monkeypatch):
    with open_store(tmp_path / "db") as db:
        db.begin("a", b"a", scratch=store.ScratchLimits(100_000, 20, 1_000_000))
        charge = db.usage()
        available(monkeypatch, 0)
        with pytest.raises(store.Refused, match="headroom"):
            journal.NativeJournal(db).prepare("a")
        assert not (db.root / "scratch").exists()
        assert db.usage() == charge
        assert db.db.execute("SELECT count(*) FROM launches").fetchone()[0] == 1


def test_publication_rechecks_headroom_and_retains_previous_state(tmp_path, monkeypatch):
    with open_store(tmp_path / "db") as db:
        seed(db, tmp_path)
        db.begin("next", b"next")
        charge = db.usage()
        available(monkeypatch, 0)
        with pytest.raises(store.Refused, match="headroom"):
            db.commit("next", checkpoint(tmp_path / "next"), b"new")
        assert db._owner()["current_call"] == "saved"
        assert db.usage() == charge
        assert db.begin("saved", b"saved") == b"retained"
        assert db.db.execute("SELECT count(*) FROM reservations").fetchone()[0] == 1


def test_free_space_failure_refuses_before_admission(tmp_path, monkeypatch):
    with open_store(tmp_path / "db") as db:

        def fail(_):
            raise OSError("unavailable")

        monkeypatch.setattr(physical.shutil, "disk_usage", fail)
        with pytest.raises(store.Refused, match="observation unavailable"):
            db.begin("a", b"a")
        assert db.db.execute("SELECT count(*) FROM calls").fetchone()[0] == 0


def test_real_sqlite_full_preserves_checkpoint_retry_and_reservation_after_restart(tmp_path):
    root = tmp_path / "db"
    with open_store(root) as db:
        seed(db, tmp_path)
        db.begin("next", b"next")
        charge = db.usage()
        with pytest.raises(sqlite3.OperationalError) as failed:
            db.commit("next", checkpoint(tmp_path / "next", os.urandom(600_000)), b"new")
        assert failed.value.sqlite_errorcode == sqlite3.SQLITE_FULL
        assert not db.db.in_transaction
        assert db.usage() == charge
        assert db._owner()["current_call"] == "saved"
        assert db.begin("saved", b"saved") == b"retained"
        assert (root / "shared.sqlite").stat().st_size <= POLICY.database_bytes
        assert db.db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        db.audit_usage()
    with open_store(root) as db:
        assert db.restore(tmp_path / "restored") == "saved"
        assert (tmp_path / "restored/state").read_bytes() == b"old"
        assert db.begin("saved", b"saved") == b"retained"
        assert db.db.execute("SELECT count(*) FROM reservations").fetchone()[0] == 1
        assert (
            db.db.execute("SELECT status FROM calls WHERE id='next'").fetchone()[0] == "interrupted"
        )


def test_injected_disk_full_after_capture_rolls_back(tmp_path):
    with open_store(tmp_path / "db") as db:
        seed(db, tmp_path)
        db.begin("next", b"next")

        def fail(boundary):
            if boundary == "checkpoint_stored":
                raise OSError(errno.ENOSPC, "injected disk full")

        with pytest.raises(OSError) as failed:
            db.commit("next", checkpoint(tmp_path / "next", b"new"), b"new", fail)
        assert failed.value.errno == errno.ENOSPC
        assert db._owner()["current_call"] == "saved"
        assert db.begin("saved", b"saved") == b"retained"
        db.audit_usage()


def test_new_session_is_growth_but_reopening_for_retry_is_not(tmp_path, monkeypatch):
    root = tmp_path / "db"
    with open_store(root) as db:
        seed(db, tmp_path)
    available(monkeypatch, 0)
    with pytest.raises(store.Refused, match="headroom"):
        open_store(root, "new")
    with open_store(root) as db:
        assert db.begin("saved", b"saved") == b"retained"
        assert db.db.execute("SELECT count(*) FROM sessions").fetchone()[0] == 1


def test_low_space_collection_preserves_result_and_allows_page_reuse(tmp_path, monkeypatch):
    root = tmp_path / "db"
    with open_store(root) as db:
        seed(db, tmp_path)
        db.retire()
        allocated = (root / "shared.sqlite").stat().st_size
        available(monkeypatch, 0)
        assert db.collect_checkpoints(limit=10) == 1
        assert db.begin("saved", b"saved") == b"retained"
        assert db.db.execute("SELECT count(*) FROM chunks").fetchone()[0] == 0
        assert (root / "shared.sqlite").stat().st_size == allocated
        db.audit_usage()


def test_new_process_reapplies_ceiling_and_replays_saved_result(tmp_path):
    root = tmp_path / "db"
    with open_store(root) as db:
        seed(db, tmp_path)
    program = """
import sys
from pathlib import Path
from scripts.experiments.mxc_session_patch.shared_store import SharedStore, Limits
from scripts.experiments.mxc_session_patch.physical import Policy
with SharedStore(Path(sys.argv[1]), 'one', {'runtime':'pinned','policy':'closed'},
                 Limits(20000000,40000000,checkpoint_bytes=1000000,result_bytes=128,files=2),
                 physical_policy=Policy(262144,131072,65536)) as db:
    assert db.db.execute('PRAGMA max_page_count').fetchone()[0] * db.db.execute('PRAGMA page_size').fetchone()[0] == 262144
    assert db.begin('saved', b'saved') == b'retained'
    print('reopened and bounded')
"""
    result = subprocess.run(
        [sys.executable, "-c", program, str(root)],
        cwd=Path(__file__).parents[1],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "reopened and bounded"


@pytest.mark.parametrize(
    "mutation",
    [
        "DROP TABLE physical_settings",
        "DELETE FROM physical_settings",
        "UPDATE physical_settings SET transaction_bytes=1",
    ],
)
def test_corrupt_or_changed_policy_refuses_on_reopen(tmp_path, mutation):
    root = tmp_path / "db"
    with open_store(root):
        pass
    with sqlite3.connect(root / "shared.sqlite") as raw:
        raw.execute(mutation)
    with pytest.raises(store.Refused, match="physical"):
        open_store(root)
