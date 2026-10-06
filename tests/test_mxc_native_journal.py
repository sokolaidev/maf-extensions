"""Real-process controls for journaled launch and conservative scratch reclamation."""

from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))
try:
    store = importlib.import_module("scripts.experiments.mxc_session_patch.shared_store")
    native = importlib.import_module("scripts.experiments.mxc_session_patch.native_journal")
    identity = importlib.import_module("scripts.experiments.mxc_session_patch.process_identity")
    host = importlib.import_module("scripts.experiments.mxc_session_patch.host_call")
    shared = importlib.import_module("scripts.experiments.mxc_session_patch.shared_call")
finally:
    sys.path.remove(str(Path(__file__).parents[1]))
PROFILE = {"runtime": "pinned", "machine": "local", "policy": "closed"}
LIMITS = store.Limits(200_000, 500_000, checkpoint_bytes=1024, result_bytes=1024, files=2)
SCRATCH = store.ScratchLimits(40_000, 20, 80_000)


@pytest.fixture
def child():
    process = subprocess.Popen(
        [sys.executable, "-c", "import sys; sys.stdin.buffer.read()"], stdin=subprocess.PIPE
    )
    try:
        yield process
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=10)
        assert process.stdin is not None
        process.stdin.close()


def launch(db, child):
    assert db.begin("a", b"code", scratch=SCRATCH) is None
    journal = native.NativeJournal(db)
    work = journal.prepare("a")
    (work / "request").write_bytes(b"code")
    journal.arm("a", child)
    return journal, work


def scratch_usage(db):
    return db.db.execute("SELECT coalesce(sum(charge),0) FROM launches").fetchone()[0]


def test_live_helper_blocks_release_and_publication_then_recovery_preserves_identity(
    tmp_path, child
):
    root = tmp_path / "db"
    with store.SharedStore(root, "one", PROFILE, LIMITS) as db:
        journal, work = launch(db, child)
        charged = db.usage()
        with pytest.raises(store.Refused, match="still alive"):
            journal.reclaim("a")
        candidate = work / "candidate"
        candidate.mkdir()
        (candidate / "index.json").write_bytes(b"snapshot")
        with pytest.raises(store.Refused, match="termination"):
            db.commit("a", candidate, b"result")
        assert db.usage() == charged
    with store.SharedStore(root, "one", PROFILE, LIMITS) as db:
        journal = native.NativeJournal(db)
        with pytest.raises(store.Refused, match="still alive"):
            journal.reclaim("a")
        child.kill()
        child.wait(timeout=10)
        journal.reclaim("a")
        assert not work.exists()
        assert scratch_usage(db) == 0
        assert db.usage() == charged - LIMITS.reservation
        with pytest.raises(store.Refused, match="recovery"):
            db.begin("a", b"code", scratch=SCRATCH)
        assert db.begin("b", b"new", scratch=SCRATCH) is None


def test_publication_keeps_scratch_charged_and_retirement_keeps_result(tmp_path, child):
    with store.SharedStore(tmp_path / "db", "one", PROFILE, LIMITS) as db:
        journal, work = launch(db, child)
        child.kill()
        child.wait(timeout=10)
        candidate = work / "candidate"
        candidate.mkdir()
        (candidate / "index.json").write_bytes(b"snapshot")
        db.commit("a", candidate, b"result")
        assert scratch_usage(db) == SCRATCH.bytes
        assert db.begin("a", b"code", scratch=SCRATCH) == b"result"
        with pytest.raises(store.Refused, match="reservation"):
            db.begin("b", b"new")
        with pytest.raises(store.Refused, match="reservation"):
            db.collect_checkpoints()
        db.retire()
        journal.reclaim("a")
        assert db.collect_checkpoints() == 1
        assert db.begin("a", b"code") == b"result"
        assert scratch_usage(db) == 0


def test_scratch_admission_is_atomic_and_shared(tmp_path):
    budget = replace(SCRATCH, store_bytes=SCRATCH.bytes)
    root = tmp_path / "db"
    with store.SharedStore(root, "one", PROFILE, LIMITS) as one:
        with store.SharedStore(root, "two", PROFILE, LIMITS) as two:
            one.begin("a", b"code", scratch=budget)
            before = two.usage()
            for denied in (budget, replace(budget, store_bytes=budget.store_bytes + 1)):
                with pytest.raises(store.Refused, match="scratch quota"):
                    two.begin("a", b"code", scratch=denied)
                assert two.usage() == before
                assert two.db.execute("SELECT 1 FROM calls WHERE session='two'").fetchone() is None
            assert not (root / "scratch").exists()


@pytest.mark.parametrize("value", [0, -1, True, 1.5, 2**63])
def test_scratch_limits_refuse_invalid_values(value):
    with pytest.raises(store.Refused):
        store.ScratchLimits(value, 20, 80_000)
    with pytest.raises(store.Refused):
        store.ScratchLimits(100, value, 80_000)
    with pytest.raises(store.Refused):
        store.ScratchLimits(100, 20, value)


def test_unknown_scratch_blocks_admission_without_charging(tmp_path):
    with store.SharedStore(tmp_path / "db", "one", PROFILE, LIMITS) as db:
        unknown = db.root / "scratch" / "unowned"
        unknown.mkdir(parents=True)
        before = db.usage()
        with pytest.raises(store.Refused, match="unknown"):
            db.begin("a", b"code", scratch=SCRATCH)
        assert db.usage() == before
        assert scratch_usage(db) == 0
        assert unknown.exists()


@pytest.mark.parametrize("damage", ["hardlink", "oversize", "entries", "unknown"])
def test_unsafe_scratch_preserves_all_files_and_charges(tmp_path, child, damage):
    with store.SharedStore(tmp_path / "db", "one", PROFILE, LIMITS) as db:
        journal, work = launch(db, child)
        child.kill()
        child.wait(timeout=10)
        if damage == "hardlink":
            outside = tmp_path / "outside"
            outside.write_bytes(b"keep")
            os.link(outside, work / "linked")
        elif damage == "oversize":
            (work / "large").write_bytes(b"x" * SCRATCH.bytes)
        elif damage == "entries":
            for index in range(SCRATCH.entries):
                (work / str(index)).touch()
        else:
            (work.parent / "unowned").mkdir()
        before = sorted(str(path) for path in work.rglob("*"))
        with pytest.raises(store.Refused):
            journal.reclaim("a")
        assert sorted(str(path) for path in work.rglob("*")) == before
        assert scratch_usage(db) == SCRATCH.bytes
        assert db.db.execute("SELECT state FROM launches").fetchone()[0] == "armed"


def test_missing_identity_and_stale_generation_never_authorize_cleanup(tmp_path, child):
    with store.SharedStore(tmp_path / "db", "one", PROFILE, LIMITS) as db:
        db.begin("a", b"code", scratch=SCRATCH)
        journal = native.NativeJournal(db)
        work = journal.prepare("a")
        with pytest.raises(store.Refused, match="unidentified"):
            journal.reclaim("a")
        db.db.execute("UPDATE sessions SET generation=generation+1")
        with pytest.raises(store.Refused, match="generation"):
            journal.arm("a", child)
        with pytest.raises(store.Refused, match="generation"):
            journal.reclaim("a")
        assert work.exists()
        assert scratch_usage(db) == SCRATCH.bytes


def test_creation_identity_and_permission_failure(tmp_path, child, monkeypatch):
    captured = identity.capture(child.pid)
    assert not identity.stopped(captured)
    with pytest.raises(store.Refused, match="different machine"):
        identity.stopped(replace(captured, machine="different"))
    with pytest.raises(store.Refused, match="boot"):
        identity.stopped(replace(captured, boot="unknown"))
    # A reused PID cannot refer to the original OS creation identity.
    assert identity.stopped(replace(captured, created="different"))
    with store.SharedStore(tmp_path / "db", "one", PROFILE, LIMITS) as db:
        journal, work = launch(db, child)

        def denied(_pid):
            raise PermissionError("unavailable")

        name = "_windows_process" if os.name == "nt" else "_linux_process"
        monkeypatch.setattr(identity, name, denied)
        with pytest.raises(store.Refused, match="evidence is unavailable"):
            journal.reclaim("a")
        assert work.exists()
        assert scratch_usage(db) == SCRATCH.bytes


@pytest.mark.parametrize(
    "boundary",
    [
        "before_cleanup_intent",
        "after_cleanup_intent",
        "after_scratch_removal",
        "before_cleanup_release",
        "after_cleanup_release",
    ],
)
def test_process_death_during_cleanup_is_recoverable(tmp_path, child, boundary):
    root = tmp_path / "db"
    with store.SharedStore(root, "one", PROFILE, LIMITS) as db:
        _, work = launch(db, child)
    child.kill()
    child.wait(timeout=10)
    source = f"""
import os
from pathlib import Path
from scripts.experiments.mxc_session_patch.shared_store import SharedStore, Limits
from scripts.experiments.mxc_session_patch.native_journal import NativeJournal
with SharedStore(Path({str(root)!r}), 'one', {PROFILE!r}, {LIMITS!r}) as db:
    NativeJournal(db).reclaim('a', lambda point: os._exit(73) if point == {boundary!r} else None)
"""
    result = subprocess.run([sys.executable, "-c", source], capture_output=True, timeout=20)
    assert result.returncode == 73, result.stderr.decode()
    with store.SharedStore(root, "one", PROFILE, LIMITS) as db:
        if boundary != "after_cleanup_release":
            assert scratch_usage(db) == SCRATCH.bytes
            native.NativeJournal(db).reclaim("a")
        assert scratch_usage(db) == 0
        assert not work.exists()
        with pytest.raises(store.Refused, match="recovery"):
            db.begin("a", b"code")
        assert db.begin("b", b"new", scratch=SCRATCH) is None


@pytest.mark.parametrize("boundary", ["before_launch_commit", "after_launch_commit"])
def test_process_death_around_identity_commit_never_sends_header(tmp_path, boundary):
    root = tmp_path / "db"
    source = f"""
import os, subprocess, sys
from pathlib import Path
from scripts.experiments.mxc_session_patch.shared_store import SharedStore, Limits, ScratchLimits
from scripts.experiments.mxc_session_patch.native_journal import NativeJournal
with SharedStore(Path({str(root)!r}), 'one', {PROFILE!r}, {LIMITS!r}) as db:
    db.begin('a', b'code', scratch={SCRATCH!r})
    journal = NativeJournal(db)
    work = journal.prepare('a')
    child = subprocess.Popen([sys.executable, '-c', 'import sys; sys.stdin.buffer.read(8)'], stdin=subprocess.PIPE)
    journal.arm('a', child, lambda point: os._exit(73) if point == {boundary!r} else None)
    raise AssertionError('crash boundary was skipped')
"""
    result = subprocess.run([sys.executable, "-c", source], capture_output=True, timeout=20)
    assert result.returncode == 73, result.stderr.decode()
    with store.SharedStore(root, "one", PROFILE, LIMITS) as db:
        assert scratch_usage(db) == SCRATCH.bytes
        journal = native.NativeJournal(db)
        if boundary == "before_launch_commit":
            with pytest.raises(store.Refused, match="unidentified"):
                journal.reclaim("a")
        else:
            journal.reclaim("a")
            assert scratch_usage(db) == 0


def test_supervisor_persists_identity_before_header_and_replay_skips_helper(tmp_path, monkeypatch):
    real_popen = subprocess.Popen
    budget = replace(SCRATCH, bytes=4 * store.CHUNK, store_bytes=8 * store.CHUNK, entries=2048)
    launches = []
    fake_helper = r"""
import json, pathlib, sys
if sys.stdin.buffer.read(8) != b'MXCOWN1\n':
    sys.exit(74)
work = pathlib.Path.cwd()
(work / 'candidate').mkdir()
(work / 'candidate' / 'index.json').write_bytes(b'checkpoint')
(work / 'native.output').write_bytes(b'ok')
(work / 'native.json').write_text(json.dumps({'captured': True, 'output': {
    'limit_bytes': 100, 'retained_bytes': 2, 'omitted_bytes': 0,
    'omitted_bytes_saturated': False, 'truncated': False}}))
"""

    def spawn(*args, **kwargs):
        launches.append(args)
        return real_popen([sys.executable, "-c", fake_helper], **kwargs)

    monkeypatch.setattr(host.subprocess, "Popen", spawn)
    original_arm = native.NativeJournal.arm

    def arm(journal, call, child):
        original_arm(journal, call, child)
        row = journal._row(call)
        assert row["state"] == "armed"
        assert row["identity"] is not None
        assert not (journal.store.root / "scratch" / row["token"] / "candidate").exists()

    monkeypatch.setattr(native.NativeJournal, "arm", arm)
    with store.SharedStore(tmp_path / "db", "one", PROFILE, LIMITS) as db:
        result = shared.call(
            db, "a", b"code", tmp_path / "helper", tmp_path / "startup", budget, 100
        )
        assert (
            shared.call(db, "a", b"code", tmp_path / "missing", tmp_path / "missing", budget, 100)
            == result
        )
        assert len(launches) == 1
        command = launches[0][0]
        assert command[1] == "call-stored"
        assert command[-3:] == ["100", "1024", "2"]
        assert scratch_usage(db) == 0
        assert not list((db.root / "scratch").iterdir())


def test_corrupt_process_identity_preserves_reservation(tmp_path, child):
    with store.SharedStore(tmp_path / "db", "one", PROFILE, LIMITS) as db:
        journal, work = launch(db, child)
        encoded = db.db.execute("SELECT identity FROM launches").fetchone()[0]
        db.db.execute("UPDATE launches SET identity=?", (encoded.replace("sha256", "broken"),))
        with pytest.raises(store.Refused, match="invalid helper identity"):
            journal.reclaim("a")
        assert work.exists()
        assert scratch_usage(db) == SCRATCH.bytes


def test_failed_identity_persistence_stops_helper_without_header(tmp_path, monkeypatch):
    real_popen = subprocess.Popen
    children = []
    marker = tmp_path / "executed"
    program = f"import sys; from pathlib import Path; header=sys.stdin.buffer.read(8); Path({str(marker)!r}).touch() if header == b'MXCOWN1\\n' else None"

    def spawn(*args, **kwargs):
        child = real_popen([sys.executable, "-c", program], **kwargs)
        children.append(child)
        return child

    def refuse(_child):
        raise store.Refused("identity commit failed")

    monkeypatch.setattr(host.subprocess, "Popen", spawn)
    with pytest.raises(store.Refused, match="identity commit failed"):
        host.execute(tmp_path / "helper", tmp_path / "startup", tmp_path, b"code", 100, refuse)
    assert children[0].poll() is not None
    assert not marker.exists()


@pytest.mark.parametrize("supervisor", ["shared", "direct"])
def test_relative_helper_and_startup_are_bound_before_changing_child_directory(
    tmp_path, monkeypatch, supervisor
):
    real_popen = subprocess.Popen
    launches = []
    helper = tmp_path / "build" / "helper.py"
    helper.parent.mkdir()
    helper.write_text(
        "import json, sys\nfrom pathlib import Path\n"
        "assert sys.stdin.buffer.read(8) == b'MXCOWN1\\n'\n"
        "startup, candidate, code, report = map(Path, sys.argv[2:6])\n"
        "assert startup.joinpath('index.json').read_bytes() == b'checkpoint'\n"
        "assert code.read_bytes() == b'code'\n"
        "candidate.mkdir()\ncandidate.joinpath('index.json').write_bytes(b'checkpoint')\n"
        "report.with_suffix('.output').write_bytes(b'ok')\n"
        "report.write_text(json.dumps({'captured':True,'output':{'limit_bytes':100,'retained_bytes':2,'omitted_bytes':0,'omitted_bytes_saturated':False,'truncated':False}}))\n",
        encoding="utf-8",
    )
    startup = tmp_path / "startup"
    startup.mkdir()
    (startup / "index.json").write_bytes(b"checkpoint")

    def spawn(command, **kwargs):
        launches.append(command)
        return real_popen([sys.executable, *command], **kwargs)

    monkeypatch.setattr(host.subprocess, "Popen", spawn)
    monkeypatch.chdir(tmp_path)
    if supervisor == "direct":
        Path("work").mkdir()
        result = host.execute(Path("build/helper.py"), Path("startup"), Path("work"), b"code", 100)
        assert b"b2s=" in result
    else:
        budget = replace(SCRATCH, bytes=4 * store.CHUNK, store_bytes=8 * store.CHUNK, entries=2048)
        with store.SharedStore(Path("db"), "one", PROFILE, LIMITS) as db:
            result = shared.call(
                db, "a", b"code", Path("build/helper.py"), Path("startup"), budget, 100
            )
            assert (
                shared.call(db, "a", b"code", Path("missing"), Path("missing"), budget, 100)
                == result
            )
            assert len(launches) == 1
            assert (
                shared.call(db, "b", b"code", Path("build/helper.py"), Path("missing"), budget, 100)
                == result
            )
            assert len(launches) == 2
            assert scratch_usage(db) == 0
            assert not list((db.root / "scratch").iterdir())
    assert all(
        Path(command[0]).is_absolute() and Path(command[2]).is_absolute() for command in launches
    )


@pytest.mark.parametrize("output_limit", [1, 2, 3, 100])
def test_serialized_output_must_fit_before_new_admission(tmp_path, monkeypatch, output_limit):
    envelope = json.dumps(
        {
            "console_base64": "A" * (4 * ((output_limit + 2) // 3)),
            "output": {
                "limit_bytes": output_limit,
                "retained_bytes": output_limit,
                "omitted_bytes": 2**64 - 1,
                "omitted_bytes_saturated": False,
                "truncated": True,
            },
        },
        sort_keys=True,
    ).encode()
    limits = replace(LIMITS, result_bytes=len(envelope) - 1)
    budget = replace(SCRATCH, bytes=4 * store.CHUNK, store_bytes=8 * store.CHUNK, entries=2048)
    with store.SharedStore(tmp_path / "db", "one", PROFILE, limits) as db:

        def unexpected(*args, **kwargs):
            pytest.fail("helper must not execute for an unpublishable result bound")

        monkeypatch.setattr(shared, "execute", unexpected)
        with pytest.raises(store.Refused, match="serialized result"):
            shared.call(
                db, "a", b"code", tmp_path / "helper", tmp_path / "startup", budget, output_limit
            )
        assert db.db.execute("SELECT count(*) FROM calls").fetchone()[0] == 0
        assert db.db.execute("SELECT count(*) FROM reservations").fetchone()[0] == 0
        assert scratch_usage(db) == 0
        assert db.begin("a", b"code") is None
        checkpoint = tmp_path / "checkpoint"
        checkpoint.mkdir()
        (checkpoint / "index.json").write_bytes(b"checkpoint")
        db.commit("a", checkpoint, b"saved")
        assert (
            shared.call(
                db, "a", b"code", tmp_path / "missing", tmp_path / "missing", budget, output_limit
            )
            == b"saved"
        )


@pytest.mark.parametrize("output_limit", [1, 2, 3, 100])
def test_serialized_output_exact_bound_can_publish(tmp_path, monkeypatch, output_limit):
    envelope = json.dumps(
        {
            "console_base64": "A" * (4 * ((output_limit + 2) // 3)),
            "output": {
                "limit_bytes": output_limit,
                "retained_bytes": output_limit,
                "omitted_bytes": 2**64 - 1,
                "omitted_bytes_saturated": False,
                "truncated": True,
            },
        },
        sort_keys=True,
    ).encode()
    limits = replace(LIMITS, result_bytes=len(envelope))
    budget = replace(SCRATCH, bytes=4 * store.CHUNK, store_bytes=8 * store.CHUNK, entries=2048)
    with store.SharedStore(tmp_path / "db", "one", PROFILE, limits) as db:

        def finish(helper, base, work, *args, **kwargs):
            (work / "candidate").mkdir()
            (work / "candidate" / "index.json").write_bytes(b"checkpoint")
            db.db.execute("DELETE FROM launches")
            return envelope

        monkeypatch.setattr(shared, "execute", finish)
        monkeypatch.setattr(native.NativeJournal, "reclaim", lambda *args: None)
        assert (
            shared.call(
                db, "a", b"code", tmp_path / "helper", tmp_path / "startup", budget, output_limit
            )
            == envelope
        )
