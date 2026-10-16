"""Acceptance evidence requires active rendering, service denial and complete recovery."""

from __future__ import annotations

import asyncio
import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from maf_sandbox import ExecResult, SandboxKey, SandboxSpec, ScopePurge

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import check_drawio_acceptance as acceptance  # noqa: E402


@pytest.mark.parametrize("family", acceptance.FONTS)
def test_font_fixture_preserves_pages_icons_and_all_variants(family):
    document = ET.fromstring(acceptance.font_diagram(family))
    assert len(document) == 2
    for page in document:
        cells = page.findall(".//mxCell[@vertex='1']")
        assert all(f"fontFamily={family};" in cell.get("style", "") for cell in cells)
        variants = [cell for cell in cells if cell.get("id", "").startswith("font-")]
        assert len(variants) == 4
        for style, cell in enumerate(variants):
            assert f"fontStyle={style};" in cell.get("style", "")
            assert "Résumé Ω Ж" in cell.get("value", "")
        assert any("Key_Vaults.svg" in cell.get("style", "") for cell in cells)


@pytest.mark.parametrize(
    "status,denial,exit_code",
    [
        (403, "policy", 0),
        (403, None, 0),
        (200, None, 0),
        (403, "policy", 1),
    ],
)
def test_acas_requires_service_denial_before_exposing_sandbox(status, denial, exit_code):
    sandbox = SimpleNamespace(
        instance_id="test-instance",
        exec=AsyncMock(
            return_value=ExecResult(
                stdout=json.dumps({"status": status, "denial": denial}),
                stderr="",
                exit_code=exit_code,
            )
        ),
    )
    backend = SimpleNamespace(acquire=AsyncMock(return_value=sandbox), dispose=AsyncMock())
    observed = acceptance.ObservedBackend(backend, "acas")
    key = SandboxKey("test-scope", "test-thread", "drawio", call_id="test-call")
    spec = SandboxSpec(kind="drawio")
    if (status, denial, exit_code) == (403, "policy", 0):
        result = asyncio.run(observed.acquire(key, spec))
        assert isinstance(result, acceptance.ObservedSandbox)
        assert result.sandbox is sandbox
        assert observed.observations == [{"closedEgress": "service-policy-denial"}]
        backend.dispose.assert_not_awaited()
    else:
        with pytest.raises(ValueError):
            asyncio.run(observed.acquire(key, spec))
        backend.dispose.assert_awaited_once_with(key, kind="drawio", instance_id="test-instance")
        assert observed.acquired.empty() and not observed.observations


def test_fault_injection_waits_for_renderer_execution():
    async def check():
        queue = asyncio.Queue()
        sandbox = SimpleNamespace(exec=AsyncMock(return_value=ExecResult("", "", 0)))
        observed = acceptance.ObservedSandbox(sandbox, queue)
        await observed.exec(["python3", "-c", "pass"], working_directory="/tmp", timeout=1)
        assert queue.empty()
        await observed.exec(
            ["python3", "-I", "renderer.py", "--timeout", "120"],
            working_directory="/tmp",
            timeout=120,
        )
        assert queue.get_nowait() is sandbox

    asyncio.run(check())


@pytest.mark.parametrize(
    "fault", [None, "not-active", "no-recovery", "repeat-removes", "artifacts"]
)
def test_owner_death_requires_active_renderer_and_fresh_backend_recovery(
    tmp_path, monkeypatch, fault
):
    destination = tmp_path / "owner"
    backend = SimpleNamespace(
        dispose_scope=AsyncMock(
            side_effect=[
                ScopePurge(disposed=0 if fault == "no-recovery" else 1),
                ScopePurge(disposed=int(fault == "repeat-removes")),
                ScopePurge(),
            ]
        )
    )
    monkeypatch.setattr(acceptance, "backend_for", AsyncMock(return_value=backend))
    monkeypatch.setattr(acceptance, "owned", lambda scope: [])
    process = SimpleNamespace(returncode=None)

    def kill():
        process.returncode = -9

    process.kill = kill
    process.wait = AsyncMock(return_value=-9)

    async def spawn(*args, **kwargs):
        assert "--worker-scope" in args
        (destination / "ready.json").write_text(
            json.dumps(
                {
                    "stopped": fault != "not-active",
                    "renderer_pid": 123,
                }
            ),
            encoding="utf-8",
        )
        if fault == "artifacts":
            (destination / "interrupted").mkdir()
        return process

    monkeypatch.setattr(acceptance.asyncio, "create_subprocess_exec", spawn)
    if fault:
        with pytest.raises(ValueError):
            asyncio.run(acceptance.owner_death("docker", "image", destination))
    else:
        report = asyncio.run(acceptance.owner_death("docker", "image", destination))
        assert report["recoveredSandboxes"] == 1
        assert report["automaticCleanup"] is False
        assert report["repeatedPurgeEmpty"] is True
    assert process.returncode == -9
    assert all(
        call.args[0].startswith("drawio-death-") for call in backend.dispose_scope.call_args_list
    )


@pytest.mark.parametrize("fault", [None, "bounded-cleanup", "missing-event", "remaining"])
def test_cancellation_retains_call_end_failure_separately_from_recovery(
    tmp_path, monkeypatch, fault
):
    backend = SimpleNamespace(
        isolation=acceptance.Isolation.CONTAINER,
        dispose_scope=AsyncMock(
            side_effect=[
                ScopePurge(disposed=int(fault == "bounded-cleanup")),
                ScopePurge(disposed=int(fault == "remaining")),
                ScopePurge(),
            ]
        ),
    )
    observed = SimpleNamespace(running=asyncio.Queue(), observations=[])
    sandbox = SimpleNamespace(
        exec=AsyncMock(
            return_value=ExecResult(
                stdout=json.dumps({"renderer_pid": 42, "stopped": True}),
                stderr="",
                exit_code=0,
            )
        )
    )
    recorder = None

    def router(backends, *, min_isolation, observer):
        nonlocal recorder
        recorder = observer
        return object()

    async def convert(**kwargs):
        observed.running.put_nowait(sandbox)
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            if fault != "missing-event":
                assert recorder is not None
                recorder.tool_call_ended(
                    SimpleNamespace(
                        failure="CancelledError",
                        unclean=int(fault == "bounded-cleanup"),
                    )
                )
            raise

    monkeypatch.setattr(acceptance, "FONTS", ())
    monkeypatch.setattr(acceptance, "backend_for", AsyncMock(return_value=backend))
    monkeypatch.setattr(acceptance, "ObservedBackend", lambda *_: observed)
    monkeypatch.setattr(acceptance, "SandboxRouter", router)
    monkeypatch.setattr(
        acceptance, "make_drawio_tools", lambda *a, **kw: [SimpleNamespace(func=convert)]
    )
    monkeypatch.setattr(acceptance, "owned", lambda scope: [])
    if fault in {"missing-event", "remaining"}:
        with pytest.raises(ValueError):
            asyncio.run(acceptance.exercise("docker", "image", tmp_path, "scope"))
    else:
        report = asyncio.run(acceptance.exercise("docker", "image", tmp_path, "scope"))
        assert report["cancellation"]["uncleanAtCallEnd"] == int(fault == "bounded-cleanup")
        assert report["cancellation"]["recoveredSandboxes"] == int(fault == "bounded-cleanup")
        assert report["cancellation"]["artifactsDelivered"] is False
        assert report["cancellation"]["repeatedPurgeEmpty"] is True
