"""Failed cleanup retries its targets while refusing the whole conversation key."""

import asyncio
import dataclasses
import threading
from concurrent.futures import ThreadPoolExecutor

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
    ScopePurge,
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
    router._seen[(_KEY, kind, id(backend))] = {sandbox.instance_id}
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


@pytest.mark.parametrize("refusal", ["mark", "reclaim"])
@pytest.mark.parametrize("failure", [None, "failure", "cancel"])
def test_refused_create_cleans_only_its_kind_and_retains_failed_targets(
    refusal, failure, monkeypatch
):
    entered, release = asyncio.Event(), asyncio.Event()

    class _Late(_Backend):
        async def acquire(self, key, spec):
            sandbox = await super().acquire(key, spec)
            if spec.kind == "late":
                entered.set()
                await release.wait()
                if refusal == "reclaim":
                    monkeypatch.setattr(sandbox, "reclaim", None)
            return sandbox

    backend = _Late("first", failure)
    router = SandboxRouter([backend], min_isolation=Isolation.NONE)

    async def scenario():
        for kind in ("dirty", "sibling"):
            await backend.acquire(_KEY, SandboxSpec(kind=kind))
        creating = asyncio.create_task(router.acquire(_KEY, SandboxSpec(kind="late")))
        await entered.wait()
        if refusal == "mark":
            router.mark_unclean(_KEY, backend=backend, kind="dirty")
        release.set()
        expected = SandboxUnclean if refusal == "mark" else TypeError
        with pytest.raises(asyncio.CancelledError if failure == "cancel" else expected):
            await creating
        assert backend.attempts == ["late"]
        assert (_KEY, "dirty") in backend.sandboxes
        assert (_KEY, "sibling") in backend.sandboxes
        assert ((_KEY, "late") in backend.sandboxes) is bool(failure)
        if failure or refusal == "mark":
            backend.failure = None
            assert await router.dispose_unclean(_KEY, timeout=1)
            assert (_KEY, "late") not in backend.sandboxes
            assert (_KEY, "sibling") in backend.sandboxes
            assert backend.attempts == [
                "late",
                *(["dirty"] if refusal == "mark" else []),
                *(["late"] if failure else []),
            ]

    asyncio.run(scenario())


@pytest.mark.parametrize("reason", [None, DisposalFailure("refused", "delete refused")])
def test_acquire_reads_refusal_atomically_while_another_loop_clears_it(reason, monkeypatch):
    backend = _Backend("first")
    router = SandboxRouter([backend], min_isolation=Isolation.NONE)
    start, progressed = threading.Event(), threading.Event()

    class _Guard:
        def __init__(self):
            self.lock = threading.Lock()

        def __enter__(self):
            if not self.lock.acquire(blocking=False):
                progressed.set()
                self.lock.acquire()

        def __exit__(self, *args):
            self.lock.release()

    class _Ledger(dict):
        armed = True

        def __contains__(self, key):
            present = super().__contains__(key)
            if self.armed:
                self.armed = False
                start.set()
                assert progressed.wait(2)
            return present

    router.mark_unclean(_KEY, reason)
    monkeypatch.setattr(router, "_unclean_guard", _Guard())
    router._unclean = _Ledger(router._unclean)

    def clear_from_another_loop():
        assert start.wait(2)
        try:
            asyncio.run(router.dispose(_KEY))
        finally:
            progressed.set()

    with ThreadPoolExecutor(max_workers=1) as pool:
        cleared = pool.submit(clear_from_another_loop)
        with pytest.raises(SandboxUnclean) as refusal:
            asyncio.run(router.acquire(_KEY, SandboxSpec(kind="test")))
        assert refusal.value.code == (None if reason is None else reason.code)
        cleared.result(timeout=2)
    assert _KEY not in router._unclean
    acquired = asyncio.run(router.acquire(_KEY, SandboxSpec(kind="test")))
    assert acquired is backend.sandboxes[(_KEY, "test")]


@pytest.mark.parametrize(
    ("failure", "failed_first"),
    [
        ("failure", False),
        ("failure", True),
        ("raise", False),
        ("raise", True),
        ("cancel", False),
        ("timeout", False),
    ],
)
def test_partial_scope_purge_retries_only_the_backend_that_failed(failure, failed_first):
    class _PurgeBackend(_Backend):
        purge_failure = None

        async def dispose_scope(self, scope, thread_id):
            if self.purge_failure == "cancel":
                raise asyncio.CancelledError
            if self.purge_failure == "timeout":
                await asyncio.Event().wait()
            if self.purge_failure == "raise":
                raise RuntimeError("delete refused")
            if self.purge_failure == "failure":
                return ScopePurge(0, DisposalFailure("refused", "delete refused"))
            return await super().dispose_scope(scope, thread_id)

    good, bad = _PurgeBackend("good"), _PurgeBackend("bad")
    bad.purge_failure = failure
    router = SandboxRouter(
        [bad, good] if failed_first else [good, bad], min_isolation=Isolation.NONE
    )

    async def scenario():
        for backend in (good, bad):
            for kind in ("a", "b"):
                await backend.acquire(_KEY, SandboxSpec(kind=kind))
                router.mark_unclean(_KEY, backend=backend, kind=kind)
        if failure in ("cancel", "timeout"):
            with pytest.raises(asyncio.CancelledError if failure == "cancel" else TimeoutError):
                await asyncio.wait_for(router.dispose_scope(_KEY.scope, _KEY.thread_id), 0.01)
        else:
            assert (await router.dispose_scope(_KEY.scope, _KEY.thread_id)).undisposed is not None
        replacement = await good.acquire(_KEY, SandboxSpec(kind="a"))
        with pytest.raises(SandboxUnclean):
            await router.acquire(_KEY, SandboxSpec(kind="a"))
        assert await router.dispose_unclean(_KEY, timeout=1)
        assert not good.attempts
        assert bad.attempts == ["a", "b"]
        assert good.sandboxes[(_KEY, "a")] is replacement
        assert _KEY not in router._unclean

    asyncio.run(scenario())


def test_scope_purge_preserves_a_newer_target_on_a_successful_backend():
    entered, release = asyncio.Event(), asyncio.Event()

    class _PurgeBackend(_Backend):
        async def dispose_scope(self, scope, thread_id):
            entered.set()
            await release.wait()
            return await super().dispose_scope(scope, thread_id)

    backend = _PurgeBackend("first")
    router = SandboxRouter([backend], min_isolation=Isolation.NONE)

    async def scenario():
        router.mark_unclean(_KEY, backend=backend, kind="a")
        purging = asyncio.create_task(router.dispose_scope(_KEY.scope, _KEY.thread_id))
        await entered.wait()
        router.mark_unclean(_KEY, backend=backend, kind="a")
        release.set()
        assert (await purging).undisposed is None
        with pytest.raises(SandboxUnclean):
            await router.acquire(_KEY, SandboxSpec(kind="a"))
        assert await router.dispose_unclean(_KEY, timeout=1)
        assert backend.attempts == ["a"]

    asyncio.run(scenario())
