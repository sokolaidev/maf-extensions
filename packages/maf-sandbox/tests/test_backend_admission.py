"""A backend's exclusive-call requirement cannot be relaxed by a workload."""

import asyncio
from dataclasses import replace

import pytest

from maf_sandbox import SandboxKey, SandboxRouter, SandboxSpec, Selection
from maf_sandbox.testing import FAKE_BACKEND_DECLARATIONS, InProcessSandboxBackend


@pytest.mark.parametrize("selection", list(Selection))
def test_backend_requires_exclusivity_without_a_spec_request(selection):
    backend = InProcessSandboxBackend(
        declarations=replace(FAKE_BACKEND_DECLARATIONS, requires_exclusive_admission=True)
    )
    router = SandboxRouter([backend], min_isolation=backend.isolation, selection=selection)
    key = SandboxKey("scope", "thread", "agent")
    spec = SandboxSpec(kind="test")
    assert not spec.exclusive_admission

    async def check():
        await router.enter_call(key, spec, owner="first")
        with pytest.raises(TimeoutError):
            await router.enter_call(key, spec, owner="second", timeout=0.02)
        await router.release_call(key, spec.kind, owner="first")
        await router.enter_call(key, spec, owner="second", timeout=1)
        await router.release_call(key, spec.kind, owner="second")

    asyncio.run(check())
