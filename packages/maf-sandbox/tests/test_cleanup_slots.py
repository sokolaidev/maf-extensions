"""Call-owned holds permit shared reclaim and exclude incompatible callers across loops."""

from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from maf_sandbox import Cleanup, SandboxKey
from maf_sandbox._cleanup import ExclusiveSlots, needs_exclusive_use

_KEY = SandboxKey(scope="scope-a", thread_id="thread-1", agent_dir="agent-1")
_KIND = "test"
#: Callers are told apart by an opaque owner — a call id in the framework, a name here.
OWNER = "call-1"
RIVAL = "call-2"


def _run(coro):
    return asyncio.run(coro)


class TestWhichRungsExclude:
    def test_reclaim_shares_and_everything_above_it_excludes(self):
        assert needs_exclusive_use(Cleanup.RECLAIM) is False
        assert needs_exclusive_use(Cleanup.RESET) is True
        assert needs_exclusive_use(Cleanup.DISPOSE) is True


class TestAHoldBelongsToTheCallThatTookIt:
    """A waiter that never got in must not give away what the holder has."""

    def test_a_cancelled_waiter_does_not_release_the_holder(self):
        slots = ExclusiveSlots()

        async def scenario() -> bool:
            holding = asyncio.Event()
            let_go = asyncio.Event()

            async def holder() -> None:
                await slots.take(_KEY, _KIND, owner=OWNER, exclusive=True, timeout=5)
                holding.set()
                await let_go.wait()
                slots.release(_KEY, _KIND, owner=OWNER)

            async def waiter() -> None:
                try:
                    await slots.take(_KEY, _KIND, owner=RIVAL, exclusive=True, timeout=5)
                finally:
                    slots.release(_KEY, _KIND, owner=RIVAL)

            first = asyncio.create_task(holder())
            await holding.wait()
            second = asyncio.create_task(waiter())
            await asyncio.sleep(0)
            second.cancel()
            with pytest.raises(asyncio.CancelledError):
                await second
            # A third call must still find the sandbox taken: the holder has not finished.
            try:
                await slots.take(_KEY, _KIND, owner="call-3", exclusive=True, timeout=0.05)
            except TimeoutError:
                got_in = False
            else:
                got_in = True
                slots.release(_KEY, _KIND, owner="call-3")
            let_go.set()
            await first
            return got_in

        assert _run(scenario()) is False, (
            "a third call entered a sandbox whose holder was still running in it — the "
            "cancelled waiter released a hold it never took"
        )

    def test_a_timed_out_waiter_does_not_release_the_holder(self):
        slots = ExclusiveSlots()

        async def scenario() -> bool:
            holding = asyncio.Event()
            let_go = asyncio.Event()

            async def holder() -> None:
                await slots.take(_KEY, _KIND, owner=OWNER, exclusive=True, timeout=5)
                holding.set()
                await let_go.wait()
                slots.release(_KEY, _KIND, owner=OWNER)

            first = asyncio.create_task(holder())
            await holding.wait()
            with pytest.raises(TimeoutError):
                await slots.take(_KEY, _KIND, owner=RIVAL, exclusive=True, timeout=0.05)
            # The refusal's own cleanup runs the same release a holder's does.
            slots.release(_KEY, _KIND, owner=RIVAL)
            try:
                await slots.take(_KEY, _KIND, owner="call-3", exclusive=True, timeout=0.05)
            except TimeoutError:
                got_in = False
            else:
                got_in = True
                slots.release(_KEY, _KIND, owner="call-3")
            let_go.set()
            await first
            return got_in

        assert _run(scenario()) is False

    def test_releasing_without_holding_is_not_an_error(self):
        slots = ExclusiveSlots()

        async def scenario() -> None:
            slots.release(_KEY, _KIND, owner=OWNER)

        _run(scenario())


class TestSharedAndExclusiveHolds:
    def test_two_reclaim_calls_share_the_sandbox(self):
        slots = ExclusiveSlots()

        async def scenario() -> bool:
            both = asyncio.Barrier(2)

            async def one(owner: str) -> None:
                await slots.take(_KEY, _KIND, owner=owner, exclusive=False, timeout=5)
                await both.wait()
                slots.release(_KEY, _KIND, owner=owner)

            # If a shared hold excluded its sibling the barrier would never clear.
            await asyncio.wait_for(asyncio.gather(one(OWNER), one(RIVAL)), timeout=5)
            return True

        assert _run(scenario()) is True

    def test_an_exclusive_hold_waits_for_a_reclaim_sibling(self):
        """An exclusive hold waits until every shared owner leaves."""
        slots = ExclusiveSlots()

        async def scenario() -> bool:
            await slots.take(_KEY, _KIND, owner=OWNER, exclusive=False, timeout=5)
            try:
                await slots.take(_KEY, _KIND, owner=RIVAL, exclusive=True, timeout=0.05)
            except TimeoutError:
                return False
            return True

        assert _run(scenario()) is False, (
            "a disposal entered a sandbox a reclaim-rung sibling was still using"
        )

    def test_a_reclaim_call_waits_for_an_exclusive_holder(self):
        """And the other arrival order, which is unsafe for the same reason."""
        slots = ExclusiveSlots()

        async def scenario() -> bool:
            await slots.take(_KEY, _KIND, owner=OWNER, exclusive=True, timeout=5)
            try:
                await slots.take(_KEY, _KIND, owner=RIVAL, exclusive=False, timeout=0.05)
            except TimeoutError:
                return False
            return True

        assert _run(scenario()) is False

    def test_a_released_hold_lets_the_next_call_in(self):
        slots = ExclusiveSlots()

        async def scenario() -> bool:
            await slots.take(_KEY, _KIND, owner=OWNER, exclusive=True, timeout=5)
            slots.release(_KEY, _KIND, owner=OWNER)
            await slots.take(_KEY, _KIND, owner=OWNER, exclusive=True, timeout=0.5)
            held = slots.holds(_KEY, _KIND, owner=OWNER)
            slots.release(_KEY, _KIND, owner=OWNER)
            return held

        assert _run(scenario()) is True

    def test_a_waiter_is_woken_rather_than_left_to_time_out(self):
        slots = ExclusiveSlots()

        async def scenario() -> float:
            await slots.take(_KEY, _KIND, owner=OWNER, exclusive=True, timeout=5)

            async def let_go_shortly() -> None:
                await asyncio.sleep(0.05)
                # A different task, and the same owner: the hold belongs to the call, so a task
                # the body spawned can give it back and the release is not a stranger's.
                slots.release(_KEY, _KIND, owner=OWNER)

            asyncio.create_task(let_go_shortly())
            started = asyncio.get_running_loop().time()
            # A generous bound: this passes by being woken, not by waiting it out.
            await slots.take(_KEY, _KIND, owner=RIVAL, exclusive=True, timeout=10)
            waited = asyncio.get_running_loop().time() - started
            slots.release(_KEY, _KIND, owner=RIVAL)
            return waited

        assert _run(scenario()) < 5

    def test_a_different_kind_is_a_different_sandbox(self):
        slots = ExclusiveSlots()

        async def scenario() -> bool:
            await slots.take(_KEY, "kind-a", owner=OWNER, exclusive=True, timeout=5)
            await slots.take(_KEY, "kind-b", owner=OWNER, exclusive=True, timeout=0.5)
            return slots.holds(_KEY, "kind-b", owner=OWNER)

        assert _run(scenario()) is True


class TestHoldsAcrossEventLoops:
    @pytest.mark.parametrize(
        "first_exclusive,second_exclusive", [(True, True), (True, False), (False, True)]
    )
    def test_incompatible_call_waits_and_is_woken_on_its_own_loop(
        self, first_exclusive, second_exclusive
    ):
        slots = ExclusiveSlots()
        started = threading.Event()
        _run(slots.take(_KEY, _KIND, owner=OWNER, exclusive=first_exclusive, timeout=1))

        async def other_loop():
            started.set()
            await slots.take(_KEY, _KIND, owner=RIVAL, exclusive=second_exclusive, timeout=2)
            slots.release(_KEY, _KIND, owner=RIVAL)

        with ThreadPoolExecutor(max_workers=1) as pool:
            waiting = pool.submit(lambda: _run(other_loop()))
            try:
                assert started.wait(1)
                with pytest.raises(TimeoutError):
                    waiting.result(timeout=0.05)
            finally:
                slots.release(_KEY, _KIND, owner=OWNER)
            waiting.result(timeout=1)
        assert not slots._slots

    def test_shared_calls_on_different_loops_can_overlap(self):
        slots = ExclusiveSlots()
        _run(slots.take(_KEY, _KIND, owner=OWNER, exclusive=False, timeout=1))
        _run(slots.take(_KEY, _KIND, owner=RIVAL, exclusive=False, timeout=1))
        assert slots.holds(_KEY, _KIND, owner=OWNER)
        assert slots.holds(_KEY, _KIND, owner=RIVAL)
        slots.release(_KEY, _KIND, owner=OWNER)
        slots.release(_KEY, _KIND, owner=RIVAL)
        assert not slots._slots


class TestQueuedOwnersDoNotGetBypassed:
    def test_shared_arrivals_wait_behind_a_queued_exclusive_owner(self):
        slots = ExclusiveSlots()

        async def scenario():
            await slots.take(_KEY, _KIND, owner=OWNER, exclusive=False, timeout=1)
            exclusive = asyncio.create_task(
                slots.take(_KEY, _KIND, owner=RIVAL, exclusive=True, timeout=1)
            )
            await asyncio.sleep(0)
            shared = asyncio.create_task(
                slots.take(_KEY, _KIND, owner="later", exclusive=False, timeout=1)
            )
            await asyncio.sleep(0)
            assert not shared.done()
            slots.release(_KEY, _KIND, owner=OWNER)
            # The queue still applies before a notified waiter resumes.
            with pytest.raises(TimeoutError):
                await slots.take(_KEY, _KIND, owner="newcomer", exclusive=False, timeout=0.01)
            await exclusive
            assert not shared.done()
            slots.release(_KEY, _KIND, owner=RIVAL)
            await shared
            slots.release(_KEY, _KIND, owner="later")

        _run(scenario())
        assert not slots._slots

    @pytest.mark.parametrize("cancel", [False, True])
    def test_abandoned_exclusive_waiter_unblocks_shared_arrivals(self, cancel):
        slots = ExclusiveSlots()

        async def scenario():
            await slots.take(_KEY, _KIND, owner=OWNER, exclusive=False, timeout=1)
            exclusive = asyncio.create_task(
                slots.take(_KEY, _KIND, owner=RIVAL, exclusive=True, timeout=0.03)
            )
            await asyncio.sleep(0)
            shared = asyncio.create_task(
                slots.take(_KEY, _KIND, owner="later", exclusive=False, timeout=1)
            )
            await asyncio.sleep(0)
            assert not shared.done()
            if cancel:
                exclusive.cancel()
            with pytest.raises(asyncio.CancelledError if cancel else TimeoutError):
                await exclusive
            await shared
            assert slots.holds(_KEY, _KIND, owner=OWNER)
            slots.release(_KEY, _KIND, owner=OWNER)
            slots.release(_KEY, _KIND, owner="later")

        _run(scenario())
        assert not slots._slots
