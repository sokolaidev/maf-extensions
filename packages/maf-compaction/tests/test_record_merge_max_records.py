"""Bound the records in merge and rewrite requests."""

from __future__ import annotations

import json
import re
from typing import Any

import pytest
from agent_framework import ChatResponse, Message
from test_record_budget import (
    _VALUE,
    _body,
    _history,
    _record_pair,
    _sent_values,
    _strategy,
    _Summarizer,
    _values,
)
from test_summary_prompt_boundary import record_attempt

from maf_compaction import _composed as composed_module
from maf_compaction._composed import (
    ToolResultAndUserTurnAnchoredSummarizationCompactionStrategy,
    _numbered_records,
    harder_record_prompt,
)
from maf_compaction._summary_input import summary_input_tokens

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


@pytest.mark.parametrize(
    ("records", "cap", "fit"), [(24, 5, 24), (7, 5, 3), (7, None, 7), (1, 5, 1)]
)
async def test_rewrites_take_the_oldest_records_within_both_limits(
    records: int, cap: int | None, fit: int
) -> None:
    model = _Summarizer()
    strategy = _strategy(model, budget=100_000, cap=None)
    strategy.harder_attempts = 1
    strategy.record_merge_max_records = cap
    bodies = [_body(index).strip() for index in range(records)]
    strategy.record_request_input_tokens = summary_input_tokens(
        harder_record_prompt(1), _numbered_records(bodies[:fit]), tokenizer=strategy.tokenizer
    )
    messages = _history(records)

    assert not await record_attempt(strategy, messages, "rewrite")

    expected = bodies[:fit][:cap]
    assert model.requests == [(harder_record_prompt(1), _numbered_records(expected))]
    assert _sent_values(messages) == _all_values(records)


@pytest.mark.parametrize("version", ["1", "2"])
async def test_only_feasible_builtin_rewrite_targets_are_sent(
    version: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MAF_COMPACTION_PROMPT_VERSION", version)
    model = _Summarizer()
    strategy = _strategy(model, budget=32, cap=None)
    strategy.user_turns.max_summary_input_tokens = 2_000
    strategy.harder_attempts = 2
    first = "ID-00001 ID-00002 ID-00003"
    second = "ID-00004 ID-00001 with some surrounding prose"
    messages = [*_record_pair(0, body=first), *_record_pair(1, body=second)]

    # All four distinct values need 35 characters. They fit at 60% but not at 36%.
    assert not await record_attempt(strategy, messages, "rewrite")
    assert [prompt for prompt, _ in model.requests] == [strategy.rewrite_prompt(1)]
    assert (strategy.record_rewrites, strategy.record_rewrites_rejected) == (1, 1)
    assert strategy.record_rewrites_skipped == 1

    strategy.tool_results.coverage_share = 0.5
    assert not await record_attempt(strategy, messages, "rewrite")
    assert [prompt for prompt, _ in model.requests] == [
        strategy.rewrite_prompt(1),
        strategy.rewrite_prompt(2),
    ]


async def test_a_custom_rewrite_can_choose_its_own_length_target() -> None:
    model = _Summarizer()
    strategy = _strategy(model, budget=100_000, cap=None)
    strategy.rewrite_prompt = lambda _: "Keep the exact values."
    strategy.harder_attempts = 1
    messages = [*_record_pair(0, body="ID-00001"), *_record_pair(1, body="ID-00002")]

    assert not await record_attempt(strategy, messages, "rewrite")
    assert len(model.requests) == 1


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
