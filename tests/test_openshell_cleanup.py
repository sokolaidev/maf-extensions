"""Execute probe cleanup boundaries without starting their Docker experiments."""

from __future__ import annotations

import ast
import importlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.mark.parametrize("probe_name", ["allocation", "lifecycle", "replay", "authority", "egress"])
@pytest.mark.parametrize(
    "failure", ["lookup", "delete", "absence", "multiple", "workspace", "close", "none"]
)
def test_cleanup_attempts_every_owner_and_client(monkeypatch, tmp_path, probe_name, failure):
    directory = Path(__file__).resolve().parents[1] / "scripts/experiments"
    monkeypatch.syspath_prepend(str(directory))
    probe = importlib.import_module(f"openshell_{probe_name}_probe")
    source_path = directory / f"openshell_{probe_name}_probe.py"
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    function = next(
        n
        for n in tree.body
        if isinstance(n, ast.FunctionDef)
        and n.name == ("replacement_case" if probe_name == "lifecycle" else "measure")
    )
    boundary = next(n for n in function.body if isinstance(n, ast.Try) and n.finalbody)
    code = compile(ast.Module(body=boundary.finalbody, type_ignores=[]), str(source_path), "exec")
    events = []
    owners = [
        dict(name=n, sandbox_id=n, id=n, delete_request_id=n, containers=[], processes={})
        for n in ("first", "middle", "last")
    ]
    failing = {"first", "last"} if failure == "multiple" else {"first"}

    class RpcError(Exception):
        def code(self):
            return "missing"

    def current(name):
        events.append(("lookup", name))
        if failure == "lookup" and name in failing:
            raise RuntimeError("lookup failed")
        return name

    def delete(name, request_id):
        events.append(("delete", name))
        if failure == "delete" and name in failing:
            raise RuntimeError("delete failed")
        return name

    def absent(owner):
        identity = owner.get("sandbox_id", owner.get("id"))
        events.append(("absent", identity))
        return not (failure in {"absence", "multiple"} and identity in failing)

    def rpc(method, request):
        if method == "GetSandbox":
            return SimpleNamespace(
                sandbox=SimpleNamespace(metadata=SimpleNamespace(id=current(request.name)))
            )
        if method == "DeleteSandbox":
            return SimpleNamespace(sandbox_id=delete(request.name, request.name))
        events.append((method, "workspace"))
        if failure == "workspace":
            raise RuntimeError("workspace failed")
        if method == "GetWorkspace":
            raise RpcError()
        return None

    def close(label):
        events.append(("close", label))
        if failure == "close" and label == "adapter":
            raise RuntimeError("close failed")

    pb = SimpleNamespace(
        GetSandboxRequest=SimpleNamespace,
        DeleteWorkspaceRequest=SimpleNamespace,
        GetWorkspaceRequest=SimpleNamespace,
    )
    grpc = SimpleNamespace(RpcError=RpcError, StatusCode=SimpleNamespace(NOT_FOUND="missing"))
    admin = SimpleNamespace(
        current_id=current, delete=delete, rpc=rpc, pb=pb, grpc=grpc, close=lambda: close("admin")
    )
    fixture = SimpleNamespace(
        current_id=current, delete=delete, verify_absent=absent, close=lambda: close("adapter")
    )
    oracle_paths = []
    for owner in owners:
        path = tmp_path / f"{owner['name']}-oracle.json"
        path.write_text(json.dumps(owner), encoding="utf-8")
        oracle_paths.append(path)
    allocation = importlib.import_module("openshell_allocation_probe")
    monkeypatch.setattr(allocation, "verify_absent", absent)
    namespace = dict(vars(probe))
    namespace.update(
        args=SimpleNamespace(evidence=SimpleNamespace(glob=lambda pattern: oracle_paths)),
        admin=admin,
        adapter=SimpleNamespace(close=lambda: close("adapter")),
        peer=SimpleNamespace(close=lambda: close("peer")),
        client=admin,
        fixture=fixture,
        channel=SimpleNamespace(close=lambda: close("adapter")),
        records=list(reversed(owners)),
        owned=list(reversed(owners)),
        owners=list(reversed(owners)),
        workspace="workspace",
        created_workspace=True,
        profile=None,
        scope=None,
        pb=pb,
        grpc=grpc,
        rpc=rpc,
        delete=lambda name: SimpleNamespace(name=name),
        verify_absent=absent,
        report={},
        name="first",
    )
    if probe_name == "egress":
        admin.close = lambda: close("adapter")
    expected_failure = failure != "none" and not (
        probe_name == "lifecycle" and failure == "workspace"
    )
    if expected_failure:
        with pytest.raises(ExceptionGroup) as raised:
            exec(code, namespace)
        errors = raised.value.exceptions
        if failure in {"lookup", "delete", "absence", "multiple"}:
            notes = " ".join(note for exc in errors for note in getattr(exc, "__notes__", []))
            assert all(identity in notes for identity in failing)
    else:
        exec(code, namespace)
    assert {name for action, name in events if action == "lookup"} == {"first", "middle", "last"}
    assert ("delete", "middle") in events
    assert ("absent", "middle") in events
    expected_closes = {
        "allocation": ["adapter", "admin"],
        "replay": ["adapter", "peer", "admin"],
        "lifecycle": ["adapter"],
        "authority": ["adapter"],
        "egress": ["adapter"],
    }[probe_name]
    assert [name for action, name in events if action == "close"] == expected_closes
    workspace_attempted = any(action == "DeleteWorkspace" for action, _ in events)
    assert workspace_attempted == (
        probe_name != "lifecycle" and failure in {"none", "workspace", "close"}
    )


def test_cleanup_retains_original_body_and_each_close_failure(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts/experiments"))
    cleanup = importlib.import_module("openshell_cleanup")
    closed = []

    def close(label):
        closed.append(label)
        raise RuntimeError(label)

    with pytest.raises(ExceptionGroup) as raised:
        with cleanup.Cleanup(lambda: close("first"), lambda: close("second")):
            raise ValueError("body")
    assert closed == ["first", "second"]
    assert [str(exc) for exc in raised.value.exceptions] == ["body", "first", "second"]


def test_provider_cleanup_attempts_all_independent_providers(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts/experiments"))
    probe = importlib.import_module("openshell_rotation_probe")
    profile = probe.RotationProfile.__new__(probe.RotationProfile)
    attempted = []

    def remove(label):
        attempted.append(label)
        raise RuntimeError("provider remains")

    profile.providers = [
        SimpleNamespace(provider_id="first", cleanup_workspace=lambda client: remove("first")),
        SimpleNamespace(provider_id="second", cleanup_workspace=lambda client: remove("second")),
    ]
    with pytest.raises(ExceptionGroup) as raised:
        profile.cleanup_workspace(object())
    assert attempted == ["second", "first"]
    assert "second" in raised.value.exceptions[0].__notes__[0]
    assert "first" in raised.value.exceptions[1].__notes__[0]
