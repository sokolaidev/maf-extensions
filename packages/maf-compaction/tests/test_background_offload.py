"""A background record-merge worker owns every container it can write, so its steps and its
session's foreground steps both run off the event loop, and the foreground reads the worker's
search only between the worker's steps.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
import time
from typing import Any

import pytest
from test_background_record_merges import running, tool_reply
from test_offload import _SlowTokenizer, _tick_until
from test_record_budget import _history, _strategy, _Summarizer

from maf_compaction._composed import _RecordMergeWork

pytestmark = pytest.mark.anyio

_CONTAINERS = (dict, list, set)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class _Waiting(_Summarizer):
    """Takes a request and never answers it, so the worker stays busy."""

    def __init__(self) -> None:
        super().__init__()
        self.asked = asyncio.Event()

    async def get_response(self, messages: Any, **kwargs: Any) -> Any:  # noqa: ARG002
        self.asked.set()
        await asyncio.Future()


def _shared_containers(worker: Any, strategy: Any) -> list[str]:
    shared = []
    for part in ("", "tool_results", "tool_results.fallback", "user_turns"):
        mine, theirs = worker, strategy
        for name in filter(None, part.split(".")):
            mine, theirs = getattr(mine, name), getattr(theirs, name)
        for name, value in vars(theirs).items():
            if isinstance(value, _CONTAINERS) and vars(mine).get(name) is value:
                shared.append(f"{part or 'strategy'}.{name}")
    return shared


async def test_a_worker_shares_no_container_with_its_strategy() -> None:
    strategy = _strategy(_Summarizer(), budget=100_000, share=0.001, cap=None)
    await strategy(_history(10))

    worker = strategy._record_worker(100_000, lambda *args: None, lambda: True)

    assert _shared_containers(worker, strategy) == []


async def _busy_worker(
    tokenizer: Any, *, size: int = 40
) -> tuple[Any, _RecordMergeWork, list, Any]:
    """A strategy whose background worker is waiting on a summary request that never ends."""
    budget = 100_000
    model = _Waiting()
    strategy = _strategy(model, budget=budget, share=0.001, cap=None, tokenizer=tokenizer)
    work = _RecordMergeWork()
    work.bind(strategy, None)
    messages = _history(size)
    with contextlib.suppress(ValueError):
        await strategy(messages)
    work.remember(messages, budget)
    await work.start(tool_reply())
    await asyncio.wait_for(model.asked.wait(), 30)
    return strategy, work, messages, model


async def test_a_pass_beside_a_busy_worker_leaves_the_loop_free() -> None:
    strategy, work, _, _ = await _busy_worker(_SlowTokenizer(), size=120)
    assert not running(work).done()
    # Built afresh, as a host hands each call, so the pass counts every message again.
    messages = _history(120)
    stop = asyncio.Event()
    ticker = asyncio.create_task(_tick_until(stop))
    await asyncio.sleep(0.01)
    started = time.perf_counter()
    try:
        await strategy(messages)
    finally:
        elapsed = time.perf_counter() - started
        stop.set()
        holds = await ticker
        await work.close()

    assert elapsed > 0.3
    assert max(holds) < 0.1


class _HoldingTokenizer(_SlowTokenizer):
    """Once armed, holds the first count it gets on its thread until released."""

    def __init__(self) -> None:
        super().__init__()
        self.armed = False
        self.holding = threading.Event()
        self.release = threading.Event()

    def count_tokens(self, text: str) -> int:
        if (
            self.armed
            and "compaction records" in text
            and threading.current_thread() is not threading.main_thread()
        ):
            self.armed = False
            self.holding.set()
            self.release.wait(10)
        return super().count_tokens(text)


async def test_a_running_worker_step_keeps_the_foreground_from_reading_its_search(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tokenizer = _HoldingTokenizer()
    model = _Waiting()
    strategy = _strategy(model, budget=100_000, share=0.001, cap=None, tokenizer=tokenizer)
    work = _RecordMergeWork()
    work.bind(strategy, None)
    messages = _history(40)
    await strategy(messages)
    work.remember(messages, 100_000)
    read: list[Any] = []
    monkeypatch.setattr(strategy, "_remember_record_search", read.append)
    tokenizer.armed = True
    await work.start(tool_reply())
    try:
        # The worker holds its step lock while counting the merge request.
        assert await asyncio.to_thread(tokenizer.holding.wait, 10)
        await work.apply_ready(messages)
        assert read == []

        tokenizer.release.set()
        await asyncio.wait_for(model.asked.wait(), 30)
        # Waiting on its summary request, the worker is between steps.
        await work.apply_ready(messages)
        assert len(read) == 1
    finally:
        tokenizer.release.set()
        await work.close()
