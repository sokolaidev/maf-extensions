"""Failed stop/start must retain ownership through physical and workspace cleanup."""

import asyncio
import importlib
from pathlib import Path
from types import SimpleNamespace as NS

import pytest


@pytest.fixture
def fixture_state(monkeypatch, tmp_path):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts/experiments"))
    native = importlib.import_module("openshell_native_probe")
    owned = importlib.import_module("openshell_owned_probe")
    allocation = importlib.import_module("openshell_allocation_probe")
    authority = importlib.import_module("openshell_authority_probe")
    ca = importlib.import_module("openshell_ca_probe")
    state = NS(phase="ready", deleted=False, fault=None, calls=[], tops=[], inspected=[])

    class NotFound(Exception):
        def code(self):
            return "not_found"

    def docker_get(path):
        if path.startswith("/containers/json?"):
            if state.fault == "inventory":
                raise RuntimeError("inventory unavailable")
            if state.deleted:
                return []
            return [
                {
                    "Id": role,
                    "State": "running" if state.phase == "ready" else "exited",
                    "Labels": {"openshell.ai/isolation-role": role},
                }
                for role in ("sandbox", "supervisor")
            ]
        if path.startswith("/volumes?"):
            return {"Volumes": [{"Name": "retained"}] if state.fault == "volume" else []}
        if "/top?" in path:
            state.tops.append(state.phase)
            if state.phase != "ready":
                raise RuntimeError("Docker observer HTTP 409")
            return {"Processes": [["200", "1", "sleep"]]}
        state.inspected.append(path)
        return (
            {"Id": "old"} if state.fault == "container" and path == "/containers/old/json" else None
        )

    def process_identity(pid):
        if not state.deleted or state.fault == "process":
            return {"100": "old-start", "200": "new-start"}[pid]
        return None

    def rpc(method, request):
        state.calls.append(method)
        if method == "StopSandbox":
            state.phase = "stopped"
        if method == "StartSandbox" and state.fault != "not_ready":
            raise TimeoutError("start response lost")
        if method == "GetWorkspace":
            raise NotFound()
        return NS(
            sandbox=NS(metadata=NS(id="owned-id"), status=NS(phase=state.phase)),
            SerializeToString=lambda: b"response",
        )

    def delete(name, request_id):
        assert name == "owned" and request_id == owner["delete_request_id"]
        state.calls.append("DeleteSandbox")
        state.deleted = True
        return NS(sandbox_id="other" if state.fault == "delete_identity" else "owned-id", outcome=2)

    client = NS(
        current_id=lambda name: (
            "other" if state.fault == "identity" else None if state.deleted else "owned-id"
        ),
        delete_response=delete,
        expected_version="0.1.7",
        rpc=rpc,
        scope=NS(workspace="workspace"),
        grpc=NS(RpcError=NotFound, StatusCode=NS(NOT_FOUND="not_found")),
        pb=NS(
            SANDBOX_PHASE_READY="ready",
            SANDBOX_PHASE_STOPPED="stopped",
            **{
                name + "Request": lambda **kwargs: kwargs
                for name in (
                    "StopSandbox",
                    "StartSandbox",
                    "GetSandbox",
                    "DeleteWorkspace",
                    "GetWorkspace",
                )
            },
        ),
    )
    owner = allocation.created_owner("owned", "owned-id")
    owner.update(containers=["old"], processes={"100": "old-start"})
    fixture = native.Fixture(client, {"owners": [owner]}, tmp_path / "report.json")
    fixture.owners.append(owner)
    monkeypatch.setattr(owned, "docker_get", docker_get)
    monkeypatch.setattr(owned, "process_identity", process_identity)
    monkeypatch.setattr(authority, "docker_get", docker_get)
    monkeypatch.setattr(authority, "process_identity", process_identity)
    ticks = iter(range(0, 1000, 10))
    monkeypatch.setattr(authority, "time", NS(monotonic=lambda: next(ticks), sleep=lambda _: None))
    ticks_ca = iter(range(0, 1000, 20))
    monkeypatch.setattr(ca, "time", NS(monotonic=lambda: next(ticks_ca)))
    monkeypatch.setattr(ca, "wait_gateway", lambda _: None)
    return fixture, owner, state, ca, owned


@pytest.mark.parametrize("fault", ["start_failed", "not_ready"])
def test_failed_restart_disposes_stopped_runtime_and_workspace(fixture_state, fault):
    fixture, owner, state, ca, _ = fixture_state
    state.fault = fault
    gateway = NS(stop=lambda: None, start=lambda: None, process=NS(returncode=0))
    row = {}

    async def run():
        try:
            await ca.recycle(fixture, owner, gateway, row)
        finally:
            await fixture.dispose(owner)

    with pytest.raises(
        (TimeoutError, RuntimeError), match="start response lost|requested lifecycle phase"
    ):
        asyncio.run(run())
    assert [r["state"] for r in row["mutations"]] == ["observed", "pending"]
    assert state.tops == ["ready"]
    assert owner["containers"] == ["old", "sandbox", "supervisor"]
    assert owner["processes"] == {"100": "old-start", "200": "new-start"}
    assert set(state.inspected) == {
        "/containers/old/json",
        "/containers/sandbox/json",
        "/containers/supervisor/json",
    }
    assert fixture.cleanup(workspace_created=True) == []
    assert state.calls.count("DeleteSandbox") == 1
    assert state.calls[-2:] == ["DeleteWorkspace", "GetWorkspace"]
    assert fixture.report["physical_cleanup_verified"] is True
    assert fixture.report["workspace_cleanup_verified"] is True


@pytest.mark.parametrize(
    "fault", [None, "identity", "delete_identity", "inventory", "process", "container", "volume"]
)
def test_outer_cleanup_preserves_stopped_ownership_and_absence_checks(fixture_state, fault):
    fixture, owner, state, _, _ = fixture_state
    state.phase, state.fault = "stopped", fault
    errors = fixture.cleanup(workspace_created=True)
    assert state.tops == []
    assert bool(errors) is (fault is not None)
    assert fixture.report["physical_cleanup_verified"] is (fault is None)
    assert fixture.report["workspace_cleanup_verified"] is (fault is None)
    if fault:
        assert not fixture.disposed
        assert "DeleteWorkspace" not in state.calls
        assert fixture.owners == [owner]
    if fault in {"identity", "inventory"}:
        assert "DeleteSandbox" not in state.calls


def test_runtime_observation_remains_strict_for_stopped_workloads(fixture_state):
    _, _, state, _, owned = fixture_state
    state.phase = "stopped"
    with pytest.raises(RuntimeError, match="HTTP 409"):
        owned.snapshot("owned-id")


def test_disposal_still_captures_running_processes(fixture_state):
    fixture, owner, state, _, _ = fixture_state
    asyncio.run(fixture.dispose(owner))
    assert state.tops == ["ready"]
    assert owner["processes"] == {"100": "old-start", "200": "new-start"}
    assert fixture.disposed == {"owned-id"}


@pytest.mark.parametrize("fault", [None, "replacement", "timeout", "lookup", "unknown_outcome"])
def test_workspace_waits_for_accepted_gateway_deletion(fixture_state, monkeypatch, fault):
    fixture, owner, state, _, _ = fixture_state
    allocation = importlib.import_module("openshell_allocation_probe")
    observations = []
    ticks = iter(range(100))
    monkeypatch.setattr(allocation, "time", NS(monotonic=lambda: next(ticks), sleep=lambda _: None))
    original = fixture.client.delete_response

    def delete(name, request_id):
        response = original(name, request_id)
        if fault == "unknown_outcome":
            response.outcome = 0
        return response

    def current(name):
        if not state.deleted:
            return "owned-id"
        observations.append(name)
        assert "DeleteWorkspace" not in state.calls
        if fault == "lookup":
            raise ConnectionError("gateway unavailable")
        if fault == "replacement":
            return "replacement"
        return "owned-id" if len(observations) < 3 or fault == "timeout" else None

    fixture.client.current_id = current
    fixture.client.delete_response = delete
    errors = fixture.cleanup(workspace_created=True)
    assert bool(errors) is (fault is not None)
    assert state.calls.count("DeleteSandbox") == 1
    assert owner["delete_response"]["outcome"] == (0 if fault == "unknown_outcome" else 2)
    assert fixture.report["workspace_cleanup_verified"] is (fault is None)
    assert owner.get("gateway_cleanup_verified", False) is (fault is None)
    if fault is None:
        assert observations == ["owned"] * 3
        assert owner["physical_cleanup_verified"]
    else:
        assert "DeleteWorkspace" not in state.calls
        assert not fixture.disposed
