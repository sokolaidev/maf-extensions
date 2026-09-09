"""Cleanup retains engine identities across siblings, replacement, and retry attempts."""

import asyncio
import copy
from dataclasses import replace

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
    Selection,
)
from maf_sandbox._router import CallAdmission
from maf_sandbox.testing import FAKE_BACKEND_DECLARATIONS, InProcessSandbox, InProcessSandboxBackend

KEY = SandboxKey(scope="scope", thread_id="thread", agent_dir="agent")
SPEC = SandboxSpec(kind="work")


class _Engine(InProcessSandboxBackend):
    def __init__(self, name="engine", **kwargs):
        super().__init__(name=name, **kwargs)
        self.instances = {}
        self.failure: str | None = None
        self.attempts = []

    async def acquire(self, key, spec):
        at = (key, spec.kind, spec.image)
        if at not in self.instances:
            self.instances[at] = InProcessSandbox()
        return self.instances[at]

    async def dispose(self, key, *, kind=None, instance_id=None):
        self.attempts.append((kind, instance_id))
        if self.failure == "cancel":
            raise asyncio.CancelledError
        if self.failure == "timeout":
            await asyncio.Event().wait()
        if self.failure:
            return DisposalFailure("refused", "delete failed")
        for at, sandbox in list(self.instances.items()):
            if at[0] == key and (kind is None or kind == at[1]):
                if instance_id is None or instance_id == sandbox.instance_id:
                    del self.instances[at]
        return None


def _router(*backends, keep=False, **kwargs):
    return SandboxRouter(
        backends,
        min_isolation=Isolation.NONE,
        reclaim=ReclaimConfig(
            timeout=0.01,
            failed_reclaim_policy=FailedReclaimPolicy.KEEP if keep else FailedReclaimPolicy.DISPOSE,
        ),
        **kwargs,
    )


def test_adoption_and_routine_cleanup_preserve_same_kind_instances():
    engine = _Engine()
    router = _router(engine)

    async def scenario():
        first = await router.acquire(KEY, replace(SPEC, image="first"))
        other = await router.acquire(KEY, replace(SPEC, image="other"))
        sibling = await router.acquire(KEY, replace(SPEC, kind="sibling"))
        assert len(engine.instances) == 3
        engine.attempts.clear()
        assert await router.dispose_kind(KEY, SPEC.kind, instance_id=first.instance_id, timeout=1)
        assert engine.attempts == [(SPEC.kind, first.instance_id)]
        assert set(one.instance_id for one in engine.instances.values()) == {
            other.instance_id,
            sibling.instance_id,
        }
        assert not router._unclean and not router._pending_disposals
        assert await router.dispose_kind(KEY, SPEC.kind, timeout=1)
        assert list(engine.instances.values()) == [sibling]

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["reported", "timeout", "cancel"])
def test_retry_on_another_loop_preserves_replacement_and_sibling(failure):
    engine = _Engine()
    router = _router(engine)
    first = asyncio.run(router.acquire(KEY, SPEC))
    sibling = asyncio.run(router.acquire(KEY, replace(SPEC, image="sibling")))
    engine.failure = failure
    attempt = router.dispose_kind(KEY, SPEC.kind, instance_id=first.instance_id, timeout=0.01)
    if failure == "cancel":
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(attempt)
    else:
        assert not asyncio.run(attempt)
    pending = router._pending_for(KEY)
    assert len(pending) == 1 and pending[0].instance_id == first.instance_id
    replacement = InProcessSandbox()
    engine.instances[(KEY, SPEC.kind, SPEC.image)] = replacement
    engine.failure = None
    assert asyncio.run(router.dispose_unclean(KEY, timeout=1))
    assert set(one.instance_id for one in engine.instances.values()) == {
        replacement.instance_id,
        sibling.instance_id,
    }
    assert engine.attempts[-1] == (SPEC.kind, first.instance_id)


def test_multiple_serving_backends_remain_reachable_for_one_kind():
    first = _Engine(
        "first",
        declarations=replace(
            FAKE_BACKEND_DECLARATIONS,
            capabilities=frozenset({Capability.EXEC}),
        ),
    )
    second = _Engine("second")
    router = _router(first, second, selection=Selection.PER_SPEC)

    async def scenario():
        a = await router.acquire(KEY, replace(SPEC, requires=frozenset({Capability.EXEC})))
        b = await router.acquire(KEY, replace(SPEC, requires=frozenset({Capability.FILES_IN})))
        first.attempts.clear()
        second.attempts.clear()
        assert await router.dispose_kind(KEY, SPEC.kind, instance_id=a.instance_id, timeout=1)
        assert first.attempts == [(SPEC.kind, a.instance_id)]
        assert second.attempts == []
        assert b in second.instances.values()
        assert await router.dispose_kind(KEY, SPEC.kind, instance_id=b.instance_id, timeout=1)
        assert second.attempts == [(SPEC.kind, b.instance_id)]

    asyncio.run(scenario())


def test_finish_cleans_each_engine_instance_once_with_fresh_wrappers():
    engine = _Engine()
    router = _router(engine)

    async def scenario():
        await router.enter_call(KEY, SPEC, owner="call")
        a = await router.acquire(KEY, SPEC)
        b = await router.acquire(KEY, replace(SPEC, image="other"))
        engine.attempts.clear()
        assert (
            await router.finish_call(
                KEY,
                SPEC,
                admission=CallAdmission(engine, Cleanup.DISPOSE, served=True),
                sandbox=b,
                sandboxes=[a, copy.copy(a), b],
                owner="call",
            )
            is None
        )
        assert engine.attempts == [(SPEC.kind, a.instance_id), (SPEC.kind, b.instance_id)]
        assert not engine.instances and not router._unclean

    asyncio.run(scenario())


@pytest.mark.parametrize("same_instance", [False, True])
def test_landed_sweep_does_not_erase_failure_recorded_after_it_started(same_instance):
    engine = _Engine()
    router = _router(engine)

    async def scenario():
        a = await router.acquire(KEY, SPEC)
        b = await router.acquire(KEY, replace(SPEC, image="other"))
        router.mark_unclean(KEY, backend=engine, kind=SPEC.kind, instance_id=a.instance_id)
        entered, release = asyncio.Event(), asyncio.Event()

        async def dispose(key, *, kind=None, instance_id=None):
            entered.set()
            await release.wait()
            return None

        engine.dispose = dispose
        sweeping = asyncio.create_task(router.dispose(KEY))
        await entered.wait()
        newer = a if same_instance else b
        router.mark_unclean(KEY, backend=engine, kind=SPEC.kind, instance_id=newer.instance_id)
        release.set()
        await sweeping
        assert router._unclean_state(KEY)[0]
        assert [target.instance_id for target in router._pending_for(KEY)] == [newer.instance_id]

    asyncio.run(scenario())


def test_keep_preserves_access_after_instance_delete_failure():
    engine = _Engine()
    router = _router(engine, keep=True)

    async def scenario():
        sandbox = await router.acquire(KEY, SPEC)
        engine.failure = "reported"
        assert not await router.dispose_kind(
            KEY, SPEC.kind, instance_id=sandbox.instance_id, timeout=1
        )
        assert not router._unclean and not router._pending_disposals
        assert await router.acquire(KEY, SPEC) is sandbox

    asyncio.run(scenario())
