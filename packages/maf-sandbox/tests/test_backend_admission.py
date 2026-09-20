"""A backend's exclusive-call requirement cannot be relaxed by a workload."""

import asyncio
from contextlib import asynccontextmanager, contextmanager
from dataclasses import replace

import pytest

from maf_sandbox import Cleanup, SandboxKey, SandboxRouter, SandboxSpec, Selection
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


@pytest.mark.parametrize("selection", list(Selection))
def test_backend_hook_keeps_its_authority_until_cleanup_finishes(selection):
    async def check():
        active = set()
        events = []
        cleaning, finish_cleanup = asyncio.Event(), asyncio.Event()

        class Backend(InProcessSandboxBackend):
            @asynccontextmanager
            async def call_admission(self, key, spec, *, owner, timeout):
                @contextmanager
                def cleanup_authority():
                    assert owner in active
                    events.append((owner, "cleanup"))
                    yield

                active.add(owner)
                events.append((owner, "enter"))
                try:
                    yield cleanup_authority()
                finally:
                    active.remove(owner)
                    events.append((owner, "exit"))

            async def dispose(self, key, *, kind=None, instance_id=None):
                assert active == {"first"}
                assert events[-1] == ("first", "cleanup")
                cleaning.set()
                await finish_cleanup.wait()
                assert active == {"first"}
                events.append(("first", "disposed"))
                return await super().dispose(key, kind=kind, instance_id=instance_id)

        backend = Backend()
        router = SandboxRouter(
            [backend],
            min_isolation=backend.isolation,
            min_cleanup=Cleanup.DISPOSE,
            selection=selection,
        )
        key, spec = SandboxKey("scope", "thread", "agent"), SandboxSpec(kind="test")
        assert not backend.declarations.requires_exclusive_admission
        assert not spec.exclusive_admission
        admission = await router.enter_call(key, spec, owner="first")
        sandbox = await backend.acquire(key, spec)
        with pytest.raises(TimeoutError):
            await router.enter_call(key, spec, owner="second", timeout=0.02)
        finishing = asyncio.create_task(
            router.finish_call(key, spec, admission=admission, sandbox=sandbox, owner="first")
        )
        try:
            await asyncio.wait_for(cleaning.wait(), 1)
            with pytest.raises(TimeoutError):
                await router.enter_call(key, spec, owner="second", timeout=0.02)
        finally:
            finish_cleanup.set()
            assert await finishing is None
        assert events == [
            ("first", "enter"),
            ("first", "cleanup"),
            ("first", "disposed"),
            ("first", "exit"),
        ]
        await router.enter_call(key, spec, owner="second", timeout=1)
        await router.release_call(key, spec.kind, owner="second")
        assert not active

    asyncio.run(check())
