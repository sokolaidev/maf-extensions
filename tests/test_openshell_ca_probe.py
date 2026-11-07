"""CA qualification needs applied trust, exact origin evidence and retained ownership."""

import asyncio
import importlib
import json
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest


@pytest.fixture
def probe(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts/experiments"))
    return importlib.import_module("openshell_ca_probe")


@pytest.mark.parametrize("wrong", [False, True])
def test_ca_source_must_be_explicit_disposable_copy(probe, tmp_path, wrong):
    ca = tmp_path / "ca.pem"
    ca.write_bytes(b"old-root")
    source = tmp_path / "active-ca.pem"
    source.write_bytes(b"another-root" if wrong else ca.read_bytes())
    config = tmp_path / "gateway.toml"
    config.write_text(
        "[openshell.supervisor.network]\nadditional_ca_bundle = "
        + json.dumps(str(source.resolve())),
        encoding="utf-8",
    )
    if wrong:
        with pytest.raises(ValueError, match="explicit active-ca.pem"):
            probe.ca_source(config, ca)
    else:
        assert probe.ca_source(config, ca) == source


@pytest.mark.parametrize("fault", [None, "hot_reload", "retain_removed", "timeout", "runtime"])
def test_lifecycle_requires_snapshot_overlap_and_removal_with_cleanup(
    probe, monkeypatch, tmp_path, fault
):
    ca = tmp_path / "ca.pem"
    ca.write_bytes(b"old-root")
    ca.with_name("rotation-ca.pem").write_bytes(b"new-root")
    source = ca.with_name("active-ca.pem")
    source.write_bytes(ca.read_bytes())
    applied = b"old-root"
    events = []
    disposals = []
    cycles = []
    owner = {"name": "owned", "sandbox_id": "immutable"}
    monkeypatch.setattr(probe, "native_https_policy", lambda *args: "policy")
    monkeypatch.setattr(probe.ssl, "create_default_context", lambda **kwargs: object())
    monkeypatch.setattr(probe, "retain_runtime", lambda *args: {})
    monkeypatch.setattr(probe, "runtime_continuity", lambda *args: fault != "runtime")
    monkeypatch.setattr(probe, "origin_get", lambda *args: list(events))
    monkeypatch.setattr(probe, "guest_request", lambda url, method: url)

    def control(host, path, *, port, context):
        return {"method": "GET", "path": path, "port": port, "host": f"{host}:{port}"}

    monkeypatch.setattr(probe, "control_https", control)

    async def recycle(fixture, retained, gateway, row):
        nonlocal applied
        assert retained is owner
        applied = source.read_bytes()
        if fault == "retain_removed":
            applied += b"old-root"
        cycles.append(applied)

    monkeypatch.setattr(probe, "recycle", recycle)

    async def execute(command, **kwargs):
        parsed = urlsplit(command[-1])
        root = b"old-root" if parsed.port == 8443 else b"new-root"
        trusted = root in (source.read_bytes() if fault == "hot_reload" else applied)
        if trusted:
            request = control(
                parsed.hostname, parsed.path + "?" + parsed.query, port=parsed.port, context=None
            )
            events.append(request)
            result = {"status": 200, "body": json.dumps(request)}
        else:
            events.append({"port": parsed.port, "handshake_error": "TLSV1_ALERT_UNKNOWN_CA"})
            result = {"error": "TimeoutError" if fault == "timeout" else "RemoteDisconnected"}
        return SimpleNamespace(exit_code=0, stderr_bytes=b"", stdout=json.dumps(result))

    async def acquire(**kwargs):
        return SimpleNamespace(owner=owner, exec=execute)

    async def dispose(retained):
        disposals.append(retained)

    fixture = SimpleNamespace(
        client=SimpleNamespace(image="image"),
        report={},
        save=lambda: None,
        acquire=acquire,
        dispose=dispose,
    )
    run = probe.measure_ca_lifecycle(fixture, "10.23.45.67", ca, source, object())
    if fault:
        with pytest.raises(RuntimeError, match="unexpected CA lifecycle observation"):
            asyncio.run(run)
        assert fixture.report["ca_lifecycle"]["qualification_passed"] is False
    else:
        asyncio.run(run)
        report = fixture.report["ca_lifecycle"]
        assert report["qualification_passed"] is True
        assert len(report["stages"]) == 5
        assert sum(len(row["cases"]) for row in report["stages"]) == 10
        assert cycles == [b"old-rootnew-root", b"new-root", b"old-root"]
    assert disposals == [owner]


@pytest.mark.parametrize("changed", [False, True])
def test_observed_runtimes_remain_owned_across_restarts(probe, monkeypatch, changed):
    owner = {
        "name": "owned",
        "sandbox_id": "immutable",
        "containers": ["old"],
        "processes": {"1": "old-start"},
    }
    fixture = SimpleNamespace(
        client=SimpleNamespace(current_id=lambda name: "other" if changed else "immutable"),
        save=lambda: None,
    )
    monkeypatch.setattr(
        probe,
        "snapshot",
        lambda identity: {"containers": [{"Id": "new"}], "processes": {"2": "new-start"}},
    )
    if changed:
        with pytest.raises(RuntimeError, match="identity changed"):
            probe.retain_runtime(fixture, owner)
        assert owner["containers"] == ["old"]
    else:
        probe.retain_runtime(fixture, owner)
        assert owner["containers"] == ["new", "old"]
        assert owner["processes"] == {"1": "old-start", "2": "new-start"}


@pytest.mark.parametrize("fault", [None, "exit", "identity", "uncertain"])
def test_recycle_orders_gateway_and_single_stop_start_without_replay(probe, monkeypatch, fault):
    calls = []
    phase = "ready"
    owner = {"name": "owned", "sandbox_id": "immutable"}
    monkeypatch.setattr(probe, "retain_runtime", lambda *args, **kwargs: None)
    monkeypatch.setattr(probe, "wait_gateway", lambda *args: calls.append("health"))

    def rpc(method, request):
        nonlocal phase
        if method != "GetSandbox":
            calls.append(method)
            if fault == "uncertain":
                raise TimeoutError("uncertain mutation")
            phase = "stopped" if method == "StopSandbox" else "ready"
        return SimpleNamespace(
            sandbox=SimpleNamespace(
                metadata=SimpleNamespace(id="other" if fault == "identity" else "immutable"),
                status=SimpleNamespace(phase=phase),
            ),
            SerializeToString=lambda: b"response",
        )

    client = SimpleNamespace(
        rpc=rpc,
        scope="workspace",
        pb=SimpleNamespace(
            SANDBOX_PHASE_STOPPED="stopped",
            SANDBOX_PHASE_READY="ready",
            StopSandboxRequest=lambda **kwargs: kwargs,
            StartSandboxRequest=lambda **kwargs: kwargs,
            GetSandboxRequest=lambda **kwargs: kwargs,
        ),
    )
    fixture = SimpleNamespace(client=client, save=lambda: None)
    gateway = SimpleNamespace(
        stop=lambda: calls.append("gateway_stop"),
        start=lambda: calls.append("gateway_start"),
        process=SimpleNamespace(returncode=1 if fault == "exit" else 0),
    )
    row = {}
    if fault:
        with pytest.raises((RuntimeError, TimeoutError)):
            asyncio.run(probe.recycle(fixture, owner, gateway, row))
        assert "StartSandbox" not in calls
        if fault == "uncertain":
            assert row["mutations"][0]["state"] == "pending"
    else:
        asyncio.run(probe.recycle(fixture, owner, gateway, row))
        assert calls == ["gateway_stop", "gateway_start", "health", "StopSandbox", "StartSandbox"]
        assert all(r["state"] == "observed" for r in row["mutations"])


def test_stopped_runtime_retains_container_without_running_process_query(probe, monkeypatch):
    owner = {
        "name": "owned",
        "sandbox_id": "immutable",
        "containers": ["old"],
        "processes": {"1": "start"},
    }
    fixture = SimpleNamespace(
        client=SimpleNamespace(current_id=lambda name: "immutable"), save=lambda: None
    )
    monkeypatch.setattr(
        probe, "snapshot", lambda identity: pytest.fail("stopped container has no Docker top")
    )
    monkeypatch.setattr(
        probe, "inventory", lambda identity: {"containers": [{"Id": "stopped"}], "volumes": []}
    )
    probe.retain_runtime(fixture, owner, processes=False)
    assert owner["containers"] == ["old", "stopped"]
    assert owner["processes"] == {"1": "start"}
