"""Monitoring preserves optional backend admission without adding it to ordinary backends."""

import asyncio
from contextlib import asynccontextmanager, nullcontext

import pytest
from maf_sandbox import BackendCallAdmission, SandboxKey, SandboxRouter, SandboxSpec, Selection
from maf_sandbox.testing import InProcessSandboxBackend

from maf_sandbox_tui import MonitoredSandboxBackend


@pytest.mark.parametrize("layers", [1, 2])
def test_monitored_backend_forwards_the_original_admission_context(layers):
    key, spec = SandboxKey("scope", "thread", "agent"), SandboxSpec(kind="test")
    authority = nullcontext()
    events = []

    @asynccontextmanager
    async def admission():
        events.append("enter")
        try:
            yield authority
        finally:
            events.append("exit")

    scope = admission()

    class Backend(InProcessSandboxBackend):
        def call_admission(self, held_key, held_spec, *, owner, timeout):
            assert (held_key, held_spec, owner, timeout) == (key, spec, "owner", 3)
            return scope

    backend = Backend()
    monitored = MonitoredSandboxBackend(backend)
    if layers == 2:
        monitored = MonitoredSandboxBackend(monitored)
    assert isinstance(monitored, BackendCallAdmission)
    forwarded = monitored.call_admission(key, spec, owner="owner", timeout=3)
    assert forwarded is scope

    async def check():
        async with forwarded as cleanup:
            assert cleanup is authority
            assert events == ["enter"]
        assert events == ["enter", "exit"]

    asyncio.run(check())


@pytest.mark.parametrize("selection", list(Selection))
def test_monitoring_an_ordinary_backend_preserves_shared_admission(selection):
    backend = InProcessSandboxBackend()
    monitored = MonitoredSandboxBackend(backend)
    assert not isinstance(monitored, BackendCallAdmission)
    router = SandboxRouter([monitored], min_isolation=backend.isolation, selection=selection)
    key, spec = SandboxKey("scope", "thread", "agent"), SandboxSpec(kind="test")

    async def check():
        await router.enter_call(key, spec, owner="first")
        await router.enter_call(key, spec, owner="second", timeout=0.02)
        await router.release_call(key, spec.kind, owner="first")
        await router.release_call(key, spec.kind, owner="second")

    asyncio.run(check())
