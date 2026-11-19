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


class _LingeringEvent(threading.Event):
    """An event whose ``set`` holds the setting thread a moment: the step reports finished
    while its task is still one callback behind, the window a cancelled caller must wait out."""

    def set(self) -> None:
        super().set()
        time.sleep(0.1)


def test_a_cancelled_callers_failed_step_is_not_reported_as_unretrieved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The step's error is retrieved even though its caller was cancelled, so asyncio never
    reports it as a task exception nobody retrieved."""
    import gc

    monkeypatch.setattr("maf_compaction._offload._new_event", _LingeringEvent)
    reported: list[dict[str, object]] = []
    started = threading.Event()

    def step() -> None:
        started.set()
        time.sleep(0.1)
        raise ValueError("step failed")

    async def main() -> None:
        asyncio.get_running_loop().set_exception_handler(
            lambda _loop, context: reported.append(context)
        )
        caller = asyncio.create_task(off_loop(step))
        await asyncio.to_thread(started.wait)
        caller.cancel()
        await asyncio.wait({caller})
        assert caller.cancelled()
        del caller
        await asyncio.sleep(0.05)
        gc.collect()
        await asyncio.sleep(0.01)

    asyncio.run(main())
    assert [context.get("message") for context in reported] == []


class _LoopThreadCounter(CharacterEstimatorTokenizer):
    """Counts like the estimator, and records each count made on the given (loop) thread."""

    def __init__(self, loop_thread: int) -> None:
        super().__init__()
        self.loop_thread = loop_thread
        self.on_loop = 0

    def count_tokens(self, text: str) -> int:
        if threading.get_ident() == self.loop_thread:
            self.on_loop += 1
        return super().count_tokens(text)


async def test_a_fold_counts_no_tokens_on_the_loop() -> None:
    """Folding standing summaries checks the fold is smaller and applies it off the loop."""
    from test_usersummary import (
        _LARGE_SUMMARY_CHARS,
        _conversation,
        _fold_scenario,
        _standing,
        _three_standing,
    )
    from test_usersummary import _Summarizer as _UserSummarizer

    strategy = _fold_scenario(_UserSummarizer("x" * _LARGE_SUMMARY_CHARS), min_band_share=0.05)
    messages = await _three_standing(strategy)
    messages += _conversation(1, first_turn=20)
    counter = _LoopThreadCounter(threading.get_ident())
    strategy.tokenizer = counter
    assert await strategy(messages)
    assert len(_standing(messages)) == 1, "the pass folded"
    assert counter.on_loop == 0


async def test_a_chunked_summary_counts_no_tokens_on_the_loop() -> None:
    """A band too long for one request is summarized in chunks; each chunk's answer is
    checked against its transcript off the loop."""
    from test_usersummary import _COMPACTING_CEILING, _conversation
    from test_usersummary import _Summarizer as _UserSummarizer

    from maf_compaction import UserTurnAnchoredSummarizationCompactionStrategy

    summarizer = _UserSummarizer()
    counter = _LoopThreadCounter(threading.get_ident())
    strategy = UserTurnAnchoredSummarizationCompactionStrategy(
        tokenizer=counter,
        client=summarizer,
        max_input_tokens=_COMPACTING_CEILING,
        max_summary_input_tokens=3_000,
        summary_input_policy="chunk",
    )
    assert await strategy(_conversation(12))
    assert len(summarizer.requests) > 1, "the band was summarized in chunks"
    assert counter.on_loop == 0


#: Synchronous helpers that walk a whole conversation, render it, or tokenize it.
_HISTORY_WORK = frozenset(
    {
        "annotate_token_counts",
        "annotate_message_groups",
        "bounded_prefix",
        "bounded_prefix_extent",
        "fits_summary_input",
        "count_tokens",
        "_format_turns",
        "_smaller",
        "_apply_fold",
        "record_bodies",
        "_value_coverage",
        "_bounded_summaries",
        "_bounded_records",
        "consolidatable_record_groups",
        "group_messages",
        "_recall_results",
        "invalid_replay_summaries",
        "replay_summaries",
        "_replay_record_edits",
        "_keep_records",
        "_reshape_records",
        "_refresh_merge_coverage",
        "_lossy_merge_matches",
        "shed_to",
        "_band",
        "_observe_summaries",
        "_transcript",
        "function_result_candidates",
        "_record_candidates",
    }
)

#: Calls an async method may still make on the loop, each for a stated reason.
_ALLOWED_ON_LOOP = {
    # The background worker's own steps run inline by design (``steps_inline``).
    ("_prepare_record_merges", "annotate_message_groups"),
    ("_prepare_record_merges", "annotate_token_counts"),
    # One turn's own text, counted to decide whether a media description is worth keeping.
    ("_describe_turn", "count_tokens"),
}


def test_no_async_method_does_history_work_on_the_loop() -> None:
    """Every whole-history, rendering or tokenizing step an async method makes goes through
    :func:`off_loop`, so the next pass that holds the loop fails here rather than in review."""
    import ast
    from pathlib import Path

    import maf_compaction

    found: list[str] = []
    for path in sorted(Path(maf_compaction.__file__).parent.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for function in ast.walk(tree):
            if not isinstance(function, ast.AsyncFunctionDef):
                continue
            excluded: set[int] = set()
            for node in ast.walk(function):
                offloaded = (
                    isinstance(node, ast.Call) and getattr(node.func, "id", None) == "off_loop"
                )
                nested = node is not function and isinstance(
                    node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)
                )
                if offloaded or nested:
                    excluded.update(id(sub) for sub in ast.walk(node))
            for node in ast.walk(function):
                if not isinstance(node, ast.Call) or id(node) in excluded:
                    continue
                name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
                if name in _HISTORY_WORK and (function.name, name) not in _ALLOWED_ON_LOOP:
                    found.append(f"{path.name}:{node.lineno} {function.name} -> {name}")
    assert found == []
