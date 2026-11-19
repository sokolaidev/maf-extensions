"""A pass's synchronous steps run off the host's event loop, so the host keeps answering.

A pass over a long conversation spends seconds of CPU between its awaits. On the event loop that
time froze every other task the host serves. These tests pin the helper that moves a step to a
worker thread, and that a composed pass leaves the loop free while it works.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
import time

import pytest
from agent_framework._compaction import CharacterEstimatorTokenizer
from test_record_budget import _history, _strategy, _Summarizer

from maf_compaction._offload import off_loop, steps_inline
from maf_compaction._summary_input import async_bounded_prefix_extent

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    # The helper is built on asyncio.to_thread; the package runs on asyncio hosts.
    return "asyncio"


async def test_a_step_runs_on_another_thread() -> None:
    loop_thread = threading.get_ident()
    assert await off_loop(threading.get_ident) != loop_thread


async def test_steps_inline_keeps_a_step_on_the_calling_thread() -> None:
    loop_thread = threading.get_ident()
    with steps_inline():
        assert await off_loop(threading.get_ident) == loop_thread
    with steps_inline(False):
        assert await off_loop(threading.get_ident) != loop_thread


async def test_a_step_returns_its_value_and_raises_its_error() -> None:
    assert await off_loop(sum, [1, 2, 3]) == 6

    def fails() -> None:
        raise LookupError("step failed")

    try:
        await off_loop(fails)
    except LookupError as error:
        assert str(error) == "step failed"
    else:
        raise AssertionError("the step's error was lost")


async def test_cancelling_the_caller_waits_for_the_step_to_finish() -> None:
    started = threading.Event()
    finished = threading.Event()

    def step() -> None:
        started.set()
        time.sleep(0.2)
        finished.set()

    caller = asyncio.create_task(off_loop(step))
    await asyncio.to_thread(started.wait)
    caller.cancel()
    try:
        await caller
    except asyncio.CancelledError:
        # The cancellation arrives only once the step can no longer touch the conversation.
        assert finished.is_set()
    else:
        raise AssertionError("the cancellation was swallowed")


async def _tick_until(stop: asyncio.Event) -> list[float]:
    """Wake every 5 ms until ``stop``; return how late each wake-up was."""
    holds: list[float] = []
    last = time.perf_counter()
    while not stop.is_set():
        await asyncio.sleep(0.005)
        now = time.perf_counter()
        holds.append(now - last - 0.005)
        last = now
    return holds


class _SlowTokenizer(CharacterEstimatorTokenizer):
    """Counts like the estimator, slowly: a stand-in for a long conversation's CPU."""

    def count_tokens(self, text: str) -> int:
        time.sleep(0.002)
        return super().count_tokens(text)


async def _longest_hold_during_pass(*, inline: bool) -> tuple[float, float]:
    """Run one composed pass beside a 5 ms ticker; return the pass time and the longest hold."""
    strategy = _strategy(_Summarizer(), budget=200, tokenizer=_SlowTokenizer())
    messages = _history(40)
    stop = asyncio.Event()
    ticker = asyncio.create_task(_tick_until(stop))
    await asyncio.sleep(0.01)
    started = time.perf_counter()
    with steps_inline(inline), contextlib.suppress(ValueError):
        # A budget this small may be refused; the pass's work is what is measured.
        await strategy(messages)
    elapsed = time.perf_counter() - started
    stop.set()
    holds = await ticker
    return elapsed, max(holds)


async def test_a_composed_pass_leaves_the_loop_free_while_it_works() -> None:
    # Control: the same pass with every step kept on the loop does freeze it, so the setup
    # below can show a hold at all.
    elapsed, longest = await _longest_hold_during_pass(inline=True)
    assert elapsed > 0.3
    assert longest > elapsed / 2

    elapsed, longest = await _longest_hold_during_pass(inline=False)
    assert elapsed > 0.3
    assert longest < 0.1


async def test_a_second_cancellation_still_waits_for_the_step() -> None:
    """A request's cancel followed by a shutdown's must not let the caller unwind mid-step."""
    started = threading.Event()
    finished = threading.Event()

    def step() -> None:
        started.set()
        time.sleep(0.3)
        finished.set()

    caller = asyncio.create_task(off_loop(step))
    await asyncio.to_thread(started.wait)
    caller.cancel()
    await asyncio.sleep(0.05)
    caller.cancel()
    try:
        await caller
    except asyncio.CancelledError:
        assert finished.is_set()
    else:
        raise AssertionError("the cancellation was swallowed")


async def _longest_hold_during_prefix_search(*, inline: bool) -> tuple[float, float]:
    """Grow a fitting batch with a slow renderer beside a 5 ms ticker, as a foreground pass does."""

    def render(items: list[int]) -> str:
        time.sleep(0.03)
        return " ".join(str(item) for item in items)

    stop = asyncio.Event()
    ticker = asyncio.create_task(_tick_until(stop))
    await asyncio.sleep(0.01)
    started = time.perf_counter()
    with steps_inline(inline):
        batch, _ = await async_bounded_prefix_extent(
            list(range(64)),
            render=render,
            prompt="Merge these.",
            tokenizer=CharacterEstimatorTokenizer(),
            limit=100_000,
            min_items=2,
            cooperative=False,
        )
    elapsed = time.perf_counter() - started
    stop.set()
    holds = await ticker
    assert len(batch) == 64
    return elapsed, max(holds)


async def test_growing_a_fitting_batch_leaves_the_loop_free() -> None:
    # Control: kept on the loop, the probes of a batch that keeps fitting freeze it.
    elapsed, longest = await _longest_hold_during_prefix_search(inline=True)
    assert elapsed > 0.15
    assert longest > elapsed / 2

    elapsed, longest = await _longest_hold_during_prefix_search(inline=False)
    assert elapsed > 0.15
    assert longest < 0.1


class _LengthCostTokenizer(CharacterEstimatorTokenizer):
    """Counts like the estimator, at a cost that grows with the text, as a real tokenizer's does.

    A per-call delay hides the user half's band checks, which tokenize a few long transcripts
    rather than many messages.
    """

    def count_tokens(self, text: str) -> int:
        time.sleep(len(text) * 2e-6)
        return super().count_tokens(text)


async def _longest_hold_during_user_pass(*, inline: bool) -> tuple[float, float]:
    """Run the user-turn strategy over a user-heavy history beside a 5 ms ticker."""
    from test_usersummary import _COMPACTING_CEILING, _conversation
    from test_usersummary import _Summarizer as _UserSummarizer

    from maf_compaction import UserTurnAnchoredSummarizationCompactionStrategy

    strategy = UserTurnAnchoredSummarizationCompactionStrategy(
        tokenizer=_LengthCostTokenizer(),
        client=_UserSummarizer(),
        max_input_tokens=_COMPACTING_CEILING,
        max_summary_input_tokens=100_000,
    )
    messages = _conversation(12)
    stop = asyncio.Event()
    ticker = asyncio.create_task(_tick_until(stop))
    await asyncio.sleep(0.01)
    started = time.perf_counter()
    with steps_inline(inline):
        changed = await strategy(messages)
    elapsed = time.perf_counter() - started
    stop.set()
    holds = await ticker
    assert changed
    return elapsed, max(holds)


async def test_a_user_heavy_pass_leaves_the_loop_free_while_it_works() -> None:
    # Control: the band selection and size checks kept on the loop do freeze it.
    elapsed, longest = await _longest_hold_during_user_pass(inline=True)
    assert elapsed > 0.1
    assert longest > elapsed / 2

    elapsed, longest = await _longest_hold_during_user_pass(inline=False)
    assert elapsed > 0.1
    assert longest < 0.1


def test_shutdown_cancelling_every_task_still_waits_for_the_step() -> None:
    """``asyncio.run`` cancels the step's task directly at shutdown, not only its caller: the
    caller must still see the step finish before its cancellation surfaces."""
    started = threading.Event()
    finished = threading.Event()
    seen_when_cancelled: list[bool] = []

    def step() -> None:
        started.set()
        time.sleep(0.3)
        finished.set()

    async def caller() -> None:
        try:
            await off_loop(step)
        except asyncio.CancelledError:
            seen_when_cancelled.append(finished.is_set())
            raise

    async def main() -> None:
        asyncio.create_task(caller())  # noqa: RUF006 - left running on purpose for shutdown
        await asyncio.to_thread(started.wait)

    asyncio.run(main())
    assert seen_when_cancelled == [True]


async def test_replay_validation_runs_off_the_loop() -> None:
    """Entering a composed pass checks saved summaries for replay, which can hash every one of
    them; it runs on a worker thread, and its scope is still set on the pass's own task."""
    loop_thread = threading.get_ident()
    strategy = _strategy(_Summarizer(), budget=200)
    validated_on: list[int] = []
    original = strategy.user_turns.invalid_replay_summaries

    def recording(*args: object, **kwargs: object) -> set[str]:
        validated_on.append(threading.get_ident())
        return original(*args, **kwargs)  # type: ignore[arg-type]

    strategy.user_turns.invalid_replay_summaries = recording  # type: ignore[method-assign]
    with contextlib.suppress(ValueError):
        await strategy(_history(10))
    assert validated_on
    assert validated_on[0] != loop_thread


def test_shutdown_with_the_step_still_queued_does_not_hang() -> None:
    """A step whose executor job had not started when shutdown cancelled it never runs; the
    caller must not wait for it. Run in a thread so a hang fails the test instead of the run."""
    from concurrent.futures import ThreadPoolExecutor

    blocker_started = threading.Event()
    step_ran = threading.Event()
    seen_cancelled: list[bool] = []

    def blocker() -> None:
        blocker_started.set()
        time.sleep(0.3)

    async def caller() -> None:
        try:
            await off_loop(step_ran.set)
        except asyncio.CancelledError:
            seen_cancelled.append(True)
            raise

    async def main() -> None:
        # One worker, busy with the blocker, so the step's job stays queued at shutdown.
        asyncio.get_running_loop().set_default_executor(ThreadPoolExecutor(max_workers=1))
        asyncio.create_task(asyncio.to_thread(blocker))  # noqa: RUF006 - left for shutdown
        while not blocker_started.is_set():
            await asyncio.sleep(0.005)
        asyncio.create_task(caller())  # noqa: RUF006 - left running on purpose for shutdown
        await asyncio.sleep(0.02)

    runner = threading.Thread(target=asyncio.run, args=(main(),), daemon=True)
    runner.start()
    runner.join(5)
    assert not runner.is_alive(), "shutdown hung waiting for a step that never started"
    assert seen_cancelled == [True]
    assert not step_ran.is_set()
