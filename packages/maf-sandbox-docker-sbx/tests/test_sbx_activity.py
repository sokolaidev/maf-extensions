"""Held sessions use real local subprocesses to check readiness, loss and reaping."""

from __future__ import annotations

import asyncio
import sys
from contextlib import asynccontextmanager

import pytest
from maf_sandbox.run_activity import SandboxRunActivityLost

from maf_sandbox_docker_sbx import SbxSandboxBackend, SbxSandboxConfig
from maf_sandbox_docker_sbx import _backend as implementation
from maf_sandbox_docker_sbx._egress import EgressPlan
from maf_sandbox_docker_sbx._plane import WorkspacePlane


def _backend(tmp_path, monkeypatch, body):
    backend = SbxSandboxBackend(SbxSandboxConfig(sbx_path=sys.executable, workspace_root=tmp_path))
    events = []

    def args(*args, nonce, **kwargs):
        return (
            "-u",
            "-c",
            "import sys; sys.stdout.reconfigure(newline=chr(10)); "
            + body.replace("READY", repr(nonce)),
        )

    async def end(name, instance, pid_file):
        events.append("end")

    async def retire(name, instance, reason):
        events.append("retire")

    monkeypatch.setattr(implementation, "_exec_args", args)
    monkeypatch.setattr(backend, "_end_the_command", end)
    monkeypatch.setattr(backend, "_retire", retire)
    sandbox = implementation._SbxSandbox(
        backend,
        "test",
        "instance",
        "/work",
        WorkspacePlane(tmp_path, "/work"),
        implementation._Mount("/host/ws", "/work", tmp_path),
        implementation._Allowlist((), EgressPlan((), (), frozenset())),
    )
    return backend, sandbox, events


@pytest.mark.parametrize("phase", ["allowlist", "process-start"])
def test_protocol_timeout_bounds_all_acquisition(tmp_path, monkeypatch, phase):
    backend, sandbox, events = _backend(tmp_path, monkeypatch, "pass")
    entered = []

    async def stalled(*args, **kwargs):
        entered.append(phase)
        await asyncio.Future()

    async def allowed(*args):
        pass

    monkeypatch.setattr(backend, "check_allowlist", stalled if phase == "allowlist" else allowed)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", stalled)

    async def acquire():
        async with sandbox.run_activity(timeout=0.05):
            pytest.fail("not ready")

    async def scenario():
        task = asyncio.create_task(acquire())
        try:
            done, _ = await asyncio.wait({task}, timeout=1)
            assert done, "acquisition exceeded its timeout"
            with pytest.raises(TimeoutError):
                await task
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        assert entered == [phase]
        assert events == []

    asyncio.run(scenario())


def test_protocol_body_and_release_outlast_acquisition_timeout(tmp_path, monkeypatch):
    backend, sandbox, events = _backend(tmp_path, monkeypatch, "pass")

    async def allowed(*args):
        pass

    @asynccontextmanager
    async def held(*args, **kwargs):
        try:
            yield object()
        finally:
            await asyncio.sleep(0.1)
            events.append("released")

    monkeypatch.setattr(backend, "check_allowlist", allowed)
    monkeypatch.setattr(backend, "hold_activity", held)

    async def scenario():
        async with sandbox.run_activity(timeout=0.05):
            await asyncio.sleep(0.1)
            events.append("body")
        assert events == ["body", "released"]

    asyncio.run(scenario())


def test_release_reaps_client_after_guest_cleanup(tmp_path, monkeypatch):
    backend, sandbox, events = _backend(
        tmp_path, monkeypatch, "import time; print(READY, flush=True); time.sleep(60)"
    )

    async def scenario():
        async with backend.hold_activity(sandbox, timeout=5) as activity:
            assert isinstance(activity, implementation._HeldActivity)
            activity.check()
            assert not events
        assert isinstance(activity, implementation._HeldActivity)
        assert activity.process.returncode is not None
        assert events == ["end"]

    asyncio.run(scenario())


@pytest.mark.parametrize("body", ["print('wrong', flush=True)", "pass"])
def test_bad_receipt_retires_and_reaps(tmp_path, monkeypatch, body):
    backend, sandbox, events = _backend(tmp_path, monkeypatch, body)

    async def scenario():
        with pytest.raises(SandboxRunActivityLost):
            async with backend.hold_activity(sandbox, timeout=5):
                pytest.fail("not ready")
        assert events == ["end", "retire"]

    asyncio.run(scenario())


def test_startup_timeout_retires(tmp_path, monkeypatch):
    backend, sandbox, events = _backend(tmp_path, monkeypatch, "import time; time.sleep(60)")

    async def scenario():
        with pytest.raises(TimeoutError):
            async with backend.hold_activity(sandbox, timeout=0.05):
                pytest.fail("not ready")
        assert events == ["end", "retire"]

    asyncio.run(scenario())


def test_unexpected_exit_is_loss_even_with_zero_status(tmp_path, monkeypatch):
    backend, sandbox, events = _backend(tmp_path, monkeypatch, "print(READY, flush=True)")

    async def scenario():
        with pytest.raises(SandboxRunActivityLost):
            async with backend.hold_activity(sandbox, timeout=5) as activity:
                assert isinstance(activity, implementation._HeldActivity)
                await activity.reader
                activity.check()
        assert events == ["end", "retire"]

    asyncio.run(scenario())


def test_cancellation_during_body_still_reaps(tmp_path, monkeypatch):
    backend, sandbox, events = _backend(
        tmp_path, monkeypatch, "import time; print(READY, flush=True); time.sleep(60)"
    )

    async def scenario():
        ready = asyncio.Event()
        activities = []

        async def held():
            async with backend.hold_activity(sandbox, timeout=5) as activity:
                activities.append(activity)
                ready.set()
                await asyncio.Future()

        task = asyncio.create_task(held())
        await asyncio.wait_for(ready.wait(), timeout=5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert activities[0].process.returncode is not None
        assert events == ["end"]

    asyncio.run(scenario())


def test_excess_output_ends_and_retires_the_session(tmp_path, monkeypatch):
    backend, sandbox, events = _backend(
        tmp_path,
        monkeypatch,
        "import time; print(READY, flush=True); print('x' * 5000, flush=True); time.sleep(60)",
    )

    async def scenario():
        with pytest.raises(SandboxRunActivityLost):
            async with backend.hold_activity(sandbox, timeout=5) as activity:
                assert isinstance(activity, implementation._HeldActivity)
                await asyncio.wait({activity.reader}, timeout=5)
                activity.check()
        assert events == ["end", "retire"]

    asyncio.run(scenario())
