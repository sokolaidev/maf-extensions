"""Failed cleanup retries its targets while refusing the whole conversation key."""

import asyncio
import dataclasses

import pytest

from maf_sandbox import (
    Capability,
    Cleanup,
    DisposalFailure,
    FailedReclaimPolicy,
    Isolation,
    ReclaimConfig,
    SandboxKey,
    SandboxRouter,
    SandboxSpec,
    SandboxUnclean,
)
from maf_sandbox._router import CallAdmission
from maf_sandbox.testing import (
    FAKE_BACKEND_DECLARATIONS,
    InProcessSandbox,
    InProcessSandboxBackend,
)

_KEY = SandboxKey(scope="scope", thread_id="thread", agent_dir="agent")


class _ResetFails(InProcessSandbox):
    async def reset(self, *, timeout):
        raise RuntimeError("reset refused")


class _Backend(InProcessSandboxBackend):
    def __init__(self, name, failure=None):
        super().__init__(
            _ResetFails(),
            name=name,
            sandbox_per_key=True,
            declarations=dataclasses.replace(
                FAKE_BACKEND_DECLARATIONS,
                capabilities=FAKE_BACKEND_DECLARATIONS.capabilities | {Capability.SNAPSHOT},
            ),
        )
        self.failure = failure
        self.attempts = []

    async def dispose(self, key, *, kind=None):
        self.attempts.append(kind)
        if self.failure == "cancel":
            raise asyncio.CancelledError
        if self.failure == "timeout":
            await asyncio.Event().wait()
        if self.failure == "raise":
            raise RuntimeError("delete refused")
        if self.failure == "failure":
            return DisposalFailure("refused", "delete refused")
        return await super().dispose(key, kind=kind)


async def _finish(router, backend, kind, rung=Cleanup.DISPOSE):
    spec = SandboxSpec(kind=kind, min_cleanup=rung)
    sandbox = await backend.acquire(_KEY, spec)
    await router._slots.take(_KEY, kind, owner=kind, exclusive=True, timeout=1)
    return await router.finish_call(
        _KEY,
        spec,
        admission=CallAdmission(backend, rung, served=True),
        sandbox=sandbox,
        owner=kind,
        timeout=0.01,
    )


@pytest.mark.parametrize("failure", ["failure", "raise", "timeout", "cancel"])
@pytest.mark.parametrize("rung", [Cleanup.DISPOSE, Cleanup.RESET])
def test_retry_preserves_sibling_kinds_and_backends(failure, rung):
    first, other = _Backend("first", failure), _Backend("other")
    router = SandboxRouter([first, other], min_isolation=Isolation.NONE)

    async def scenario():
        # The reset-failing sandbox serves the cleanup target first.
        await first.acquire(_KEY, SandboxSpec(kind="dirty"))
        sibling = await first.acquire(_KEY, SandboxSpec(kind="sibling"))
        elsewhere = await other.acquire(_KEY, SandboxSpec(kind="dirty"))
        if failure == "cancel":
            with pytest.raises(asyncio.CancelledError):
                await _finish(router, first, "dirty", rung)
        else:
            assert await _finish(router, first, "dirty", rung)
        with pytest.raises(SandboxUnclean):
            await router.acquire(_KEY, SandboxSpec(kind="sibling"))
        first.failure = None
        assert await router.dispose_unclean(_KEY, timeout=1)
        assert first.attempts == ["dirty", "dirty"]
        assert not other.attempts
        assert first.sandboxes[(_KEY, "sibling")] is sibling
        assert other.sandboxes[(_KEY, "dirty")] is elsewhere
        assert (_KEY, "dirty") not in first.sandboxes
        assert _KEY not in router._unclean

    asyncio.run(scenario())


def test_key_stays_refused_until_every_pending_backend_and_kind_lands():
    first, second = _Backend("first", "failure"), _Backend("second", "failure")
    router = SandboxRouter([first, second], min_isolation=Isolation.NONE)

    async def scenario():
        assert await _finish(router, first, "a")
        assert await _finish(router, first, "b")
        assert await _finish(router, second, "a")
        first.failure = None
        assert not await router.dispose_unclean(_KEY, timeout=1)
        with pytest.raises(SandboxUnclean):
            await router.acquire(_KEY, SandboxSpec(kind="clean"))
        assert first.attempts == ["a", "b", "a", "b"]
        second.failure = None
        assert await router.dispose_unclean(_KEY, timeout=1)
        assert first.attempts == ["a", "b", "a", "b"]
        assert second.attempts == ["a", "a", "a"]
        assert _KEY not in router._unclean

    asyncio.run(scenario())


@pytest.mark.parametrize("later_kind", ["a", "b"])
def test_successful_retry_does_not_clear_a_newer_cleanup_failure(later_kind):
    entered, release = asyncio.Event(), asyncio.Event()

    class _Overlapping(_Backend):
        calls = 0

        async def dispose(self, key, *, kind=None):
            self.calls += 1
            if self.calls == 2:
                entered.set()
                await release.wait()
                return None
            return DisposalFailure("refused", "delete refused")

    backend = _Overlapping("first")
    router = SandboxRouter([backend], min_isolation=Isolation.NONE)

    async def scenario():
        assert await _finish(router, backend, "a")
        retry = asyncio.create_task(router.dispose_unclean(_KEY, timeout=1))
        await entered.wait()
        assert await _finish(router, backend, later_kind)
        release.set()
        assert not await retry
        with pytest.raises(SandboxUnclean):
            await router.acquire(_KEY, SandboxSpec(kind=later_kind))

    asyncio.run(scenario())


@pytest.mark.parametrize("rung", [Cleanup.DISPOSE, Cleanup.RESET])
@pytest.mark.parametrize("failure", ["failure", "cancel"])
def test_keep_does_not_refuse_after_strong_cleanup_fails(rung, failure):
    backend = _Backend("first", failure)
    router = SandboxRouter(
        [backend],
        min_isolation=Isolation.NONE,
        reclaim=ReclaimConfig(failed_reclaim_policy=FailedReclaimPolicy.KEEP),
    )

    async def scenario():
        if failure == "cancel":
            with pytest.raises(asyncio.CancelledError):
                await _finish(router, backend, "dirty", rung)
        else:
            assert await _finish(router, backend, "dirty", rung)
        assert _KEY not in router._unclean
        assert await router.acquire(_KEY, SandboxSpec(kind="dirty")) is backend.sandbox

    asyncio.run(scenario())
