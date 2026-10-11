"""Durable remote ownership controls; helpers are ordinary processes, not Hyperlight."""

from __future__ import annotations

import importlib
import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))
try:
    remote = importlib.import_module("scripts.experiments.mxc_session_patch.remote_authority")
finally:
    sys.path.remove(str(Path(__file__).parents[1]))

PROFILE = "a" * 64
A = remote.Resource("host-a", "boot-a", "guest-a")
B = remote.Resource("host-b", "boot-b", "guest-b")
C = remote.Resource("host-a", "boot-a", "guest-c")
WHOLE = remote.Policy(automatic_shutdown=True, machine_shutdown_hosts=frozenset({"host-a"}))


def authority(root, **kwargs):
    return remote.Authority(root, enabled=True, **kwargs)


def current(db, owner):
    with db.guard(owner):
        pass


def stopped(db, owner, request="stop"):
    db.request_fence(owner, request)
    db.confirm_fence(request, lambda _: True)


def test_default_does_not_create_coordinator(tmp_path):
    root = tmp_path / "remote"
    with pytest.raises(remote.Refused, match="opt-in"):
        remote.Authority(root)
    assert not root.exists()


def test_acquire_ack_loss_restart_and_immutable_profile(tmp_path):
    with authority(tmp_path / "db") as db:
        owner = db.acquire("session", PROFILE, A)
    with authority(tmp_path / "db") as db:
        assert db.acquire("session", PROFILE, A) == owner
        current(db, owner)
        with pytest.raises(remote.Refused, match="compatibility"):
            db.acquire("session", "b" * 64, A)
        with pytest.raises(remote.Refused, match="already has an owner"):
            db.acquire("session", PROFILE, B)
        with pytest.raises(remote.Refused, match="already used"):
            db.acquire("another", PROFILE, A)


@pytest.mark.parametrize("automatic", [False, True])
def test_machine_scope_requires_explicit_authority(tmp_path, automatic):
    with authority(tmp_path / "db", policy=remote.Policy(automatic_shutdown=True)) as db:
        owner = db.acquire("session", PROFILE, A)
        with pytest.raises(remote.Refused, match="whole-machine"):
            db.request_fence(owner, "stop", scope="machine", automatic=automatic)
        current(db, owner)
        assert db.db.execute("SELECT COUNT(*) FROM fences").fetchone()[0] == 0


def test_automatic_shutdown_is_separate_opt_in(tmp_path):
    with authority(tmp_path / "db") as db:
        owner = db.acquire("session", PROFILE, A)
        with pytest.raises(remote.Refused, match="automatic shutdown is disabled"):
            db.request_fence(owner, "stop", automatic=True)
        current(db, owner)
        fence = db.request_fence(owner, "manual")
        assert not fence.automatic


def test_guest_fence_revokes_only_its_owner_and_waits_for_confirmation(tmp_path):
    with authority(tmp_path / "db") as db:
        owner = db.acquire("session", PROFILE, A)
        other = db.acquire("other", PROFILE, C)
        request = db.request_fence(owner, "stop")
        assert request.sessions == ("session",)
        with pytest.raises(remote.Refused, match="revoked"):
            current(db, owner)
        current(db, other)
        with pytest.raises(remote.Refused, match="not confirmed"):
            db.acquire("session", PROFILE, B)
        with pytest.raises(remote.Refused, match="not confirmed"):
            db.confirm_fence("stop", lambda _: False)
    with authority(tmp_path / "db") as db:
        with pytest.raises(remote.Refused, match="not confirmed"):
            db.acquire("session", PROFILE, B)
        seen = []

        def verify(fence):
            seen.append(fence)
            return True

        db.confirm_fence("stop", verify)
        assert seen == [request]
        replacement = db.acquire("session", PROFILE, B)
        assert replacement.generation > owner.generation
        current(db, replacement)
        with pytest.raises(remote.Refused, match="revoked"):
            current(db, owner)
        with pytest.raises(remote.Refused, match="fenced"):
            db.acquire("new-session", PROFILE, A)


def test_shared_machine_fence_coordinates_all_affected_sessions(tmp_path):
    with authority(tmp_path / "db", policy=WHOLE) as db:
        one = db.acquire("one", PROFILE, A)
        two = db.acquire("two", PROFILE, C)
        unrelated = db.acquire("unrelated", PROFILE, B)
        different_boot = db.acquire("different", PROFILE, replace(A, boot="boot-new"))
        request = db.request_fence(one, "stop-host", scope="machine", automatic=True)
        assert request.sessions == ("one", "two")
        for owner in (one, two):
            with pytest.raises(remote.Refused, match="revoked"):
                current(db, owner)
        for owner in (unrelated, different_boot):
            current(db, owner)
        with pytest.raises(remote.Refused, match="fenced"):
            db.acquire("escape", PROFILE, replace(A, guest="new-guest"))
        db.confirm_fence(request.id, lambda _: True)
        for session, guest in [("one", "replacement-one"), ("two", "replacement-two")]:
            new = db.acquire(session, PROFILE, replace(B, guest=guest))
            current(db, new)
        with pytest.raises(remote.Refused, match="fenced"):
            db.acquire("escape", PROFILE, replace(A, guest="another-guest"))


def test_overlapping_machine_fence_refuses_without_revoking_other_session(tmp_path):
    with authority(tmp_path / "db", policy=WHOLE) as db:
        one = db.acquire("one", PROFILE, A)
        two = db.acquire("two", PROFILE, C)
        db.request_fence(one, "guest")
        with pytest.raises(remote.Refused, match="overlapping"):
            db.request_fence(two, "machine", scope="machine")
        current(db, two)
        assert db.db.execute("SELECT COUNT(*) FROM fences").fetchone()[0] == 1


def test_fence_retries_and_old_confirmations_do_not_restore_old_authority(tmp_path):
    with authority(tmp_path / "db") as db:
        one = db.acquire("session", PROFILE, A)
        request = db.request_fence(one, "first")
        assert db.request_fence(one, "first") == request
        with pytest.raises(remote.Refused, match="identity differs"):
            db.request_fence(one, "first", scope="machine")
        db.confirm_fence("first", lambda _: True)
        two = db.acquire("session", PROFILE, B)
        db.request_fence(two, "second")
        db.confirm_fence("first", lambda _: pytest.fail("already confirmed"))
        with pytest.raises(remote.Refused, match="not confirmed"):
            db.acquire("session", PROFILE, C)
        with pytest.raises(remote.Refused, match="identity differs"):
            db.request_fence(two, "first")


def test_verifier_failure_and_metadata_change_leave_takeover_pending(tmp_path):
    with authority(tmp_path / "db") as db:
        owner = db.acquire("session", PROFILE, A)
        db.request_fence(owner, "stop")

        def unavailable(_):
            raise OSError("provider unavailable")

        with pytest.raises(OSError, match="provider unavailable"):
            db.confirm_fence("stop", unavailable)
        with pytest.raises(remote.Refused, match="not confirmed"):
            db.acquire("session", PROFILE, B)

        def changed(_):
            db.db.execute("UPDATE sessions SET fence='wrong' WHERE id='session'")
            return True

        with pytest.raises(remote.Refused, match="membership changed"):
            db.confirm_fence("stop", changed)


def test_verification_is_outside_transaction_and_request_changes_refuse(tmp_path):
    root = tmp_path / "db"
    with authority(root) as db, authority(root) as other:
        owner = db.acquire("session", PROFILE, A)
        db.request_fence(owner, "stop")

        def verify(_):
            other.acquire("other", PROFILE, B)
            row = other.db.execute("SELECT request FROM fences WHERE id='stop'").fetchone()
            data = json.loads(row[0])
            data["record"]["resource"]["boot"] = "different-boot"
            other.db.execute("UPDATE fences SET request=? WHERE id='stop'", (json.dumps(data),))
            return True

        with pytest.raises(remote.Refused, match="metadata digest"):
            db.confirm_fence("stop", verify)
        with pytest.raises(remote.Refused, match="not confirmed"):
            db.acquire("session", PROFILE, C)


def test_guard_serializes_publication_and_takeover(tmp_path):
    root = tmp_path / "db"
    with authority(root) as db, authority(root) as other:
        owner = db.acquire("session", PROFILE, A)
        db.db.execute("CREATE TABLE publications(call TEXT PRIMARY KEY, result BLOB)")
        with db.guard(owner) as connection:
            connection.execute("INSERT INTO publications VALUES(?,?)", ("call", b"result"))
            with pytest.raises(remote.Refused, match="unavailable"):
                other.request_fence(owner, "stop")
        other.request_fence(owner, "stop")
        with pytest.raises(remote.Refused, match="revoked"):
            with db.guard(owner) as connection:
                connection.execute("INSERT INTO publications VALUES(?,?)", ("stale", b"wrong"))
        assert [tuple(row) for row in db.db.execute("SELECT * FROM publications")] == [
            ("call", b"result")
        ]


def test_guard_rollback_does_not_publish(tmp_path):
    with authority(tmp_path / "db") as db:
        owner = db.acquire("session", PROFILE, A)
        db.db.execute("CREATE TABLE publications(result BLOB)")
        with pytest.raises(ValueError):
            with db.guard(owner) as connection:
                connection.execute("INSERT INTO publications VALUES(?)", (b"uncommitted",))
                raise ValueError("interrupted")
        assert db.db.execute("SELECT COUNT(*) FROM publications").fetchone()[0] == 0
        current(db, owner)


@pytest.mark.parametrize("ledger", ["sessions", "resources", "fences"])
def test_metadata_capacity_refuses_without_losing_existing_authority(tmp_path, ledger):
    with authority(tmp_path / "db", capacity=2) as db:
        owner = db.acquire("session", PROFILE, A)
        if ledger == "sessions":
            other = db.acquire("other", PROFILE, B)
            with pytest.raises(remote.Refused, match="capacity"):
                db.acquire("overflow", PROFILE, C)
            current(db, owner)
            current(db, other)
        else:
            stopped(db, owner, "one")
            owner = db.acquire("session", PROFILE, B)
            if ledger == "resources":
                stopped(db, owner, "two")
                with pytest.raises(remote.Refused, match="capacity"):
                    db.acquire("session", PROFILE, C)
            else:
                db.db.execute(
                    "INSERT INTO fences VALUES('full',?,?,'confirmed')",
                    (
                        db._encode(remote.Fence("full", C, "guest", False, ("unused",))),
                        db._encode(owner),
                    ),
                )
                with pytest.raises(remote.Refused, match="capacity"):
                    db.request_fence(owner, "overflow")
                current(db, owner)


@pytest.mark.parametrize("field,value", [("token", "wrong"), ("generation", 0), ("resource", {})])
def test_corrupt_owner_metadata_refuses(tmp_path, field, value):
    with authority(tmp_path / "db") as db:
        owner = db.acquire("session", PROFILE, A)
        record = json.loads(db._encode(owner))
        record["record"][field] = value
        db.db.execute("UPDATE sessions SET owner=?", (json.dumps(record),))
        with pytest.raises(remote.Refused):
            current(db, owner)


def test_wrong_format_and_storage_failure_refuse(tmp_path):
    root = tmp_path / "db"
    with authority(root) as db:
        owner = db.acquire("session", PROFILE, A)
        db.db.execute("PRAGMA query_only=ON")
        with pytest.raises(remote.Refused, match="unavailable"):
            db.request_fence(owner, "stop")
        db.db.execute("PRAGMA query_only=OFF")
        current(db, owner)
        db.db.execute("UPDATE settings SET version=999")
    with pytest.raises(remote.Refused, match="format"):
        authority(root)


CHILD = """
import sys, os
from contextlib import contextmanager
from pathlib import Path
from scripts.experiments.mxc_session_patch.remote_authority import Authority, Resource
root, operation, boundary = sys.argv[1:]
with Authority(Path(root), enabled=True) as db:
    owner = db.acquire('session', 'a'*64, Resource('host-a','boot-a','guest-a'))
    original = db._transaction
    @contextmanager
    def crash():
        with original():
            yield
            if boundary == 'before_commit':
                os._exit(76)
        os._exit(76)
    db._transaction = crash
    if operation == 'fence':
        db.request_fence(owner, 'stop')
    else:
        with db.guard(owner) as connection:
            connection.execute('INSERT INTO publications VALUES(?,?)', ('call', b'result'))
"""


@pytest.mark.parametrize("operation", ["fence", "publication"])
@pytest.mark.parametrize("boundary", ["before_commit", "after_commit"])
def test_process_death_preserves_exact_committed_transition(tmp_path, operation, boundary):
    root = tmp_path / "db"
    with authority(root) as db:
        owner = db.acquire("session", PROFILE, A)
        db.db.execute("CREATE TABLE publications(call TEXT PRIMARY KEY,result BLOB)")
    child = subprocess.run(
        [sys.executable, "-c", CHILD, str(root), operation, boundary],
        cwd=Path(__file__).parents[1],
        capture_output=True,
        timeout=20,
    )
    assert child.returncode == 76, child.stderr.decode()
    committed = boundary == "after_commit"
    with authority(root) as db:
        if operation == "fence" and committed:
            with pytest.raises(remote.Refused, match="revoked"):
                current(db, owner)
            with pytest.raises(remote.Refused, match="not confirmed"):
                db.acquire("session", PROFILE, B)
            db.confirm_fence("stop", lambda _: True)
            current(db, db.acquire("session", PROFILE, B))
        else:
            current(db, owner)
        rows = [tuple(row) for row in db.db.execute("SELECT * FROM publications")]
        assert rows == ([("call", b"result")] if operation == "publication" and committed else [])


RACE = """
import json, sys, time
from pathlib import Path
from scripts.experiments.mxc_session_patch.remote_authority import Authority, Resource, Refused
root, signal, label, operation, encoded = sys.argv[1:]
with Authority(Path(root), enabled=True) as db:
    Path(signal + label).write_text('ready')
    until = time.monotonic() + 15
    while not Path(signal).exists():
        if time.monotonic() >= until:
            raise TimeoutError('start signal absent')
        time.sleep(0.01)
    try:
        if operation == 'acquire':
            owner = db.acquire('session', 'a'*64, Resource(label, 'boot', 'guest'))
            print(json.dumps({'ok': True, 'owner': db._encode(owner)}))
        else:
            db.request_fence(db._decode_owner(encoded), label)
            print(json.dumps({'ok': True, 'fence': label}))
    except Refused as error:
        print(json.dumps({'ok': False, 'error': str(error)}))
"""


@pytest.mark.parametrize("operation", ["acquire", "fence"])
def test_competing_processes_grant_exactly_one_owner_or_takeover(tmp_path, operation):
    import time

    root = tmp_path / "db"
    signal = tmp_path / "start"
    with authority(root) as db:
        encoded = db._encode(db.acquire("session", PROFILE, A)) if operation == "fence" else ""
    children = []
    try:
        for label in ("one", "two"):
            child = subprocess.Popen(
                [sys.executable, "-c", RACE, str(root), str(signal), label, operation, encoded],
                cwd=Path(__file__).parents[1],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            children.append(child)
            deadline = time.monotonic() + 15
            while not Path(str(signal) + label).exists():
                assert child.poll() is None, child.communicate()
                assert time.monotonic() < deadline
                time.sleep(0.01)
        signal.write_text("go")
        results = []
        for child in children:
            stdout, stderr = child.communicate(timeout=20)
            assert child.returncode == 0, stderr.decode()
            results.append(json.loads(stdout))
        assert sum(result["ok"] for result in results) == 1, results
        with authority(root) as db:
            winner = next(result for result in results if result["ok"])
            if operation == "acquire":
                current(db, db._decode_owner(winner["owner"]))
                assert db.db.execute("SELECT COUNT(*) FROM resources").fetchone()[0] == 1
            else:
                assert db.db.execute("SELECT COUNT(*) FROM fences").fetchone()[0] == 1
                with pytest.raises(remote.Refused, match="not confirmed"):
                    db.acquire("session", PROFILE, B)
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
            child.communicate(timeout=10)


CONFIRM_CRASH = """
import sys, os
from contextlib import contextmanager
from pathlib import Path
from scripts.experiments.mxc_session_patch.remote_authority import Authority
root, boundary = sys.argv[1:]
with Authority(Path(root), enabled=True) as db:
    original = db._transaction
    count = 0
    @contextmanager
    def crash():
        global count
        count += 1
        with original():
            yield
            if count == 2 and boundary == 'before_commit':
                os._exit(76)
        if count == 2:
            os._exit(76)
    db._transaction = crash
    db.confirm_fence('stop', lambda _: True)
"""


@pytest.mark.parametrize("boundary", ["before_commit", "after_commit"])
def test_confirmation_crash_never_activates_replacement(tmp_path, boundary):
    root = tmp_path / "db"
    with authority(root) as db:
        owner = db.acquire("session", PROFILE, A)
        db.request_fence(owner, "stop")
    child = subprocess.run(
        [sys.executable, "-c", CONFIRM_CRASH, str(root), boundary],
        cwd=Path(__file__).parents[1],
        capture_output=True,
        timeout=20,
    )
    assert child.returncode == 76, child.stderr.decode()
    with authority(root) as db:
        with pytest.raises(remote.Refused, match="revoked"):
            current(db, owner)
        if boundary == "before_commit":
            with pytest.raises(remote.Refused, match="not confirmed"):
                db.acquire("session", PROFILE, B)
        else:
            assert (
                db.db.execute("SELECT state FROM sessions WHERE id='session'").fetchone()[0]
                == "stopped"
            )
        db.confirm_fence("stop", lambda _: True)
        replacement = db.acquire("session", PROFILE, B)
        current(db, replacement)


@pytest.mark.parametrize("evidence", [False, None, 1, "stopped"])
def test_unverified_termination_never_grants_execution(tmp_path, evidence):
    with authority(tmp_path / "db") as db:
        owner = db.acquire("session", PROFILE, A)
        db.request_fence(owner, "stop")
        with pytest.raises(remote.Refused, match="not confirmed"):
            db.confirm_fence("stop", lambda _: evidence)
        with pytest.raises(remote.Refused, match="not confirmed"):
            db.acquire("session", PROFILE, B)


def test_changed_policy_cannot_repeat_automatic_shutdown(tmp_path):
    root = tmp_path / "db"
    with authority(root, policy=WHOLE) as db:
        owner = db.acquire("session", PROFILE, A)
        db.request_fence(owner, "stop", automatic=True)
    with authority(root) as db:
        with pytest.raises(remote.Refused, match="automatic shutdown is disabled"):
            db.request_fence(owner, "stop", automatic=True)
        with pytest.raises(remote.Refused, match="not confirmed"):
            db.acquire("session", PROFILE, B)


def test_sqlite_full_keeps_prior_authority_and_rolls_back_publication(tmp_path):
    with authority(tmp_path / "db") as db:
        owner = db.acquire("session", PROFILE, A)
        size = db.db.execute("PRAGMA page_size").fetchone()[0]
        assert (
            db.db.execute("PRAGMA max_page_count").fetchone()[0] * size == remote.MAX_DATABASE_BYTES
        )
        db.db.execute("CREATE TABLE publications(result BLOB)")
        pages = db.db.execute("PRAGMA page_count").fetchone()[0]
        db.db.execute(f"PRAGMA max_page_count={pages}")
        with pytest.raises(remote.Refused, match="unavailable"):
            with db.guard(owner) as connection:
                connection.execute("INSERT INTO publications VALUES(zeroblob(1048576))")
        current(db, owner)
        assert db.db.execute("SELECT COUNT(*) FROM publications").fetchone()[0] == 0


def test_corrupt_stopped_state_cannot_bypass_pending_termination(tmp_path):
    with authority(tmp_path / "db") as db:
        owner = db.acquire("session", PROFILE, A)
        db.request_fence(owner, "stop")
        db.db.execute("UPDATE sessions SET state='stopped'")
        with pytest.raises(remote.Refused, match="lacks confirmed"):
            db.acquire("session", PROFILE, B)


@pytest.mark.parametrize(
    "field,value", [("generation", 100), ("profile", "b" * 64), ("fence", "missing")]
)
def test_corrupt_stopped_binding_refuses_replacement(tmp_path, field, value):
    with authority(tmp_path / "db") as db:
        owner = db.acquire("session", PROFILE, A)
        stopped(db, owner)
        db.db.execute(f"UPDATE sessions SET {field}=?", (value,))
        with pytest.raises(remote.Refused):
            db.acquire("session", value if field == "profile" else PROFILE, B)


def test_active_profile_change_refuses_publication(tmp_path):
    with authority(tmp_path / "db") as db:
        owner = db.acquire("session", PROFILE, A)
        db.db.execute("UPDATE sessions SET profile=?", ("b" * 64,))
        with pytest.raises(remote.Refused, match="revoked"):
            current(db, owner)


@pytest.mark.parametrize("name", ["shared.sqlite", "state.sqlite"])
def test_existing_local_store_is_not_silently_promoted(tmp_path, name):
    root = tmp_path / "local"
    remote.prepare(root)
    original = root / name
    original.write_bytes(b"local-state")
    with pytest.raises(remote.Refused, match="explicit migration"):
        authority(root)
    assert original.read_bytes() == b"local-state"
    assert not (root / "authority.sqlite").exists()


INITIALIZE_CRASH = """
import os, sys
from contextlib import contextmanager
from pathlib import Path
from scripts.experiments.mxc_session_patch.remote_authority import Authority
root, boundary = sys.argv[1:]
original = Authority._transaction
@contextmanager
def crash(self):
    with original(self):
        if boundary == 'before_schema':
            os._exit(76)
        yield
        if boundary == 'before_commit':
            os._exit(76)
    os._exit(76)
Authority._transaction = crash
Authority(Path(root), enabled=True, capacity=17)
"""


@pytest.mark.parametrize("boundary", ["before_schema", "before_commit", "after_commit"])
def test_initialization_crash_can_reopen_and_preserves_configuration(tmp_path, boundary):
    root = tmp_path / "db"
    child = subprocess.run(
        [sys.executable, "-c", INITIALIZE_CRASH, str(root), boundary],
        cwd=Path(__file__).parents[1],
        capture_output=True,
        timeout=20,
    )
    assert child.returncode == 76, child.stderr.decode()
    assert (root / "authority.sqlite").exists()
    with authority(root, capacity=17) as db:
        owner = db.acquire("session", PROFILE, A)
        current(db, owner)
    with authority(root, capacity=17) as db:
        assert db.acquire("session", PROFILE, A) == owner
    with pytest.raises(remote.Refused, match="capacity"):
        authority(root, capacity=18)


def test_nonempty_foreign_schema_is_not_initialized(tmp_path):
    import sqlite3

    root = tmp_path / "db"
    remote.prepare(root)
    with sqlite3.connect(root / "authority.sqlite") as connection:
        connection.execute("CREATE TABLE foreign_data(value TEXT)")
        connection.execute("INSERT INTO foreign_data VALUES('preserved')")
    with pytest.raises(remote.Refused, match="unavailable or corrupt"):
        authority(root)
    with sqlite3.connect(root / "authority.sqlite") as connection:
        assert connection.execute("SELECT * FROM foreign_data").fetchall() == [("preserved",)]
        assert connection.execute("SELECT name FROM sqlite_schema").fetchall() == [
            ("foreign_data",)
        ]
