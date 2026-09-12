"""The last active call cleans folded instance records before admission reopens."""

import asyncio
import copy
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from maf_sandbox import (
    Cleanup,
    DisposalFailure,
    FailedReclaimPolicy,
    Isolation,
    ReclaimConfig,
    SandboxKey,
    SandboxRouter,
    SandboxSpec,
)
from maf_sandbox._cleanup import _Waiter
from maf_sandbox._router import CallAdmission
from maf_sandbox.testing import InProcessSandbox, InProcessSandboxBackend

KEY = SandboxKey(scope="s", thread_id="t", agent_id="a")
SPEC = SandboxSpec(kind="test", confined_to_guest_call_path=True)


def _router(*, policy=FailedReclaimPolicy.DISPOSE):
    backend = InProcessSandboxBackend()
    return SandboxRouter(
        [backend], min_isolation=Isolation.NONE, reclaim=ReclaimConfig(failed_reclaim_policy=policy)
    ), backend


def _queue(router, backend, sandbox, owner, rung=Cleanup.DISPOSE):
    return router.queue_cleanup(
        KEY,
        SPEC,
        admission=CallAdmission(backend, rung),
        sandbox=sandbox,
        owner=owner,
        rung=rung,
        timeout=1,
    )


@pytest.mark.parametrize(
    "rungs",
    [
        (Cleanup.RESET, Cleanup.DISPOSE),
        (Cleanup.DISPOSE, Cleanup.RESET),
        (Cleanup.RESET, Cleanup.RESET),
    ],
)
def test_same_instance_folds_strongest_rung_and_cleans_once(rungs, monkeypatch):
    router, backend = _router()
    sandbox = InProcessSandbox()
    ran = []

    async def clean(key, spec, serving, rung, held, unclean, bound):
        assert router._slots._slots[(KEY, SPEC.kind)].state == "cleaning"
        ran.append((serving, rung, held.instance_id))

    monkeypatch.setattr(router, "_run_the_rung", clean)

    async def scenario():
        for owner in ("first", "second"):
            await router.enter_call(KEY, SPEC, owner=owner)
        first = _queue(router, backend, sandbox, "first", rungs[0])
        await router.release_call(KEY, SPEC.kind, owner="first")
        assert not ran
        second = _queue(router, backend, copy.copy(sandbox), "second", rungs[1])
        assert first is second
        await router.release_call(KEY, SPEC.kind, owner="second")
        assert await router.wait_cleanup(first) is None
        assert not router._slots._slots

    asyncio.run(scenario())
    expected = Cleanup.DISPOSE if Cleanup.DISPOSE in rungs else Cleanup.RESET
    assert ran == [(backend, expected, sandbox.instance_id)]


@pytest.mark.parametrize("same_backend", [False, True])
def test_all_instance_records_finish_before_admission_reopens(same_backend, monkeypatch):
    router, backend = _router()
    other = backend if same_backend else InProcessSandboxBackend(name="other")
    a, b = InProcessSandbox(), InProcessSandbox()
    if not same_backend:
        b = copy.copy(a)
    started, proceed = asyncio.Event(), asyncio.Event()
    ran = []

    async def clean(key, spec, serving, rung, sandbox, unclean, bound):
        ran.append((serving, sandbox.instance_id))
        started.set()
        await proceed.wait()

    monkeypatch.setattr(router, "_run_the_rung", clean)

    async def scenario():
        for owner in ("first", "last"):
            await router.enter_call(KEY, SPEC, owner=owner)
        _queue(router, backend, a, "first")
        _queue(router, other, b, "first")
        await router.release_call(KEY, SPEC.kind, owner="first")
        cleaning = asyncio.create_task(router.release_call(KEY, SPEC.kind, owner="last"))
        await started.wait()
        assert router._slots._slots[(KEY, SPEC.kind)].state == "cleaning"
        with pytest.raises(TimeoutError):
            await router.enter_call(KEY, SPEC, owner="late", timeout=0.01)
        proceed.set()
        await cleaning
        await router.enter_call(KEY, SPEC, owner="late")
        await router.release_call(KEY, SPEC.kind, owner="late")
        assert not router._slots._slots

    asyncio.run(asyncio.wait_for(scenario(), 3))
    assert ran == [(backend, a.instance_id), (other, b.instance_id)]


@pytest.mark.parametrize("cancel", [False, True])
def test_abandoned_completion_waiter_cannot_drop_pending_cleanup(cancel, monkeypatch):
    router, backend = _router()
    sandbox = InProcessSandbox()
    ran = []

    async def clean(*args):
        ran.append(True)

    monkeypatch.setattr(router, "_run_the_rung", clean)

    async def scenario():
        await router.enter_call(KEY, SPEC, owner="first")
        await router.enter_call(KEY, SPEC, owner="last")
        record = _queue(router, backend, sandbox, "first")
        await router.release_call(KEY, SPEC.kind, owner="first")
        waiter = asyncio.create_task(router._slots.wait(record, timeout=0.01 if not cancel else 1))
        await asyncio.sleep(0)
        if cancel:
            waiter.cancel()
        with pytest.raises(asyncio.CancelledError if cancel else TimeoutError):
            await waiter
        assert not record.done and not record.waiters
        assert router._slots._slots[(KEY, SPEC.kind)].state == "draining"
        await router.release_call(KEY, SPEC.kind, owner="last")
        assert record.done and not router._slots._slots

    asyncio.run(scenario())
    assert ran == [True]


@pytest.mark.parametrize("policy", list(FailedReclaimPolicy))
def test_cancelled_cleaner_transfers_all_unfinished_records_before_waking(policy, monkeypatch):
    router, backend = _router(policy=policy)
    sandboxes = [InProcessSandbox(), InProcessSandbox()]
    started = asyncio.Event()

    async def clean(*args):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(router, "_run_the_rung", clean)

    async def scenario():
        await router.enter_call(KEY, SPEC, owner="call")
        records = [_queue(router, backend, sandbox, "call") for sandbox in sandboxes]
        cleaning = asyncio.create_task(router.release_call(KEY, SPEC.kind, owner="call"))
        await started.wait()
        cleaning.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cleaning
        assert all(record.done and record.failure for record in records)
        assert not router._slots._slots
        pending = router._pending_for(KEY)
        assert {one.instance_id for one in pending} == (
            {s.instance_id for s in sandboxes} if policy is FailedReclaimPolicy.DISPOSE else set()
        )

    asyncio.run(scenario())


def test_drain_and_completion_waiters_are_notified_on_their_own_loops(monkeypatch):
    router, backend = _router()
    sandbox = InProcessSandbox()
    waiting = threading.Event()
    ran = []

    async def clean(*args):
        ran.append(threading.get_ident())

    monkeypatch.setattr(router, "_run_the_rung", clean)

    async def prepare():
        await router.enter_call(KEY, SPEC, owner="first")
        await router.enter_call(KEY, SPEC, owner="last")
        record = _queue(router, backend, sandbox, "first")
        await router.release_call(KEY, SPEC.kind, owner="first")
        return record

    record = asyncio.run(prepare())

    async def waiter():
        waiting.set()
        assert await router.wait_cleanup(record) is None
        await router.enter_call(KEY, SPEC, owner="new", timeout=1)
        await router.release_call(KEY, SPEC.kind, owner="new")

    with ThreadPoolExecutor(max_workers=1) as pool:
        done = pool.submit(lambda: asyncio.run(waiter()))
        assert waiting.wait(1)
        asyncio.run(router.release_call(KEY, SPEC.kind, owner="last"))
        done.result(timeout=2)
    assert ran == [threading.get_ident()]
    assert not router._slots._slots


def test_closed_completion_loop_cannot_strand_live_waiters(monkeypatch):
    router, backend = _router()
    abandoned = asyncio.new_event_loop()

    async def clean(*args):
        pass

    monkeypatch.setattr(router, "_run_the_rung", clean)

    async def scenario():
        await router.enter_call(KEY, SPEC, owner="call")
        record = _queue(router, backend, InProcessSandbox(), "call")
        record.waiters.append(_Waiter(abandoned, abandoned.create_future(), False))
        abandoned.close()
        waiter = asyncio.create_task(router.wait_cleanup(record))
        await asyncio.sleep(0)
        await router.release_call(KEY, SPEC.kind, owner="call")
        assert await waiter is None
        assert not router._slots._slots

    asyncio.run(scenario())


def test_simultaneous_last_exits_from_eight_threads_claim_one_cleanup(monkeypatch):
    router, backend = _router()
    sandbox = InProcessSandbox()
    entered = threading.Barrier(8)
    ran = []

    async def clean(key, spec, serving, rung, held, unclean, bound):
        ran.append(rung)
        await asyncio.sleep(0.01)

    monkeypatch.setattr(router, "_run_the_rung", clean)

    async def call(index):
        owner = str(index)
        await router.enter_call(KEY, SPEC, owner=owner)
        entered.wait(timeout=3)
        return await router.finish_call(
            KEY,
            SPEC,
            admission=CallAdmission(backend, Cleanup.RESET if index else Cleanup.DISPOSE),
            sandbox=copy.copy(sandbox),
            owner=owner,
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda index: asyncio.run(call(index)), range(8)))
    assert results == [None] * 8
    assert ran == [Cleanup.DISPOSE]
    assert not router._slots._slots


def test_failed_instance_does_not_skip_another_and_survives_entry_eviction(monkeypatch):
    router, backend = _router()
    a, b = InProcessSandbox(), InProcessSandbox()
    attempts = []

    async def dispose(key, *, kind=None, instance_id=None):
        attempts.append(instance_id)
        if instance_id == a.instance_id:
            return DisposalFailure("refused", "delete refused")
        return None

    monkeypatch.setattr(backend, "dispose", dispose)

    async def scenario():
        await router.enter_call(KEY, SPEC, owner="call")
        first = _queue(router, backend, a, "call")
        second = _queue(router, backend, b, "call")
        await router.release_call(KEY, SPEC.kind, owner="call")
        assert await router.wait_cleanup(first)
        assert await router.wait_cleanup(second) is None
        assert not router._slots._slots
        assert [one.instance_id for one in router._pending_for(KEY)] == [a.instance_id]

    asyncio.run(scenario())
    assert attempts == [a.instance_id, b.instance_id]


def test_a_waiter_is_renewed_as_each_cleanup_target_lands(monkeypatch):
    """One call can hold several instances, and their cleanups run one after another. A waiter
    budgeting a single target would be refused while its predecessor is still inside a later
    target's own bounds."""
    router, backend = _router()
    first, second = InProcessSandbox(), InProcessSandbox()
    assert first.instance_id != second.instance_id
    stage = 0.3
    landed = []

    async def clean(key, spec, serving, rung, held, unclean, bound):
        await asyncio.sleep(stage)
        landed.append(held.instance_id)
        return None

    monkeypatch.setattr(router, "_run_the_rung", clean)

    async def scenario():
        await router.enter_call(KEY, SPEC, owner="holder")
        _queue(router, backend, first, "holder")
        _queue(router, backend, second, "holder")
        # Enough for one target and its escalation, short of two targets end to end.
        waiting = asyncio.create_task(router.enter_call(KEY, SPEC, owner="waiter", timeout=0.45))
        await asyncio.sleep(0)
        await router.release_call(KEY, SPEC.kind, owner="holder")
        await waiting
        await router.release_call(KEY, SPEC.kind, owner="waiter")

    asyncio.run(asyncio.wait_for(scenario(), timeout=10))
    assert landed == [first.instance_id, second.instance_id]
    assert not router._slots._slots
