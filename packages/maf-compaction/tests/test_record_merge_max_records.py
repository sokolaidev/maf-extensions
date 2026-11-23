"""Cap how many records one merge batch may hold (``record_merge_max_records``)."""

from __future__ import annotations

import json
import re
from typing import Any

import pytest
from agent_framework import ChatResponse, Message
from test_record_budget import (
    _VALUE,
    _history,
    _sent_values,
    _strategy,
    _Summarizer,
    _values,
)

from maf_compaction import _composed as composed_module
from maf_compaction._composed import ToolResultAndUserTurnAnchoredSummarizationCompactionStrategy

pytestmark = pytest.mark.anyio


def _batch_size(body: str) -> int:
    return len(re.findall(r"^\d+\. ", body, re.MULTILINE))


def _all_values(records: int) -> set[str]:
    return {value for index in range(records) for value in _values(index)}


@pytest.mark.parametrize("records", [1, 0, -1, True, 2.5, "5"])
def test_invalid_record_merge_max_records_are_refused(records: Any) -> None:
    original = _strategy(_Summarizer(), budget=1000)
    with pytest.raises(ValueError, match="record_merge_max_records"):
        ToolResultAndUserTurnAnchoredSummarizationCompactionStrategy(
            tokenizer=original.tokenizer,
            tool_results=original.tool_results,
            user_turns=original.user_turns,
            record_merge_max_records=records,
        )


def test_no_cap_by_default() -> None:
    assert _strategy(_Summarizer(), budget=1000).record_merge_max_records is None


@pytest.mark.parametrize("cap", [None, 2, 3, 5])
async def test_a_sweep_sends_no_batch_over_the_cap(cap: int | None) -> None:
    """Seven records that fit one request: uncapped they merge as one batch, capped in batches of
    at most the cap, and every value is kept either way."""
    messages = _history(7)
    summarizer = _Summarizer()
    strategy = _strategy(summarizer, budget=100_000, share=0.001, cap=None)
    strategy.record_merge_max_records = cap

    assert await strategy._merge_batches(messages, None, 0.001)

    sizes = [_batch_size(body) for body in summarizer.merges]
    if cap is None:
        assert sizes == [7]
    else:
        assert sizes and max(sizes) <= cap, sizes
        assert sizes[0] == cap, "the oldest records, as many as the cap allows"
    assert _sent_values(messages) == _all_values(7)


async def test_the_single_merge_without_a_share_is_capped() -> None:
    messages = _history(6)
    summarizer = _Summarizer()
    strategy = _strategy(summarizer, budget=100_000, share=None, cap=None)
    strategy.record_merge_max_records = 4

    assert await strategy._merge_batches(messages, None, None)

    assert [_batch_size(body) for body in summarizer.merges] == [4]
    assert _sent_values(messages) == _all_values(6)


async def test_a_remembered_uncapped_batch_is_capped_after_restore() -> None:
    """A search saved before the cap remembered a batch of all eight records; restored under a cap
    of three, the sweep still sends no more than three."""
    first = _Summarizer(merge=lambda _: (_ for _ in ()).throw(ConnectionError("unavailable")))
    strategy = _strategy(first, budget=100_000, share=0.001, cap=None)
    messages = _history(8)
    assert not await strategy._merge_batches(messages, None, 0.001)
    assert [_batch_size(body) for body in first.merges] == [8], "the premise: one batch of eight"

    summarizer = _Summarizer()
    restored = _strategy(summarizer, budget=100_000, share=0.001, cap=None)
    restored.record_merge_max_records = 3
    restored.restore_session_state(json.loads(json.dumps(strategy.session_state())))

    assert await restored._merge_batches(messages, None, 0.001)

    sizes = [_batch_size(body) for body in summarizer.merges]
    assert sizes and max(sizes) <= 3, sizes
    assert _sent_values(messages) == _all_values(8)


async def test_pending_halves_over_the_cap_are_dropped_and_searched_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A refused batch of eight leaves halves of four pending; restored under a cap of three, the
    halves are not resumed and the records merge in batches of at most three."""
    now = 0.0
    monkeypatch.setattr(composed_module, "monotonic", lambda: now)

    class Waiting(_Summarizer):
        async def get_response(self, messages: list[Message], **kwargs: Any) -> ChatResponse:
            nonlocal now
            answer = await super().get_response(messages, **kwargs)
            now = 31.0
            return answer

    refusing = Waiting(merge=lambda body: _VALUE.findall(body)[0])
    messages = _history(8)
    strategy = _strategy(refusing, budget=100_000, share=0.001, cap=None)
    token = strategy._chain_deadline.set(30.0)
    try:
        with pytest.raises(composed_module._ChainTimeSpent):
            await strategy._merge_records(messages)
    finally:
        strategy._chain_deadline.reset(token)
    state = json.loads(json.dumps(strategy.session_state()))
    assert [pending[0] for pending in state["pending_merges"]] == [4, 4], "the premise"

    summarizer = _Summarizer()
    restored = _strategy(summarizer, budget=100_000, share=0.001, cap=None)
    restored.record_merge_max_records = 3
    restored.restore_session_state(state)

    assert await restored._merge_records(messages)

    sizes = [_batch_size(body) for body in summarizer.merges]
    assert sizes and max(sizes) <= 3, sizes
    assert _sent_values(messages) == _all_values(8)
