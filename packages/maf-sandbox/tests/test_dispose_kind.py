"""Host disposal narrows the delete while preserving unrelated sandboxes and refusals."""

import asyncio
import dataclasses
import math

import pytest

from maf_sandbox import (
    DisposalFailure,
    Isolation,
    SandboxDisposed,
    SandboxKey,
    SandboxObserver,
    SandboxRouter,
    SandboxSpec,
    SandboxUnclean,
    Selection,
)
from maf_sandbox.testing import InProcessSandboxBackend

_KEY = SandboxKey(scope="scope", thread_id="thread", agent_dir="agent")
_SPEC = SandboxSpec(kind="target")


class _Recorder(SandboxObserver):
    def __init__(self):
        self.events: list[SandboxDisposed] = []

    def sandbox_disposed(self, event: SandboxDisposed) -> None:
        self.events.append(event)


@pytest.mark.parametrize("selection", [Selection.FIXED, Selection.PER_SPEC])
@pytest.mark.parametrize("call_id", [None, "call"])
def test_disposes_only_the_named_kind_on_every_registered_backend(selection, call_id):
    first = InProcessSandboxBackend(name="first", sandbox_per_key=True)
    second = InProcessSandboxBackend(name="second", sandbox_per_key=True)
    router = SandboxRouter([first, second], min_isolation=Isolation.NONE, selection=selection)
    key = dataclasses.replace(_KEY, call_id=call_id)
    other_key = dataclasses.replace(key, thread_id="other")
    sibling_spec = SandboxSpec(kind="sibling")

    async def scenario():
        for backend in (first, second):
            await backend.acquire(key, _SPEC)
            await backend.acquire(key, sibling_spec)
            await backend.acquire(other_key, _SPEC)
        siblings = [backend.sandboxes[(key, "sibling")] for backend in (first, second)]
        elsewhere = [backend.sandboxes[(other_key, "target")] for backend in (first, second)]
        assert await router.dispose_kind(key, "target", timeout=1)
        for backend, sibling, other in zip((first, second), siblings, elsewhere, strict=True):
            assert (key, "target") not in backend.sandboxes
            assert backend.sandboxes[(key, "sibling")] is sibling
            assert backend.sandboxes[(other_key, "target")] is other
            assert backend.disposed_kinds == ["target"]

    asyncio.run(scenario())


def test_absent_kind_and_empty_router_are_successful():
    backend = InProcessSandboxBackend(sandbox_per_key=True)
    for backends in ([], [backend]):
        router = SandboxRouter(backends, min_isolation=Isolation.NONE)
        assert asyncio.run(router.dispose_kind(_KEY, "missing", timeout=1))


@pytest.mark.parametrize("failure", ["returned", "raised"])
def test_failure_is_reported_and_does_not_stop_the_sweep_or_refuse_the_key(failure, caplog):
    bad = InProcessSandboxBackend(
        name="bad",
        dispose_failure=DisposalFailure("refused", "still present")
        if failure == "returned"
        else None,
        dispose_error=RuntimeError("delete broke") if failure == "raised" else None,
    )
    good = InProcessSandboxBackend(name="good")
    recorder = _Recorder()
    router = SandboxRouter([bad, good], min_isolation=Isolation.NONE, observer=recorder)
    assert asyncio.run(router.dispose_kind(_KEY, "target", timeout=1)) is False
    assert bad.disposed_kinds == good.disposed_kinds == ["target"]
    failed, succeeded = recorder.events
    assert failed.backend == "bad"
    assert failed.failure is not None
    assert failed.failure.code == ("refused" if failure == "returned" else "unknown")
    assert succeeded.backend == "good"
    assert (succeeded.outcome, succeeded.failure) == ("gone", None)
    assert (
        "still present" in caplog.text if failure == "returned" else "delete broke" in caplog.text
    )
    assert asyncio.run(router.acquire(_KEY, _SPEC)) is bad.sandbox


@pytest.mark.parametrize("timeout", [0, -1, math.inf, -math.inf, math.nan])
def test_invalid_timeout_does_not_delete(timeout):
    backend = InProcessSandboxBackend()
    router = SandboxRouter([backend], min_isolation=Isolation.NONE)
    with pytest.raises(ValueError, match="finite positive"):
        asyncio.run(router.dispose_kind(_KEY, "target", timeout=timeout))
    assert backend.disposed == []


@pytest.mark.parametrize("kind", [None, 1])
def test_invalid_kind_cannot_become_a_whole_key_sweep(kind):
    backend = InProcessSandboxBackend()
    router = SandboxRouter([backend], min_isolation=Isolation.NONE)
    with pytest.raises(TypeError, match="kind must be a string"):
        asyncio.run(router.dispose_kind(_KEY, kind, timeout=1))
    assert backend.disposed == []


@pytest.mark.parametrize("cancel", [False, True])
def test_interrupted_delete_is_observed_and_releases_the_lock(cancel, caplog):
    entered = asyncio.Event()
    release = asyncio.Event()

    class _Blocking(InProcessSandboxBackend):
        async def dispose(self, key, *, kind=None):
            entered.set()
            await release.wait()
            return await super().dispose(key, kind=kind)

    backend = _Blocking()
    recorder = _Recorder()
    router = SandboxRouter([backend], min_isolation=Isolation.NONE, observer=recorder)

    async def scenario():
        disposing = asyncio.create_task(
            router.dispose_kind(_KEY, "target", timeout=5 if cancel else 0.05)
        )
        await entered.wait()
        if cancel:
            disposing.cancel()
            with pytest.raises(asyncio.CancelledError):
                await disposing
        else:
            assert await disposing is False
            assert "did not finish within" in caplog.text
        assert len(recorder.events) == 1
        assert recorder.events[0].failure is not None
        assert recorder.events[0].outcome != "gone"
        assert await router.acquire(_KEY, _SPEC) is backend.sandbox
        release.set()
        assert await router.dispose_kind(_KEY, "target", timeout=1)

    asyncio.run(scenario())


@pytest.mark.parametrize("other", ["dispose", "dispose_unclean", "dispose_kind"])
@pytest.mark.parametrize("kind_first", [False, True])
def test_disposals_share_the_per_key_lock_and_the_wait_is_bounded(other, kind_first):
    entered = asyncio.Event()
    release = asyncio.Event()
    attempts = []

    class _Blocking(InProcessSandboxBackend):
        async def dispose(self, key, *, kind=None):
            attempts.append((key, kind))
            if key == _KEY and len(attempts) == 1:
                entered.set()
                await release.wait()
            return None

    router = SandboxRouter([_Blocking()], min_isolation=Isolation.NONE)

    async def other_disposal():
        if other == "dispose":
            return await router.dispose(_KEY)
        if other == "dispose_unclean":
            return await router.dispose_unclean(_KEY, timeout=1)
        return await router.dispose_kind(_KEY, "sibling", timeout=1)

    async def scenario():
        first = asyncio.create_task(
            router.dispose_kind(_KEY, "target", timeout=1) if kind_first else other_disposal()
        )
        await entered.wait()
        if kind_first:
            second = asyncio.create_task(other_disposal())
            await asyncio.sleep(0)
            assert len(attempts) == 1
            assert not second.done()
        else:
            assert await router.dispose_kind(_KEY, "target", timeout=0.05) is False
            assert len(attempts) == 1
            second = None
        other_key = dataclasses.replace(_KEY, thread_id="other")
        assert await router.dispose_kind(other_key, "target", timeout=1)
        release.set()
        await first
        if second is not None:
            await second

    asyncio.run(scenario())


@pytest.mark.parametrize("pending_kind", ["sibling", None])
def test_success_retires_only_the_named_kind_and_keeps_other_targets_refused(pending_kind):
    backend = InProcessSandboxBackend()
    router = SandboxRouter([backend], min_isolation=Isolation.NONE)
    router.mark_unclean(_KEY, backend=backend, kind="target")
    router.mark_unclean(_KEY, backend=backend, kind=pending_kind)
    assert asyncio.run(router.dispose_kind(_KEY, "target", timeout=1))
    with pytest.raises(SandboxUnclean):
        asyncio.run(router.acquire(_KEY, _SPEC))
    assert asyncio.run(router.dispose_unclean(_KEY, timeout=1))
    assert backend.disposed_kinds == ["target", pending_kind]
    assert asyncio.run(router.acquire(_KEY, _SPEC)) is backend.sandbox


def test_success_reopens_a_key_when_all_its_pending_targets_are_for_this_kind():
    backends = [InProcessSandboxBackend(name=name) for name in ("first", "second")]
    router = SandboxRouter(backends, min_isolation=Isolation.NONE)
    router.mark_unclean(_KEY, kind="target")
    assert asyncio.run(router.dispose_kind(_KEY, "target", timeout=1))
    assert asyncio.run(router.acquire(_KEY, _SPEC)) is backends[0].sandbox


def test_failed_sweep_retains_pending_targets():
    backend = InProcessSandboxBackend(dispose_failure=DisposalFailure("refused", "still there"))
    router = SandboxRouter([backend], min_isolation=Isolation.NONE)
    router.mark_unclean(_KEY, kind="target")
    assert asyncio.run(router.dispose_kind(_KEY, "target", timeout=1)) is False
    with pytest.raises(SandboxUnclean):
        asyncio.run(router.acquire(_KEY, _SPEC))
    backend.dispose_failure = None
    assert asyncio.run(router.dispose_unclean(_KEY, timeout=1))
    assert backend.disposed_kinds == ["target", "target"]


def test_success_does_not_clear_a_newer_mark_for_the_same_kind():
    class _MarkedDuringDelete(InProcessSandboxBackend):
        async def dispose(self, key, *, kind=None):
            router.mark_unclean(key, backend=self, kind=kind)
            return None

    backend = _MarkedDuringDelete()
    router = SandboxRouter([backend], min_isolation=Isolation.NONE)
    router.mark_unclean(_KEY, backend=backend, kind="target")
    assert asyncio.run(router.dispose_kind(_KEY, "target", timeout=1))
    with pytest.raises(SandboxUnclean):
        asyncio.run(router.acquire(_KEY, _SPEC))
