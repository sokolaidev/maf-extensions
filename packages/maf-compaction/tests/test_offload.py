"""A pass's synchronous steps run off the host's event loop, so the host keeps answering.

A pass over a long conversation spends seconds of CPU between its awaits. On the event loop that
time froze every other task the host serves. These tests pin the helper that moves a step to a
worker thread, and that a composed pass leaves the loop free while it works.
"""

from __future__ import annotations

import asyncio
import threading
import time

import pytest
from agent_framework._compaction import CharacterEstimatorTokenizer
from test_record_budget import _history, _strategy, _Summarizer

from maf_compaction._offload import off_loop, steps_inline

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


class _SlowTokenizer(CharacterEstimatorTokenizer):
    """Counts like the estimator, slowly: a stand-in for a long conversation's CPU."""

    def count_tokens(self, text: str) -> int:
        time.sleep(0.002)
        return super().count_tokens(text)


async def _longest_hold_during_pass(*, inline: bool) -> tuple[float, float]:
    """Run one composed pass beside a 5 ms ticker; return the pass time and the longest hold."""
    strategy = _strategy(_Summarizer(), budget=200, tokenizer=_SlowTokenizer())
    messages = _history(40)
    holds: list[float] = []
    stop = asyncio.Event()

    async def tick() -> None:
        last = time.perf_counter()
        while not stop.is_set():
            await asyncio.sleep(0.005)
            now = time.perf_counter()
            holds.append(now - last - 0.005)
            last = now

    ticker = asyncio.create_task(tick())
    await asyncio.sleep(0.01)
    started = time.perf_counter()
    with steps_inline(inline):
        try:
            await strategy(messages)
        except ValueError:
            pass  # a budget this small may be refused; the pass's work is what is measured
    elapsed = time.perf_counter() - started
    stop.set()
    await ticker
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
