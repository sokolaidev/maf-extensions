"""Who may be inside a sandbox at once, and who is allowed to give it back.

Every test here reproduces a defect review found in the first cut of the ladder. The shapes are
worth stating once, because each one passes under the bug for a different reason:

- A pair-keyed release let a *waiter* hand back the holder's sandbox, so a third call walked
  into a sandbox someone was still running in.
- Deciding from the arriving spec alone meant a `RECLAIM` caller never registered at all, so an
  exclusive holder could not see it and deleted the sandbox underneath it.
"""

from __future__ import annotations

import asyncio

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
                # A second call queues behind the holder and is cancelled before it ever gets
                # in. Its cleanup path is the holder's, so a release keyed on the pair rather
                # than on the caller frees the holder's sandbox from here.
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
        """The mixed case: one spec resolves to RECLAIM and another to DISPOSE over one sandbox.

        A caller that skipped the slot for being on RECLAIM was invisible here, and the
        exclusive holder went straight in and deleted the sandbox underneath it.
        """
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
