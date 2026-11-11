"""Bound record reduction without losing accepted work, values or cancellation."""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import re
from typing import Any

import pytest
from agent_framework import ChatResponse, Content, Message, UsageDetails
from test_record_budget import (
    _VALUE,
    _history,
    _record_size,
    _sent,
    _sent_values,
    _size,
    _strategy,
    _Summarizer,
    _values,
)

from maf_compaction import DEFAULT_CHAIN_SECONDS, CompactionBudgetExceeded
from maf_compaction import _composed as composed_module
from maf_compaction._composed import ToolResultAndUserTurnAnchoredSummarizationCompactionStrategy

pytestmark = pytest.mark.anyio


def _batch_size(body: str) -> int:
    return len(re.findall(r"^\d+\. ", body, re.MULTILINE))


@pytest.mark.parametrize("seconds", [0, -1, float("inf"), float("nan"), True])
def test_invalid_chain_seconds_are_refused(seconds: float) -> None:
    original = _strategy(_Summarizer(), budget=1000)
    with pytest.raises(ValueError, match="chain_seconds"):
        ToolResultAndUserTurnAnchoredSummarizationCompactionStrategy(
            tokenizer=original.tokenizer,
            tool_results=original.tool_results,
            user_turns=original.user_turns,
            chain_seconds=seconds,
        )


@pytest.mark.parametrize("depth", [-1, True, 1.5])
def test_invalid_split_depth_is_refused(depth: Any) -> None:
    original = _strategy(_Summarizer(), budget=1000)
    with pytest.raises(ValueError, match="record_merge_split_depth"):
        ToolResultAndUserTurnAnchoredSummarizationCompactionStrategy(
            tokenizer=original.tokenizer,
            tool_results=original.tool_results,
            user_turns=original.user_turns,
            record_merge_split_depth=depth,
        )


async def test_lossy_parent_retries_halves_and_replays_both_accepted_children() -> None:
    summarizer = _Summarizer(
        merge=lambda body: (
            _VALUE.findall(body)[0]
            if _batch_size(body) > 2
            else " ".join(sorted(set(_VALUE.findall(body))))
        )
    )
    messages = _history(4)
    original = copy.deepcopy(messages)
    strategy = _strategy(summarizer, budget=_record_size(messages) - 200, share=0.01, cap=None)
    assert strategy.chain_seconds == DEFAULT_CHAIN_SECONDS == 30

    await strategy(messages)

    assert [_batch_size(body) for body in summarizer.merges] == [4, 2, 2]
    assert strategy.records_merged == 2
    assert _sent_values(messages) == {value for i in range(4) for value in _values(i)}
    restored = _strategy(summarizer, budget=100_000, cap=None)
    restored.restore_session_state(json.loads(json.dumps(strategy.session_state())))
    await restored(original)
    assert _sent(original) == _sent(messages)
    assert len(summarizer.requests) == 3


async def test_refused_children_are_remembered_through_restore_and_split_depth_is_bounded() -> None:
    summarizer = _Summarizer(merge=lambda body: _VALUE.findall(body)[0])
    messages = _history(16)
    strategy = _strategy(summarizer, budget=_record_size(messages) - 200, share=0.01, cap=None)
    await strategy(messages)
    assert [_batch_size(body) for body in summarizer.merges] == [16, 8, 4, 4, 8, 4, 4]
    restored = _strategy(summarizer, budget=100_000, cap=None)
    restored.restore_session_state(json.loads(json.dumps(strategy.session_state())))
    await restored._merge_batches(messages, None, 0.001)
    assert len(summarizer.merges) == 7
    assert restored.record_merges_skipped == 7


async def test_truncated_answer_is_split_even_when_it_keeps_every_value(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class Limited(_Summarizer):
        async def get_response(self, messages: list[Message], **kwargs: Any) -> ChatResponse:
            response = await super().get_response(messages, **kwargs)
            response.finish_reason = "length" if _batch_size(self.merges[-1]) > 2 else "stop"
            response.usage_details = UsageDetails(
                input_token_count=500,
                output_token_count=100,
                reasoning_output_token_count=80,
            )
            return response

    summarizer = Limited()
    messages = _history(4)
    strategy = _strategy(summarizer, budget=_record_size(messages) - 200, share=0.01, cap=None)
    with caplog.at_level(logging.INFO, logger="maf_compaction._composed"):
        await strategy(messages)
    assert [_batch_size(body) for body in summarizer.merges] == [4, 2, 2]
    assert strategy.record_consolidations_lossy == 0
    assert "output limit" in caplog.text
    assert "finish_reason=length" in caplog.text
    assert "input_tokens=500 output_tokens=100 reasoning_tokens=80" in caplog.text
    assert not _VALUE.search(caplog.text)


@pytest.mark.parametrize("retry", [False, True])
@pytest.mark.parametrize("output", ["empty", "whitespace", "reasoning_only"])
async def test_empty_length_response_splits_without_retiring_options(
    retry: bool,
    output: str,
) -> None:
    class Limited(_Summarizer):
        def __init__(self) -> None:
            super().__init__()
            self.options: list[dict[str, Any]] = []

        async def get_response(self, messages: list[Message], **kwargs: Any) -> ChatResponse:
            self.options.append(dict(kwargs.get("options", {})))
            response = await super().get_response(messages, **kwargs)
            if _batch_size(self.merges[-1]) <= 2:
                return response
            contents = (
                [Content.from_text_reasoning(text="Still deciding.")]
                if output == "reasoning_only"
                else [Content.from_text(" \n\t " if output == "whitespace" else "")]
            )
            return ChatResponse(messages=[Message("assistant", contents)], finish_reason="length")

    summarizer = Limited()
    messages = _history(4)
    values = _sent_values(messages)
    strategy = _strategy(summarizer, budget=100_000, share=0.001, cap=10_000)
    strategy.user_turns.retry_without_summary_options = retry
    configured = dict(strategy.user_turns.summary_options)

    assert await strategy._merge_batches(messages, None, 0.001)

    assert [_batch_size(body) for body in summarizer.merges] == [4, 2, 2]
    assert summarizer.options == [configured] * 3
    assert not strategy.user_turns._summary_options_disabled
    assert strategy.record_summary_failures == 0
    assert strategy.records_merged == 2
    assert _sent_values(messages) == values


@pytest.mark.parametrize("retry", [False, True])
async def test_empty_length_rewrite_is_refused_without_retiring_options(retry: bool) -> None:
    class Limited(_Summarizer):
        async def get_response(self, messages: list[Message], **kwargs: Any) -> ChatResponse:
            response = await super().get_response(messages, **kwargs)
            response.finish_reason = "length"
            return response

    summarizer = Limited(rewrite=lambda _: " \n\t ")
    messages = _history(2)
    original = _sent(messages)
    strategy = _strategy(summarizer, budget=100_000, cap=10_000)
    strategy.harder_attempts = 1
    strategy.user_turns.retry_without_summary_options = retry

    assert not await strategy._rewrite_records(messages, composed_module._ChainStart.take(messages))
    assert len(summarizer.requests) == 1
    assert strategy.record_rewrites_rejected == 1
    assert strategy.record_summary_failures == 0
    assert not strategy.user_turns._summary_options_disabled
    assert _sent(messages) == original


async def test_refusal_reports_exact_distinct_value_counts(
    caplog: pytest.LogCaptureFixture,
) -> None:
    messages = _history(2)
    summarizer = _Summarizer(merge=lambda _: "VAL-000-0")
    strategy = _strategy(summarizer, budget=100_000, share=0.001, cap=None)
    with caplog.at_level(logging.WARNING, logger="maf_compaction._composed"):
        await strategy._merge_batches(messages, None, 0.001)
    assert "values_total=10 kept=1 missing=9 required=8" in caplog.text
    assert not _VALUE.search(caplog.text)
    assert strategy._keeps_values(["VAL-001 VAL-002"], "VAL-002 VAL-001")
    assert not strategy._keeps_values(["VAL-001 VAL-002"], "val-002 val-001")
    values = [f"VAL-{i:03}" for i in range(200)]
    assert strategy._value_coverage([" ".join(values)], " ".join(values[:-1])) == (200, 199, 160)


async def test_deadline_retains_accepted_merge_and_never_starts_fold_or_rewrite(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_timeout = asyncio.timeout
    scopes: list[asyncio.Timeout] = []

    def controlled_timeout(seconds: float | None) -> asyncio.Timeout:
        scope = real_timeout(None)
        scopes.append(scope)
        return scope

    monkeypatch.setattr(asyncio, "timeout", controlled_timeout)
    cancelled = asyncio.Event()

    class Waiting(_Summarizer):
        async def get_response(self, messages: list[Message], **kwargs: Any) -> ChatResponse:
            if len(self.requests) == 1:
                scopes[-1].reschedule(asyncio.get_running_loop().time())
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()
            return await super().get_response(messages, **kwargs)

    summarizer = Waiting()
    messages = _history(16)
    strategy = _strategy(summarizer, budget=_record_size(messages) - 200, share=0.01)
    strategy.harder_attempts = 2
    strategy.user_turns.retry_without_summary_options = True

    await strategy(messages)

    assert cancelled.is_set()
    assert strategy.chain_timeouts == 1
    assert strategy.records_merged == 1
    assert strategy.user_summaries_merged == strategy.record_rewrites == 0
    assert not strategy.user_turns._summary_options_disabled
    assert _size(messages) < strategy.max_input_tokens
    assert _sent_values(messages) == {value for i in range(16) for value in _values(i)}
    assert strategy._refused_merges == set(), "deadline expiry is not a refusal"


async def test_caller_cancellation_propagates_without_retiring_options() -> None:
    entered = asyncio.Event()

    class Waiting(_Summarizer):
        async def get_response(self, messages: list[Message], **kwargs: Any) -> ChatResponse:
            entered.set()
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

    messages = _history(4)
    strategy = _strategy(Waiting(), budget=_record_size(messages) - 200, share=0.01)
    strategy.user_turns.retry_without_summary_options = True
    task = asyncio.create_task(strategy(messages))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert strategy.chain_timeouts == 0
    assert strategy._refused_merges == set()
    assert not strategy.user_turns._summary_options_disabled


async def test_timeout_before_a_child_resumes_halves_after_restore(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_timeout = asyncio.timeout
    scopes: list[asyncio.Timeout] = []

    def controlled_timeout(seconds: float | None) -> asyncio.Timeout:
        scope = real_timeout(None)
        scopes.append(scope)
        return scope

    monkeypatch.setattr(asyncio, "timeout", controlled_timeout)

    class Waiting(_Summarizer):
        async def get_response(self, messages: list[Message], **kwargs: Any) -> ChatResponse:
            if self.requests:
                scopes[-1].reschedule(asyncio.get_running_loop().time())
                await asyncio.Event().wait()
            return await super().get_response(messages, **kwargs)

    summarizer = Waiting(merge=lambda body: _VALUE.findall(body)[0])
    messages = _history(4)
    strategy = _strategy(summarizer, budget=_record_size(messages) - 200, share=0.01, cap=None)
    await strategy(messages)
    assert strategy.chain_timeouts == 1
    assert len(strategy._refused_merges) == len(strategy._split_merges) == 1
    resumed = _Summarizer()
    restored = _strategy(resumed, budget=100_000, cap=None)
    restored.restore_session_state(json.loads(json.dumps(strategy.session_state())))
    await restored._merge_batches(messages, None, 0.001)
    assert [_batch_size(body) for body in resumed.merges] == [2, 2]
    assert restored.records_merged == 2


async def test_expired_deadline_still_enforces_the_sending_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    messages = _history(2)
    strategy = _strategy(_Summarizer(), budget=50, share=0.01)
    clock = iter([0.0, 31.0])
    monkeypatch.setattr(composed_module, "monotonic", lambda: next(clock, 31.0))
    with pytest.raises(CompactionBudgetExceeded):
        await strategy(messages)
    assert strategy.chain_timeouts == 1
    assert strategy._refused_merges == set()


async def test_share_alone_does_not_start_a_chain() -> None:
    messages = _history(4)
    summarizer = _Summarizer()
    strategy = _strategy(summarizer, budget=_size(messages) * 2, share=0.01)
    await strategy(messages)
    assert summarizer.requests == []


async def test_missing_split_state_loads_as_no_pending_children() -> None:
    strategy = _strategy(_Summarizer(), budget=1000)
    state = strategy.session_state()
    state.pop("split_merges")
    strategy.restore_session_state(state)
    assert strategy._split_merges == set()
