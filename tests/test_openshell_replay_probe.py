"""The retention fixture must alter exactly one create receipt in a stopped isolated gateway."""

from __future__ import annotations

import importlib
import json
import sqlite3
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest


@pytest.fixture
def injection(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts/experiments"))
    probe = importlib.import_module("openshell_replay_probe")
    database = tmp_path / "gateway.db"
    with sqlite3.connect(database) as db:
        db.execute(
            "CREATE TABLE objects (id TEXT, object_type TEXT, payload BLOB, created_at_ms INTEGER, updated_at_ms INTEGER)"
        )
        for row_id, kind, owner in (
            ("match", "mutation_admission_v1", "original"),
            ("other", "mutation_admission_v1", "other"),
            ("outside", "another_type", "original"),
        ):
            payload = {
                "success": {"Ordinary": {"Sandbox": {"id": owner}}},
                "completed_at_ms": 123,
                "payload_hash": "unchanged",
            }
            db.execute(
                "INSERT INTO objects VALUES (?,?,?,?,?)",
                (row_id, kind, json.dumps(payload).encode(), 123, 123),
            )
    db.close()
    stopped = []
    started = []
    gateway = SimpleNamespace(
        stop=lambda: stopped.append(True),
        start=lambda: started.append(True),
        process=SimpleNamespace(poll=lambda: 0 if stopped else None),
    )
    return probe, {"database": database, "gateway": gateway}, stopped, started


def rows(control):
    with sqlite3.connect(control["database"]) as db:
        result = db.execute("SELECT * FROM objects ORDER BY id").fetchall()
    db.close()
    return {row[0]: row for row in result}


@pytest.mark.parametrize("mode", ["expired_success", "unresolved"])
def test_injection_changes_only_selected_create_receipt(injection, mode):
    probe, control, stopped, started = injection
    before = rows(control)
    probe.inject_admission(control, "original", mode)
    after = rows(control)
    assert stopped == started == [True]
    assert after["other"] == before["other"] and after["outside"] == before["outside"]
    payload = json.loads(after["match"][2])
    assert payload["payload_hash"] == "unchanged"
    assert abs(after["match"][3] - (int(time.time() * 1000) - 25 * 60 * 60 * 1000)) < 2000
    if mode == "expired_success":
        assert payload["success"] == json.loads(before["match"][2])["success"]
        assert payload["completed_at_ms"] == after["match"][3]
    else:
        assert payload["success"] is None and payload["completed_at_ms"] is None


@pytest.mark.parametrize("count", [0, 2])
def test_missing_or_ambiguous_receipt_never_changes_database(injection, count):
    probe, control, _, started = injection
    with sqlite3.connect(control["database"]) as db:
        if count == 0:
            db.execute("DELETE FROM objects WHERE id='match'")
        else:
            db.execute(
                "INSERT INTO objects SELECT 'duplicate',object_type,payload,created_at_ms,updated_at_ms FROM objects WHERE id='match'"
            )
    db.close()
    before = rows(control)
    with pytest.raises(RuntimeError, match="expected one"):
        probe.inject_admission(control, "original", "expired_success")
    assert rows(control) == before and started == [True]


def test_unknown_injection_never_changes_database(injection):
    probe, control, _, started = injection
    before = rows(control)
    with pytest.raises(ValueError, match="unknown fault"):
        probe.inject_admission(control, "original", "invalid")
    assert rows(control) == before and started == [True]


def test_running_gateway_refuses_database_injection(injection):
    probe, control, _, started = injection
    control["gateway"].process.poll = lambda: None
    before = rows(control)
    with pytest.raises(RuntimeError, match="did not stop"):
        probe.inject_admission(control, "original", "expired_success")
    assert rows(control) == before and not started


@pytest.mark.parametrize("optimized", [False, True])
@pytest.mark.parametrize("mode", ["owned", "replacement", "wrong_delete", "still_present"])
def test_fixture_disposal_preserves_guards_under_optimization(optimized, mode):
    code = textwrap.dedent("""
        import json
        import sys
        from types import SimpleNamespace

        sys.path.insert(0, sys.argv[1])
        import openshell_allocation_probe as probe

        calls = []
        owner = {"name": "fixture", "sandbox_id": "original", "delete_request_id": "request",
                 "containers": ["container"], "processes": {"1": "2"}}
        mode = sys.argv[2]

        def current(name):
            calls.append("lookup")
            return "replacement" if mode == "replacement" else "original"

        def delete(name, request_id):
            calls.append(["delete", name, request_id])
            return "replacement" if mode == "wrong_delete" else "original"

        def absent(record):
            calls.append(["observe", record])
            return mode != "still_present"

        probe.verify_absent = absent
        error = None
        try:
            probe.dispose_fixture(SimpleNamespace(current_id=current, delete=delete), owner)
        except RuntimeError as exc:
            error = str(exc)
        print(json.dumps({"calls": calls, "error": error}))
    """)
    result = subprocess.run(
        [
            sys.executable,
            *(["-O"] if optimized else []),
            "-c",
            code,
            str(Path(__file__).resolve().parents[1] / "scripts/experiments"),
            mode,
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=20,
    )
    receipt = json.loads(result.stdout)
    expected = ["lookup"]
    if mode != "replacement":
        expected.append(["delete", "fixture", "request"])
    if mode in ("owned", "still_present"):
        expected.append(
            [
                "observe",
                {
                    "id": "original",
                    "observed": {"containers": [{"Id": "container"}], "processes": {"1": "2"}},
                },
            ]
        )
    assert receipt["calls"] == expected
    assert (
        receipt["error"]
        == {
            "owned": None,
            "replacement": "fixture identity changed before deletion",
            "wrong_delete": "fixture delete targeted another instance",
            "still_present": "fixture cleanup incomplete; retain identities",
        }[mode]
    )


@pytest.fixture(params=[0, 2], ids=["normal", "optimized"])
def load_probe(request, monkeypatch):
    directory = Path(__file__).resolve().parents[1] / "scripts/experiments"
    monkeypatch.syspath_prepend(str(directory))

    def load(name):
        path = directory / (name + ".py")
        module = ModuleType(name)
        module.__file__ = str(path)
        exec(
            compile(path.read_text(encoding="utf-8"), str(path), "exec", optimize=request.param),
            module.__dict__,
        )
        return module

    return load


@pytest.mark.parametrize("stage", ["get", "snapshot", "validate", "deadline", "ready"])
def test_create_hands_off_identity_before_readiness(load_probe, monkeypatch, tmp_path, stage):
    probe = load_probe("openshell_allocation_probe")
    owner_path = tmp_path / "admitted.json"
    retained = []
    client = probe.Client.__new__(probe.Client)
    client.scope = object()
    client.pb = SimpleNamespace(
        CreateSandboxRequest=SimpleNamespace(
            FromString=lambda value: SimpleNamespace(name="fixture")
        ),
        GetSandboxRequest=SimpleNamespace,
        SANDBOX_PHASE_READY=1,
    )

    def admitted(record):
        retained.append(record)
        probe._write(owner_path, record)

    def rpc(method, request):
        if method == "CreateSandbox":
            return SimpleNamespace(sandbox=SimpleNamespace(metadata=SimpleNamespace(id="original")))
        assert retained and json.loads(owner_path.read_text())["sandbox_id"] == "original"
        if stage == "get":
            raise RuntimeError("get")
        return SimpleNamespace(
            sandbox=SimpleNamespace(
                metadata=SimpleNamespace(id="original"),
                status=SimpleNamespace(phase=0 if stage == "deadline" else 1),
            )
        )

    def snapshot(identifier):
        assert retained[0]["sandbox_id"] == identifier
        if stage == "snapshot":
            raise RuntimeError("snapshot")
        return {"containers": [{"Id": "captured"}], "processes": {"1": "2"}}

    def validate(record):
        if stage == "validate":
            raise RuntimeError("validate")

    client.rpc = rpc
    monkeypatch.setattr(probe, "snapshot", snapshot)
    monkeypatch.setattr(probe, "validate", validate)
    ticks = iter([0, 0, 31])
    monkeypatch.setattr(probe.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(probe.time, "sleep", lambda seconds: None)
    if stage == "ready":
        result = client.send(b"request", on_created=admitted)
        assert result is retained[0]
        assert result["containers"] == ["captured"]
    else:
        with pytest.raises(
            RuntimeError, match="did not become ready" if stage == "deadline" else stage
        ):
            client.send(b"request", on_created=admitted)
    assert retained[0]["sandbox_id"] == "original"
    assert json.loads(owner_path.read_text())["containers"] == []


@pytest.mark.parametrize("state", ["present", "absent", "unavailable"])
def test_workspace_cleanup_requires_explicit_absence(load_probe, state):
    probe = load_probe("openshell_allocation_probe")

    class RpcError(Exception):
        def code(self):
            return state

    calls = []

    def rpc(method, request):
        calls.append((method, request.name))
        if state != "present":
            raise RpcError(state)
        return object()

    client = SimpleNamespace(
        rpc=rpc,
        pb=SimpleNamespace(GetWorkspaceRequest=SimpleNamespace),
        grpc=SimpleNamespace(RpcError=RpcError, StatusCode=SimpleNamespace(NOT_FOUND="absent")),
    )
    if state == "absent":
        assert probe.verify_workspace_absent(client, "fixture") is True
    else:
        with pytest.raises(RuntimeError if state == "present" else RpcError):
            probe.verify_workspace_absent(client, "fixture")
    assert calls == [("GetWorkspace", "fixture")]


def test_replacement_waits_for_independent_absence(load_probe, monkeypatch, tmp_path):
    probe = load_probe("openshell_lifecycle_probe")
    created = []
    checks = iter([False, True])

    class Fixture:
        def __init__(self, *args):
            self.client = SimpleNamespace(wait_deleted=lambda *args, **kwargs: None)

        def create(self, name, *, on_created):
            record = {"name": name, "sandbox_id": "original", "delete_request_id": "request"}
            created.append(record)
            on_created(record)
            return record

        def current_id(self, name):
            return "original"

        def delete(self, name, request_id):
            return "original"

        def verify_absent(self, record):
            return next(checks)

        def close(self):
            pass

    monkeypatch.setattr(probe, "Fixture", Fixture)
    monkeypatch.setattr(probe, "save", lambda *args: None)
    args = SimpleNamespace(
        endpoint="fixture", image="fixture", ledger=tmp_path / "owner", case="stale_race"
    )
    with pytest.raises(RuntimeError, match="original physical resources remain"):
        probe.replacement_case(args)
    assert len(created) == 1


@pytest.mark.parametrize("stage", ["output", "readiness", "snapshot", "ready"])
def test_lifecycle_create_hands_off_before_readiness(load_probe, monkeypatch, stage):
    probe = load_probe("openshell_lifecycle_probe")
    retained = []
    fixture = probe.Fixture.__new__(probe.Fixture)
    fixture.image = "fixture"

    def output(*args, **kwargs):
        assert retained[0]["sandbox_id"] == "original"
        if stage == "output":
            raise RuntimeError("output")

    monkeypatch.setattr(probe, "print", output, raising=False)

    def ready(*args, **kwargs):
        assert retained[0]["sandbox_id"] == "original"
        if stage == "readiness":
            raise RuntimeError("readiness")

    def snapshot(identifier):
        assert retained[0]["sandbox_id"] == identifier
        if stage == "snapshot":
            raise RuntimeError("snapshot")
        return {"containers": [{"Id": "captured"}], "processes": {"1": "2"}}

    fixture.client = SimpleNamespace(
        health=lambda: SimpleNamespace(version="0.1.2"),
        create=lambda **kwargs: SimpleNamespace(id="original"),
        wait_ready=ready,
    )
    monkeypatch.setattr(probe, "fixture_spec", lambda image: None)
    monkeypatch.setattr(probe, "snapshot", snapshot)
    if stage == "ready":
        assert fixture.create("fixture", on_created=retained.append) is retained[0]
        assert retained[0]["containers"] == ["captured"]
    else:
        with pytest.raises(RuntimeError, match=stage):
            fixture.create("fixture", on_created=retained.append)
    assert retained[0]["sandbox_id"] == "original"


@pytest.mark.parametrize("failure", ["metadata", "readiness", "snapshot", "none"])
def test_raw_replay_retains_admitted_identity_before_followup(load_probe, monkeypatch, failure):
    probe = load_probe("openshell_replay_probe")
    allocation = load_probe("openshell_allocation_probe")
    retained, calls = [], []
    client = allocation.Client.__new__(allocation.Client)
    client.scope, client.token = object(), "fixture-token"
    client.pb = SimpleNamespace(
        CreateSandboxRequest=SimpleNamespace(
            FromString=lambda payload: SimpleNamespace(name="fixture")
        ),
        GetSandboxRequest=SimpleNamespace,
        SANDBOX_PHASE_READY=1,
    )
    client.grpc = SimpleNamespace(RpcError=type("RpcError", (Exception,), {}))

    def metadata():
        assert retained[0]["sandbox_id"] == "new-instance"
        if failure == "metadata":
            raise RuntimeError("metadata")
        return []

    def create(request, **kwargs):
        calls.append("create")
        return (
            SimpleNamespace(sandbox=SimpleNamespace(metadata=SimpleNamespace(id="new-instance"))),
            SimpleNamespace(initial_metadata=metadata),
        )

    def rpc(method, request):
        calls.append(method)
        assert retained[0]["sandbox_id"] == "new-instance"
        if failure == "readiness":
            raise RuntimeError("readiness")
        return SimpleNamespace(
            sandbox=SimpleNamespace(
                metadata=SimpleNamespace(id="new-instance"), status=SimpleNamespace(phase=1)
            )
        )

    def snapshot(identifier):
        assert retained[0]["sandbox_id"] == identifier
        if failure == "snapshot":
            raise RuntimeError("snapshot")
        return {"containers": [{"Id": "captured"}], "processes": {"1": "2"}}

    client.stub = SimpleNamespace(CreateSandbox=SimpleNamespace(with_call=create))
    client.rpc = rpc
    monkeypatch.setattr(allocation, "snapshot", snapshot)
    monkeypatch.setattr(allocation, "validate", lambda owner: None)

    def exercise():
        result = probe.replay(client, b"request", on_created=retained.append)
        assert result["sandbox_id"] == retained[0]["sandbox_id"]
        assert client.wait_ready(retained[0]) is retained[0]

    if failure == "none":
        exercise()
        assert retained[0]["containers"] == ["captured"]
    else:
        with pytest.raises(RuntimeError, match=failure):
            exercise()
        assert retained[0]["containers"] == []
    assert calls.count("create") == 1
    assert retained[0]["name"] == "fixture"
