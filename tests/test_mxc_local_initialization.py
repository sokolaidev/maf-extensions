"""Ordinary-process startup crashes for the experimental local MXC stores."""

from __future__ import annotations

import importlib
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT))
try:
    host = importlib.import_module("scripts.experiments.mxc_session_patch.host_store")
    shared = importlib.import_module("scripts.experiments.mxc_session_patch.shared_store")
finally:
    sys.path.remove(str(ROOT))

PROFILE = {"runtime": "pinned", "policy": "closed", "machine": "local"}
LIMITS = shared.Limits(200_000, 500_000, checkpoint_bytes=1024, result_bytes=128, files=2)
KINDS = ["legacy", "format4", "format5", "format6", "format7"]


def open_store(root, kind, profile=PROFILE):
    if kind == "legacy":
        return host.Store(root, profile)
    options = {}
    if kind == "format5":
        options["idle_policy"] = shared.idle.Policy(3600)
    if kind in ("format6", "format7"):
        options["physical_policy"] = shared.physical.Policy(8 * 1024**2, 1024**2, 0)
    if kind == "format7":
        options["prelaunch_recovery"] = True
    return shared.SharedStore(root, "one", profile, LIMITS, **options)


def database(root, kind):
    return root / ("state.sqlite" if kind == "legacy" else "shared.sqlite")


CRASH = """
import importlib.util, os, sqlite3, sys
from pathlib import Path
spec = importlib.util.spec_from_file_location('initialization_controls', sys.argv[1])
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
root, kind, point = Path(sys.argv[2]), sys.argv[3], sys.argv[4]
original = sqlite3.connect
def stop():
    if len(sys.argv) > 5:
        Path(sys.argv[5]).touch()
        sys.stdin.readline()
    else:
        os._exit(76)
class Connection(sqlite3.Connection):
    tables = 0
    commits = 0
    def trace(self, statement):
        sql = statement.strip().upper()
        if sql.startswith('CREATE TABLE'):
            if (point == 'before_schema' and self.tables == 0
                or point == 'after_first_table' and self.tables == 1):
                os._exit(76)
            self.tables += 1
        if point == 'before_metadata' and sql.startswith(('INSERT INTO META ', 'INSERT INTO SESSIONS ')):
            os._exit(76)
    def execute(self, statement, *args, **kwargs):
        result = super().execute(statement, *args, **kwargs)
        if point == 'after_metadata' and statement.strip().upper().startswith(('INSERT INTO META ', 'INSERT INTO SESSIONS ')):
            stop()
        return result
    def commit(self):
        self.commits += 1
        target = 2 if point.endswith('_owner_commit') else 1
        if self.commits == target and point in ('before_commit', 'before_owner_commit'):
            os._exit(76)
        super().commit()
        if self.commits == target and point in ('after_commit', 'after_owner_commit'):
            os._exit(76)
    def __exit__(self, *args):
        if args[0] is None:
            self.commit()
        else:
            self.rollback()
def connect(*args, **kwargs):
    db = original(*args, **kwargs, factory=Connection)
    if point == 'after_connect':
        os._exit(76)
    db.set_trace_callback(db.trace)
    return db
sqlite3.connect = connect
m.open_store(root, kind)
if len(sys.argv) == 5:
    raise AssertionError('initialization did not reach crash boundary')
"""


def crash(root, kind, point):
    result = subprocess.run(
        [sys.executable, "-c", CRASH, str(Path(__file__)), str(root), kind, point],
        cwd=ROOT,
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == 76, result.stderr.decode()
    assert database(root, kind).exists()


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize(
    "point",
    [
        "after_connect",
        "before_schema",
        "after_first_table",
        "before_metadata",
        "after_metadata",
        "before_commit",
        "after_commit",
    ],
)
def test_initialization_process_death_recovers_atomically(tmp_path, kind, point):
    root = tmp_path / "db"
    crash(root, kind, point)
    with sqlite3.connect(database(root, kind)) as connection:
        has_schema = (
            connection.execute("SELECT 1 FROM sqlite_schema LIMIT 1").fetchone() is not None
        )
        assert has_schema == (point == "after_commit")
    if point == "after_commit":
        with pytest.raises(host.Refused, match="profile"):
            open_store(root, kind, PROFILE | {"policy": "changed"})
    with open_store(root, kind) as db:
        assert db.restore(tmp_path / "empty") is None
        checkpoint = tmp_path / "candidate"
        checkpoint.mkdir()
        (checkpoint / "index.json").write_bytes(b"checkpoint")
        assert db.begin("a", b"code") is None
        db.commit("a", checkpoint, b"result")
    with open_store(root, kind) as db:
        assert db.generation == 2 + (kind == "legacy" and point == "after_commit")
        assert db.begin("a", b"code") == b"result"
        assert db.restore(tmp_path / "restored") == "a"
        assert (tmp_path / "restored/index.json").read_bytes() == b"checkpoint"
        if kind != "legacy":
            assert db.db.execute("SELECT version FROM settings").fetchone()[0] == int(kind[-1])
            shared.accounting.audit(db.db)


@pytest.mark.parametrize("kind", KINDS[1:])
@pytest.mark.parametrize("point", ["before_owner_commit", "after_owner_commit"])
def test_shared_owner_initialization_crash_retains_first_commit(tmp_path, kind, point):
    root = tmp_path / "db"
    crash(root, kind, point)
    with pytest.raises(host.Refused, match="profile"):
        open_store(root, kind, PROFILE | {"runtime": "changed"})
    with open_store(root, kind) as db:
        assert db.generation == (2 if point == "after_owner_commit" else 1)
        assert db.db.execute("SELECT version FROM settings").fetchone()[0] == int(kind[-1])
        shared.accounting.audit(db.db)


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("empty", ["file", "sqlite"])
def test_existing_empty_database_initializes(tmp_path, kind, empty):
    root = tmp_path / "db"
    shared.prepare(root)
    path = database(root, kind)
    if empty == "file":
        path.touch()
    else:
        with sqlite3.connect(path) as connection:
            connection.execute("VACUUM")
        assert path.stat().st_size > 0
    with open_store(root, kind) as db:
        assert db.generation == 1
        assert db.restore(tmp_path / "empty") is None


@pytest.mark.parametrize("kind", ["legacy", "format4"])
def test_foreign_schema_is_refused_without_changes(tmp_path, kind):
    root = tmp_path / "db"
    shared.prepare(root)
    path = database(root, kind)
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE foreign_data(value TEXT)")
        connection.execute("INSERT INTO foreign_data VALUES('preserved')")
    before = path.read_bytes()
    with pytest.raises(host.Refused):
        open_store(root, kind)
    assert path.read_bytes() == before


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("damage", ["missing_metadata", "missing_table", "altered_table"])
def test_initialized_store_damage_is_not_repaired(tmp_path, kind, damage):
    root = tmp_path / "db"
    with open_store(root, kind):
        pass
    path = database(root, kind)
    with sqlite3.connect(path) as connection:
        if damage == "missing_metadata":
            connection.execute("DELETE FROM " + ("meta" if kind == "legacy" else "settings"))
        elif damage == "missing_table":
            connection.execute("DROP TABLE chunks")
        else:
            connection.execute("ALTER TABLE chunks ADD COLUMN unexpected TEXT")
    before = path.read_bytes()
    with pytest.raises(host.Refused):
        open_store(root, kind)
    assert path.read_bytes() == before


@pytest.mark.parametrize("kind", KINDS)
def test_missing_committed_session_preserves_checkpoint_and_result_rows(tmp_path, kind):
    root = tmp_path / "db"
    with open_store(root, kind) as db:
        checkpoint = tmp_path / "candidate"
        checkpoint.mkdir()
        (checkpoint / "index.json").write_bytes(b"checkpoint")
        db.begin("a", b"code")
        db.commit("a", checkpoint, b"result")
    path = database(root, kind)
    with sqlite3.connect(path) as connection:
        connection.execute("DELETE FROM " + ("meta" if kind == "legacy" else "sessions"))
    before = path.read_bytes()
    with pytest.raises(host.Refused):
        open_store(root, kind)
    assert path.read_bytes() == before


def test_empty_shared_database_cannot_bypass_legacy_migration(tmp_path):
    root = tmp_path / "db"
    shared.prepare(root)
    with open_store(root, "legacy"):
        pass
    before = (root / "state.sqlite").read_bytes()
    (root / "shared.sqlite").touch()
    with pytest.raises(host.Refused, match="migration"):
        open_store(root, "format4")
    assert (root / "state.sqlite").read_bytes() == before
    assert (root / "shared.sqlite").read_bytes() == b""


@pytest.mark.parametrize("kind", ["legacy", "format4"])
def test_nonempty_partial_schema_remains_refused(tmp_path, kind):
    root = tmp_path / "db"
    shared.prepare(root)
    path = database(root, kind)
    with sqlite3.connect(path) as connection:
        if kind == "legacy":
            connection.execute("CREATE TABLE meta (id INTEGER PRIMARY KEY, profile TEXT)")
        else:
            connection.execute("CREATE TABLE settings(id INTEGER PRIMARY KEY, version INTEGER)")
            connection.execute("INSERT INTO settings VALUES(1,4)")
    before = path.read_bytes()
    with pytest.raises(host.Refused):
        open_store(root, kind)
    assert path.read_bytes() == before


def test_legacy_multiline_schema_reopens_without_migration(tmp_path):
    import json

    root = tmp_path / "db"
    root.mkdir()
    with sqlite3.connect(root / "state.sqlite") as connection:
        connection.executescript("""
            CREATE TABLE IF NOT EXISTS meta (
                id INTEGER PRIMARY KEY CHECK(id=1), profile TEXT NOT NULL,
                generation INTEGER NOT NULL, current_call TEXT);
            CREATE TABLE IF NOT EXISTS calls (
                id TEXT PRIMARY KEY, request TEXT NOT NULL, status TEXT NOT NULL,
                generation INTEGER NOT NULL, result BLOB, result_hash TEXT);
            CREATE TABLE IF NOT EXISTS chunks (hash TEXT PRIMARY KEY, data BLOB NOT NULL);
            CREATE TABLE IF NOT EXISTS files (
                call_id TEXT NOT NULL REFERENCES calls(id), path TEXT NOT NULL,
                size INTEGER NOT NULL, hash TEXT NOT NULL, chunks TEXT NOT NULL,
                PRIMARY KEY(call_id, path));
        """)
        connection.execute(
            "INSERT INTO meta VALUES(1,?,9,NULL)",
            (json.dumps(PROFILE, sort_keys=True, separators=(",", ":")),),
        )
    with open_store(root, "legacy") as db:
        assert db.generation == 10


@pytest.mark.parametrize("kind", ["legacy", "format4"])
def test_initializer_lock_excludes_competing_process(tmp_path, kind):
    import time

    root = tmp_path / "db"
    ready = tmp_path / "ready"
    with subprocess.Popen(
        [
            sys.executable,
            "-c",
            CRASH,
            str(Path(__file__)),
            str(root),
            kind,
            "after_metadata",
            str(ready),
        ],
        cwd=ROOT,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ) as child:
        try:
            deadline = time.monotonic() + 20
            while not ready.exists() and child.poll() is None and time.monotonic() < deadline:
                time.sleep(0.01)
            assert ready.exists(), "initializer did not reach metadata boundary"
            with pytest.raises((host.Refused, OSError)):
                open_store(root, kind)
            _, stderr = child.communicate(b"continue\n", timeout=20)
            assert child.returncode == 0, stderr.decode()
        finally:
            if child.poll() is None:
                child.kill()
                child.communicate()
    with open_store(root, kind) as db:
        assert db.generation == 2
