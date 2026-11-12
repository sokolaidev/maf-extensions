"""Coordinator rollback refusal and ordinary-process commit interruption controls."""

from __future__ import annotations

import base64
import importlib
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))
try:
    api = importlib.import_module("scripts.experiments.mxc_session_patch.remote_api")
finally:
    sys.path.remove(str(Path(__file__).parents[1]))

LIMITS = api.Limits(200_000, 500_000, checkpoint_bytes=1024, result_bytes=128, files=2)
SESSIONS = {"session": api.Session({"runtime": "pinned", "policy": "closed"}, LIMITS)}
GRANTS = {c * 64: api.Grant("host-" + c, "boot-" + c, frozenset(SESSIONS)) for c in ("a", "b")}
JOURNALS = ("ownership/authority.sqlite", "publication/shared.sqlite")


def coordinator(root, **kwargs):
    kwargs.setdefault("freshness_root", root.parent / "freshness")
    return closing(api.Coordinator(root, GRANTS, SESSIONS, enabled=True, **kwargs))


def request(db, operation, *, who="a", **arguments):
    return db.dispatch(
        who * 64,
        {"version": 1, "operation": operation, "session": "session", "arguments": arguments},
    )


def encoded(value):
    return base64.b64encode(value).decode("ascii")


def publish(db, owner, call):
    assert request(db, "begin", owner=owner, call=call, request=encoded(b"code"))["result"] is None
    request(
        db,
        "publish",
        owner=owner,
        call=call,
        request=encoded(b"code"),
        files={"index.json": encoded(call.encode())},
        result=encoded(call.encode()),
    )


@pytest.mark.parametrize("restore", [(), (0,), (1,), (0, 1)])
def test_stale_or_mixed_journals_refused_before_serving(tmp_path, restore):
    root = tmp_path / "service"
    with coordinator(root, initialize=True) as db:
        owner = request(db, "acquire", guest="guest-a")["owner"]
        publish(db, owner, "seed")
    old = [(root / p).read_bytes() for p in JOURNALS]
    with coordinator(root) as db:
        publish(db, owner, "completed")
        db.fence(api.Authority._decode_owner(owner), "stop-a", lambda _: True)
        replacement = request(db, "acquire", who="b", guest="guest-b")["owner"]
    latest = [(root / p).read_bytes() for p in JOURNALS]
    witness = tmp_path / "freshness" / "freshness.sqlite"
    retained = witness.read_bytes()
    for index in restore:
        (root / JOURNALS[index]).write_bytes(old[index])
    if restore:
        with pytest.raises(api.Refused, match="freshness"):
            with coordinator(root):
                pytest.fail("stale journals were accepted")
        assert witness.read_bytes() == retained
        for index, data in enumerate(latest):
            (root / JOURNALS[index]).write_bytes(data)
    with coordinator(root) as db:
        with pytest.raises(api.Refused):
            request(db, "acquire", guest="guest-a")
        assert request(
            db, "retry", who="b", owner=replacement, call="completed", request=encoded(b"code")
        ) == {"result": encoded(b"completed")}
        assert request(db, "restore", who="b", owner=replacement)["files"] == {
            "index.json": encoded(b"completed")
        }


@pytest.mark.parametrize("damage", ["missing", "empty", "corrupt", "stale"])
def test_unavailable_or_stale_record_refuses_without_recreation(tmp_path, damage):
    root = tmp_path / "service"
    with coordinator(root, initialize=True):
        pass
    path = tmp_path / "freshness" / "freshness.sqlite"
    old = path.read_bytes()
    with coordinator(root) as db:
        request(db, "acquire", guest="guest-a")
    if damage == "missing":
        path.unlink()
    else:
        path.write_bytes({"empty": b"", "corrupt": b"corrupt", "stale": old}[damage])
    saved = path.read_bytes() if path.exists() else None
    with pytest.raises(api.Refused, match="freshness"):
        with coordinator(root):
            pytest.fail("invalid freshness record was accepted")
    assert (path.read_bytes() if path.exists() else None) == saved


def test_record_failure_during_service_refuses_read_and_write(tmp_path):
    root = tmp_path / "service"
    with coordinator(root, initialize=True) as db:
        owner = request(db, "acquire", guest="guest-a")["owner"]
        publish(db, owner, "completed")
        path = tmp_path / "freshness" / "freshness.sqlite"
        saved = path.read_bytes()
        path.unlink()
        for operation, args in [
            ("retry", dict(owner=owner, call="completed", request=encoded(b"code"))),
            ("acquire", dict(guest="guest-a")),
        ]:
            with pytest.raises(api.Refused, match="freshness"):
                request(db, operation, **args)
            assert not path.exists()
        path.write_bytes(saved)
        assert request(db, "acquire", guest="guest-a")["owner"] == owner


def test_protected_journals_cannot_drop_configuration_or_use_another_record(tmp_path):
    root = tmp_path / "service"
    with coordinator(root, initialize=True):
        pass
    with pytest.raises(api.Refused, match="requires its freshness"):
        with coordinator(root, freshness_root=None):
            pass
    with coordinator(tmp_path / "other", freshness_root=tmp_path / "other-record", initialize=True):
        pass
    with pytest.raises(api.Refused, match="freshness"):
        with coordinator(root, freshness_root=tmp_path / "other-record"):
            pass
    with pytest.raises(api.Refused, match="requires its freshness"):
        api.SharedStore(root / "publication", "session", dict(SESSIONS["session"].profile), LIMITS)


def test_existing_stores_are_never_adopted(tmp_path):
    root = tmp_path / "service"
    with coordinator(root, freshness_root=None, initialize=True):
        pass
    before = [(root / p).read_bytes() for p in JOURNALS]
    with pytest.raises(api.Refused):
        with coordinator(root, initialize=True):
            pass
    assert not (tmp_path / "freshness").exists()
    with coordinator(tmp_path / "other", initialize=True):
        pass
    with pytest.raises(api.Refused, match="migration"):
        with coordinator(root):
            pass
    assert [(root / p).read_bytes() for p in JOURNALS] == before


@pytest.mark.parametrize("location", ["service", "service/record", "."])
def test_freshness_root_must_be_separate(tmp_path, location):
    with pytest.raises(api.Refused, match="outside"):
        with coordinator(tmp_path / "service", freshness_root=tmp_path / location, initialize=True):
            pass


def test_one_coordinator_per_record_and_multiple_publication_sessions(tmp_path):
    sessions = {name: SESSIONS["session"] for name in ("session", "second")}
    grants = {"a" * 64: api.Grant("host-a", "boot-a", frozenset(sessions))}
    with closing(
        api.Coordinator(
            tmp_path / "service",
            grants,
            sessions,
            enabled=True,
            initialize=True,
            freshness_root=tmp_path / "freshness",
        )
    ) as db:
        with pytest.raises(api.Refused):
            api.freshness.Witness(tmp_path / "freshness", tmp_path / "other")
        owner = request(db, "acquire", guest="guest-a")["owner"]
        publish(db, owner, "first")
        db.dispatch(
            "a" * 64,
            {
                "version": 1,
                "operation": "acquire",
                "session": "second",
                "arguments": {"guest": "guest-second"},
            },
        )
    with closing(
        api.Coordinator(
            tmp_path / "service",
            grants,
            sessions,
            enabled=True,
            freshness_root=tmp_path / "freshness",
        )
    ):
        pass


def test_freshness_failure_rolls_back_publication_and_recovers(tmp_path, monkeypatch):
    root = tmp_path / "service"
    with coordinator(root, initialize=True) as db:
        owner = request(db, "acquire", guest="guest-a")["owner"]
        request(db, "begin", owner=owner, call="call", request=encoded(b"code"))
        original = api.freshness.Witness.advance

        def fail(self, connection, role, previous):
            original(self, connection, role, previous)
            if role == "publication":
                raise sqlite3.OperationalError("injected disk failure")

        args = dict(
            owner=owner,
            call="call",
            request=encoded(b"code"),
            files={"index.json": encoded(b"checkpoint")},
            result=encoded(b"result"),
        )
        with monkeypatch.context() as patch:
            patch.setattr(api.freshness.Witness, "advance", fail)
            with pytest.raises(api.Refused, match="freshness"):
                request(db, "publish", **args)
        request(db, "publish", **args)
    with coordinator(root) as db:
        assert request(db, "retry", owner=owner, call="call", request=encoded(b"code")) == {
            "result": encoded(b"result")
        }


def test_scratch_reconciliation_preserves_record_and_result(tmp_path):
    root = tmp_path / "service"
    policy = api.remote_scratch.Policy(bytes=4096, entries=8, total_bytes=4096)
    with coordinator(root, initialize=True, scratch_policy=policy) as db:
        owner = request(db, "acquire", guest="guest-a")["owner"]
        publish(db, owner, "completed")
        receipt = db.pending_scratch()[0]
    with coordinator(root, scratch_policy=policy) as db:
        assert db.pending_scratch() == [receipt]
        db.reconcile_scratch(receipt, lambda _: True)
        assert db.pending_scratch() == []
        assert request(db, "retry", owner=owner, call="completed", request=encoded(b"code")) == {
            "result": encoded(b"completed")
        }
        publish(db, owner, "next")


def test_partial_provisioning_cannot_be_resumed_or_certified(tmp_path, monkeypatch):
    root = tmp_path / "service"
    original = api.freshness.Witness.advance

    def fail(self, connection, role, previous):
        if role == "publication":
            raise sqlite3.OperationalError("provisioning interrupted")
        original(self, connection, role, previous)

    with monkeypatch.context() as patch:
        patch.setattr(api.freshness.Witness, "advance", fail)
        with pytest.raises(api.Refused):
            with coordinator(root, initialize=True):
                pass
    witness = tmp_path / "freshness" / "freshness.sqlite"
    saved = witness.read_bytes()
    with pytest.raises(api.Refused, match="incomplete"):
        with coordinator(root):
            pass
    with pytest.raises(api.Refused, match="empty root"):
        with coordinator(root, initialize=True):
            pass
    assert witness.read_bytes() == saved


CRASH = r"""
import base64, os, sys
from pathlib import Path
from scripts.experiments.mxc_session_patch import remote_api as api
root, role, stage = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
limits = api.Limits(200000, 500000, checkpoint_bytes=1024, result_bytes=128, files=2)
db = api.Coordinator(root, {'a'*64: api.Grant('host-a','boot-a',frozenset({'session'}))},
    {'session': api.Session({'runtime':'pinned','policy':'closed'}, limits)},
    enabled=True, freshness_root=root.parent/'freshness')
def call(operation, **arguments):
    return db.dispatch('a'*64, dict(version=1,operation=operation,session='session',arguments=arguments))
original = api.freshness.Witness.advance
def crash(self, connection, target, previous):
    if target == role and stage == 'before_record':
        os._exit(76)
    original(self, connection, target, previous)
    if target == role and stage == 'before_commit':
        os._exit(76)
if role == 'ownership':
    api.freshness.Witness.advance = crash
    call('acquire', guest='guest-a')
else:
    owner = call('acquire', guest='guest-a')['owner']
    code = base64.b64encode(b'code').decode()
    call('begin', owner=owner, call='call', request=code)
    api.freshness.Witness.advance = crash
    call('publish', owner=owner, call='call', request=code,
         files={'index.json':code}, result=code)
os._exit(76)
"""


@pytest.mark.parametrize("role", ["ownership", "publication"])
@pytest.mark.parametrize("stage", ["before_record", "before_commit", "after_commit"])
def test_process_exit_recovers_atomic_journal_and_record(tmp_path, role, stage):
    root = tmp_path / "service"
    with coordinator(root, initialize=True):
        pass
    result = subprocess.run(
        [sys.executable, "-c", CRASH, str(root), role, stage],
        cwd=Path(__file__).parents[1],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 76, result.stderr
    with coordinator(root) as db:
        with db.authority() as authority:
            rows = authority.db.execute("SELECT owner FROM sessions").fetchall()
            if role == "ownership":
                assert bool(rows) == (stage == "after_commit")
                return
            owner = rows[0][0]
        if stage == "after_commit":
            assert request(db, "retry", owner=owner, call="call", request=encoded(b"code")) == {
                "result": encoded(b"code")
            }
        else:
            assert (
                db.stores["session"][1]
                .db.execute("SELECT status FROM calls WHERE id='call'")
                .fetchone()[0]
                == "interrupted"
            )
            with pytest.raises(api.Refused, match="explicit recovery"):
                request(db, "retry", owner=owner, call="call", request=encoded(b"code"))
