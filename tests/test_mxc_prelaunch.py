"""Prove the durable boundary between unlaunched scratch and an unidentified process."""

from __future__ import annotations

import importlib
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))
try:
    files = importlib.import_module("scripts.experiments.mxc_files_patch.physical_probe")
    idle = importlib.import_module("scripts.experiments.mxc_session_patch.idle")
    physical = importlib.import_module("scripts.experiments.mxc_session_patch.physical")
    journal = importlib.import_module("scripts.experiments.mxc_session_patch.native_journal")
    identity = importlib.import_module("scripts.experiments.mxc_session_patch.process_identity")
    store = importlib.import_module("scripts.experiments.mxc_session_patch.shared_store")
finally:
    sys.path.remove(str(Path(__file__).parents[1]))


PRELAUNCH_VERSION = store.PRELAUNCH_VERSION
Limits, Refused, ScratchLimits, SharedStore = (
    store.Limits,
    store.Refused,
    store.ScratchLimits,
    store.SharedStore,
)
NativeJournal = journal.NativeJournal
Identity, stopped, terminate = identity.Identity, identity.stopped, identity.terminate
SEED = files.SEED

MIB = 1024**2
PROFILE = {"runtime": "fixture", "policy": "closed"}
POLICY = physical.Policy(8 * MIB, MIB, MIB)
LIMITS = Limits(200_000, 500_000, checkpoint_bytes=1024, result_bytes=1024, files=2)
SCRATCH = ScratchLimits(40_000, 20, 80_000)


def opened(root: Path, **kwargs):
    return SharedStore(root, "one", PROFILE, LIMITS, physical_policy=POLICY, **kwargs)


def prepared(db) -> Path:
    db.begin("a", b"code", scratch=SCRATCH)
    work = NativeJournal(db).prepare("a")
    (work / "partial").write_bytes(b"checkpoint prefix")
    return work


def run_worker(root: Path, source: str) -> subprocess.CompletedProcess:
    preamble = f"""
import os, subprocess, sys
from pathlib import Path
from scripts.experiments.mxc_session_patch.native_journal import NativeJournal
from scripts.experiments.mxc_session_patch.process_identity import capture
from scripts.experiments.mxc_session_patch.shared_store import SharedStore, Limits, ScratchLimits
from scripts.experiments.mxc_session_patch.physical import Policy
root = Path({str(root)!r})
db = SharedStore(root, 'one', {PROFILE!r}, Limits(200_000, 500_000, checkpoint_bytes=1024, result_bytes=1024, files=2), physical_policy=Policy(8*1024**2,1024**2,1024**2), prelaunch_recovery=True)
journal = NativeJournal(db)
"""
    return subprocess.run(
        [sys.executable, "-c", preamble + source],
        env={**os.environ, "PYTHONPATH": str(Path.cwd())},
        capture_output=True,
        timeout=30,
    )


def test_new_root_requires_explicit_policy_and_matching_format(tmp_path):
    with pytest.raises(Refused, match="physical policy"):
        SharedStore(tmp_path / "invalid", "one", PROFILE, LIMITS, prelaunch_recovery=True)
    legacy = tmp_path / "legacy"
    with opened(legacy) as db:
        prepared(db)
    with pytest.raises(Refused, match="matching new store"):
        opened(legacy, prelaunch_recovery=True)
    with opened(legacy) as db:
        with pytest.raises(Refused, match="unidentified"):
            NativeJournal(db).reclaim("a")
    root = tmp_path / "current"
    with opened(root, prelaunch_recovery=True) as db:
        assert db.db.execute("SELECT version FROM settings").fetchone()[0] == PRELAUNCH_VERSION
        generation = db.generation
    with pytest.raises(Refused, match="matching new store"):
        opened(root)
    with sqlite3.connect(root / "shared.sqlite") as db:
        assert db.execute("SELECT generation FROM sessions").fetchone()[0] == generation


@pytest.mark.parametrize("deleting", [False, True])
def test_recovery_requires_new_owner_and_respects_idle_and_retirement(tmp_path, deleting):
    root = tmp_path / "store"
    policy = idle.Policy(60)
    with opened(root, prelaunch_recovery=True, idle_policy=policy) as db:
        work = prepared(db)
        with pytest.raises(Refused, match="new owner"):
            NativeJournal(db).reclaim("a")
        assert work.exists()
        charged = db.usage()
    with opened(root, prelaunch_recovery=True, idle_policy=policy) as db:
        journal = NativeJournal(db)
        if deleting:
            journal.delete()
        else:
            journal.reclaim("a")
        assert not work.exists()
        assert db.usage() == charged - LIMITS.reservation
        assert db.db.execute("SELECT count(*) FROM launches").fetchone()[0] == 0
        assert db.db.execute("SELECT count(*) FROM reservations").fetchone()[0] == 0
        with pytest.raises(Refused, match="recovery"):
            db.begin("a", b"code", scratch=SCRATCH)
        if deleting:
            with pytest.raises(Refused, match="retired"):
                db.begin("new", b"new", scratch=SCRATCH)
        else:
            assert db.db.execute("SELECT deadline FROM session_idle").fetchone()[0] is not None
            assert db.begin("new", b"new", scratch=SCRATCH) is None


@pytest.mark.parametrize(
    "fault",
    [
        "before_spawn_intent",
        "after_spawn_intent",
        "after_spawn",
        "before_launch_commit",
        "after_launch_commit",
    ],
)
def test_process_death_never_confuses_unlaunched_and_unidentified(tmp_path, fault):
    root = tmp_path / "store"
    marker = tmp_path / "executed"
    identity_path = tmp_path / "child.json"
    child_source = "import sys; from pathlib import Path; header=sys.stdin.buffer.readline(); Path(sys.argv[1]).write_text('executed') if header==b'MXCOWN1\\n' else None"
    source = f"""
def boundary(point):
    if point == {fault!r}: os._exit(73)
db.begin('a', b'code', scratch=ScratchLimits(40_000,20,80_000))
work = journal.prepare('a')
(work/'partial').write_bytes(b'checkpoint prefix')
journal.start('a', boundary)
child = subprocess.Popen([sys.executable, '-c', {child_source!r}, {str(marker)!r}], stdin=subprocess.PIPE)
Path({str(identity_path)!r}).write_text(capture(child.pid).encode())
journal.arm('a', child, boundary)
raise AssertionError('crash boundary was not reached')
"""
    result = run_worker(root, source)
    try:
        assert result.returncode == 73, result.stderr.decode(errors="replace")
        if identity_path.exists():
            identity = Identity.decode(identity_path.read_text())
            deadline = time.monotonic() + 10
            while not stopped(identity) and time.monotonic() < deadline:
                time.sleep(0.02)
            assert stopped(identity)
        assert not marker.exists()
        with opened(root, prelaunch_recovery=True) as db:
            row = db.db.execute("SELECT * FROM launches").fetchone()
            work = db.root / "scratch" / row["token"]
            charged = db.usage()
            if fault in ("before_spawn_intent", "after_launch_commit"):
                NativeJournal(db).reclaim("a")
                assert not work.exists()
                assert db.begin("new", b"new", scratch=SCRATCH) is None
            else:
                assert row["state"] == "starting" and row["identity"] is None
                with pytest.raises(Refused, match="unidentified"):
                    NativeJournal(db).reclaim("a")
                assert (work / "partial").read_bytes() == b"checkpoint prefix"
                assert db.usage() == charged
                with pytest.raises(Refused, match="outstanding reservation"):
                    db.begin("new", b"new", scratch=SCRATCH)
    finally:
        if identity_path.exists():
            terminate(Identity.decode(identity_path.read_text()))


@pytest.mark.parametrize(
    "fault",
    [
        "before_cleanup_intent",
        "after_cleanup_intent",
        "after_scratch_removal",
        "before_cleanup_release",
        "after_cleanup_release",
    ],
)
def test_prelaunch_cleanup_resumes_after_process_death(tmp_path, fault):
    root = tmp_path / "store"
    with opened(root, prelaunch_recovery=True) as db:
        work = prepared(db)
        charged = db.usage()
    result = run_worker(
        root, f"journal.reclaim('a', lambda point: os._exit(73) if point == {fault!r} else None)"
    )
    assert result.returncode == 73, result.stderr.decode(errors="replace")
    with opened(root, prelaunch_recovery=True) as db:
        if fault != "after_cleanup_release":
            assert db.usage() == charged
            assert db.db.execute("SELECT count(*) FROM reservations").fetchone()[0] == 1
            NativeJournal(db).reclaim("a")
        assert not work.exists()
        assert db.usage() == charged - LIMITS.reservation
        assert db.db.execute("SELECT count(*) FROM launches").fetchone()[0] == 0
        assert db.begin("new", b"new", scratch=SCRATCH) is None


def test_unlaunched_cleanup_still_refuses_unaccounted_files(tmp_path):
    root = tmp_path / "store"
    with opened(root, prelaunch_recovery=True) as db:
        work = prepared(db)
        (work / "excess").write_bytes(b"x" * SCRATCH.bytes)
    with opened(root, prelaunch_recovery=True) as db:
        charged = db.usage()
        with pytest.raises(Refused, match="reserved bounds"):
            NativeJournal(db).reclaim("a")
        assert db.usage() == charged and work.exists()


@pytest.mark.parametrize("folder", ["mxc_session_patch", "mxc_streams_patch", "mxc_files_patch"])
@pytest.mark.parametrize("intent_fails", [False, True])
def test_each_supervisor_commits_intent_before_process_creation(
    tmp_path, monkeypatch, folder, intent_fails
):
    shared = importlib.import_module(f"scripts.experiments.{folder}.shared_call")
    transport_name = "host_call" if folder == "mxc_session_patch" else "transport"
    transport = importlib.import_module(f"scripts.experiments.{folder}.{transport_name}")
    limits = Limits(256 * MIB, 512 * MIB, checkpoint_bytes=1024, result_bytes=64 * MIB, files=2)
    scratch = ScratchLimits(100 * MIB, 4096, 200 * MIB)
    with SharedStore(
        tmp_path / "store", "one", PROFILE, limits, physical_policy=POLICY, prelaunch_recovery=True
    ) as db:
        spawned = []

        def popen(*args, **kwargs):
            assert db.db.execute("SELECT state FROM launches").fetchone()[0] == "starting"
            spawned.append(True)
            raise OSError("spawn fixture")

        monkeypatch.setattr(transport.subprocess, "Popen", popen)

        def boundary(point):
            if intent_fails and point == "before_spawn_intent":
                raise OSError("intent fixture")

        request = SEED if folder == "mxc_files_patch" else b"print(1)"
        args = (db, "a", request, tmp_path / "helper", tmp_path / "startup", scratch)
        if folder == "mxc_session_patch":
            args += (1024,)
        with pytest.raises(OSError, match="intent fixture" if intent_fails else "spawn fixture"):
            shared.call(*args, boundary=boundary)
        assert bool(spawned) is not intent_fails
        assert db.db.execute("SELECT state FROM launches").fetchone()[0] == (
            "prepared" if intent_fails else "starting"
        )


def test_identified_format7_helper_must_stop_before_reclamation(tmp_path):
    root = tmp_path / "store"
    with opened(root, prelaunch_recovery=True) as db:
        work = prepared(db)
        journal = NativeJournal(db)
        journal.start("a")
        child = subprocess.Popen(
            [sys.executable, "-c", "import sys; sys.stdin.buffer.read()"], stdin=subprocess.PIPE
        )
        try:
            journal.arm("a", child)
            with pytest.raises(Refused, match="still alive"):
                journal.reclaim("a")
        finally:
            child.kill()
            child.wait(timeout=10)
            assert child.stdin is not None
            child.stdin.close()
    with opened(root, prelaunch_recovery=True) as db:
        NativeJournal(db).reclaim("a")
        assert not work.exists()


def test_prepared_without_scratch_can_be_reconciled_after_restart(tmp_path):
    root = tmp_path / "store"
    with opened(root, prelaunch_recovery=True) as db:
        db.begin("a", b"code", scratch=SCRATCH)
    with opened(root, prelaunch_recovery=True) as db:
        NativeJournal(db).reclaim("a")
        assert db.begin("new", b"new", scratch=SCRATCH) is None


def test_altered_launch_schema_refused_before_owner_writes(tmp_path):
    root = tmp_path / "store"
    with opened(root, prelaunch_recovery=True) as db:
        generation = db.generation
        db.db.execute("ALTER TABLE launches ADD COLUMN extra TEXT")
    with pytest.raises(Refused, match="pre-launch schema"):
        opened(root, prelaunch_recovery=True)
    with sqlite3.connect(root / "shared.sqlite") as db:
        assert db.execute("SELECT generation FROM sessions").fetchone()[0] == generation
