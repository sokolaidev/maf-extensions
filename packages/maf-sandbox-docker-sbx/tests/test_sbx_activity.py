"""Held sessions use real local subprocesses to check readiness, loss and reaping."""

from __future__ import annotations

import asyncio
import sys
from types import SimpleNamespace

import pytest
from maf_sandbox.run_activity import SandboxRunActivityLost

from maf_sandbox_docker_sbx import SbxSandboxBackend, SbxSandboxConfig
from maf_sandbox_docker_sbx import _backend as implementation


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
    sandbox = SimpleNamespace(name="test", instance_id="instance", base="/work", mount=None)
    return backend, sandbox, events


def test_release_reaps_client_after_guest_cleanup(tmp_path, monkeypatch):
    backend, sandbox, events = _backend(
        tmp_path, monkeypatch, "import time; print(READY, flush=True); time.sleep(60)"
    )

    async def scenario():
        async with backend.hold_activity(sandbox, timeout=5) as activity:
            activity.check()
            assert not events
        assert activity.process.returncode is not None
        assert events == ["end"]

    asyncio.run(scenario())


@pytest.mark.parametrize("body", ["print('wrong', flush=True)", "pass"])
def test_bad_receipt_retires_and_reaps(tmp_path, monkeypatch, body):
    backend, sandbox, events = _backend(tmp_path, monkeypatch, body)

    async def scenario():
        with pytest.raises((SandboxRunActivityLost, asyncio.IncompleteReadError)):
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
