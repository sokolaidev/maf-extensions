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
    _body,
    _history,
    _record_size,
    _sent,
    _sent_values,
    _size,
    _strategy,
    _Summarizer,
    _values,
)
from test_summary_input import summaries

from maf_compaction import DEFAULT_CHAIN_SECONDS, CompactionBudgetExceeded
from maf_compaction import _composed as composed_module
from maf_compaction._composed import (
    ToolResultAndUserTurnAnchoredSummarizationCompactionStrategy,
    _numbered_records,
)

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
    now = 0.0
    monkeypatch.setattr(composed_module, "monotonic", lambda: now)

    class Waiting(_Summarizer):
        async def get_response(self, messages: list[Message], **kwargs: Any) -> ChatResponse:
            nonlocal now
            answer = await super().get_response(messages, **kwargs)
            now = 31.0
            return answer

    summarizer = Waiting()
    messages = _history(16)
    strategy = _strategy(summarizer, budget=_record_size(messages) - 200, share=0.01)
    strategy.harder_attempts = 2
    strategy.user_turns.retry_without_summary_options = True

    await strategy(messages)

    assert len(summarizer.requests) == 1
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
    now = 0.0
    monkeypatch.setattr(composed_module, "monotonic", lambda: now)

    class Waiting(_Summarizer):
        async def get_response(self, messages: list[Message], **kwargs: Any) -> ChatResponse:
            nonlocal now
            answer = await super().get_response(messages, **kwargs)
            now = 31.0
            return answer

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


@pytest.mark.parametrize("records", [4, 8])
@pytest.mark.parametrize("restore", [False, True])
@pytest.mark.parametrize("rebuild", [False, True])
@pytest.mark.parametrize("share", [None, 0.001])
async def test_pending_sibling_resumes_after_an_accepted_child(
    records: int, restore: bool, rebuild: bool, share: float | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = 0.0
    monkeypatch.setattr(composed_module, "monotonic", lambda: now)

    class Interrupting(_Summarizer):
        async def get_response(self, messages: list[Message], **kwargs: Any) -> ChatResponse:
            nonlocal now
            response = await super().get_response(messages, **kwargs)
            if len(self.requests) == (2 if records == 4 else 3):
                now = 31.0
            return response

    summarizer = Interrupting(
        merge=lambda body: (
            _VALUE.findall(body)[0]
            if _batch_size(body) > 2
            else " ".join(sorted(set(_VALUE.findall(body))))
        )
    )
    messages = _history(records)
    original = copy.deepcopy(messages)
    strategy = _strategy(summarizer, budget=100_000, share=share, cap=None)
    token = strategy._chain_deadline.set(30.0)
    try:
        with pytest.raises(composed_module._ChainTimeSpent):
            await strategy._merge_records(messages)
    finally:
        strategy._chain_deadline.reset(token)
    assert strategy.records_merged == 1
    pending = _numbered_records([_body(2).strip(), _body(3).strip()])

    resumed = _Summarizer(
        merge=lambda body: (
            " ".join(sorted(set(_VALUE.findall(body))))
            if _batch_size(body) == 2
            else _VALUE.findall(body)[0]
            if _batch_size(body) == 4 and records == 8
            else body + " padding" * 2000
        )
    )
    if restore:
        restored = _strategy(resumed, budget=100_000, share=share, cap=None)
        restored.restore_session_state(json.loads(json.dumps(strategy.session_state())))
        strategy = restored
    else:
        monkeypatch.setattr(strategy.user_turns, "client", resumed)
    if rebuild:
        messages = original
        assert strategy._keep_decisions(messages)
    before = strategy.records_merged

    assert await strategy._merge_records(messages)
    assert resumed.merges[0] == pending
    assert strategy.records_merged - before == records // 2 - 1
    assert _sent_values(messages) == {value for index in range(records) for value in _values(index)}


@pytest.mark.parametrize("share", [None, 0.001])
async def test_resumed_batch_retains_partial_success_when_a_later_child_fails(
    share: float | None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = 0.0
    monkeypatch.setattr(composed_module, "monotonic", lambda: now)

    class Interrupting(_Summarizer):
        async def get_response(self, messages: list[Message], **kwargs: Any) -> ChatResponse:
            nonlocal now
            response = await super().get_response(messages, **kwargs)
            if len(self.requests) == 2:
                now = 31.0
            return response

    first = Interrupting(
        merge=lambda body: (
            _VALUE.findall(body)[0]
            if _batch_size(body) > 4
            else " ".join(sorted(set(_VALUE.findall(body))))
        )
    )
    messages = _history(8)
    strategy = _strategy(first, budget=100_000, share=share, cap=None)
    token = strategy._chain_deadline.set(30.0)
    try:
        with pytest.raises(composed_module._ChainTimeSpent):
            await strategy._merge_records(messages)
    finally:
        strategy._chain_deadline.reset(token)
    assert strategy.records_merged == 1

    class Failing(_Summarizer):
        async def get_response(self, messages: list[Message], **kwargs: Any) -> ChatResponse:
            if len(self.requests) == 2:
                raise ConnectionError("temporarily unavailable")
            return await super().get_response(messages, **kwargs)

    resumed = _strategy(
        Failing(
            merge=lambda body: (
                _VALUE.findall(body)[0]
                if _batch_size(body) > 2
                else " ".join(sorted(set(_VALUE.findall(body))))
            )
        ),
        budget=100_000,
        share=share,
        cap=None,
    )
    resumed.restore_session_state(json.loads(json.dumps(strategy.session_state())))
    assert await resumed._merge_records(messages)
    assert resumed.records_merged == 1
    assert resumed.record_summary_failures == 1

    final_model = _Summarizer()
    final = _strategy(final_model, budget=100_000, share=share, cap=None)
    final.restore_session_state(json.loads(json.dumps(resumed.session_state())))
    assert await final._merge_records(messages)
    assert [_batch_size(body) for body in final_model.merges] == [2]
    assert _sent_values(messages) == {value for index in range(8) for value in _values(index)}


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


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("lossy", [False, True])
async def test_legacy_refused_batch_retries_halves_but_explicit_empty_state_does_not(legacy, lossy):
    original = _strategy(
        _Summarizer(merge=lambda body: _VALUE.findall(body)[0] if lossy else body * 3),
        budget=100_000,
        share=0.001,
        cap=None,
    )
    original.record_merge_split_depth = 0
    await original._merge_batches(_history(4), None, 0.001)
    state = json.loads(json.dumps(original.session_state()))
    assert len(state["refused_merges"]) == 1
    if legacy:
        state.pop("split_merges")
        state.pop("pending_merges")
    else:
        state["split_merges"] = []
    model = _Summarizer()
    restored = _strategy(model, budget=100_000, share=0.001, cap=None)
    restored.restore_session_state(state)
    messages = _history(4)
    await restored._merge_batches(messages, None, 0.001)
    assert [_batch_size(body) for body in model.merges] == ([2, 2] if legacy else [])
    assert restored.records_merged == (2 if legacy else 0)
    assert _sent_values(messages) == {value for i in range(4) for value in _values(i)}


async def test_expired_chain_does_not_start_an_option_retry(monkeypatch):
    now = 0.0
    monkeypatch.setattr(composed_module, "monotonic", lambda: now)

    class Failing(_Summarizer):
        async def get_response(self, messages, **kwargs):
            nonlocal now
            await super().get_response(messages, **kwargs)
            now = 31.0
            raise ValueError("Unsupported option")

    model = Failing()
    messages = _history(16)
    strategy = _strategy(model, budget=_record_size(messages) - 200, share=0.01)
    strategy.user_turns.retry_without_summary_options = True
    await strategy(messages)
    assert len(model.requests) == 1
    assert strategy.chain_timeouts == 1
    assert not strategy.user_turns._summary_options_disabled


@pytest.mark.parametrize("fail", [False, True])
async def test_fold_finishes_after_deadline_without_starting_option_retry(monkeypatch, fail):
    now = 0.0
    monkeypatch.setattr(composed_module, "monotonic", lambda: now)

    class Late(_Summarizer):
        async def get_response(self, messages, **kwargs):
            nonlocal now
            answer = await super().get_response(messages, **kwargs)
            now = 31.0
            if fail:
                raise ValueError("Unsupported option")
            return answer

    model = Late()
    strategy = _strategy(model, budget=100_000, cap=10_000)
    strategy.user_turns.retry_without_summary_options = True
    messages = summaries(2)
    _size(messages)
    token = strategy._chain_deadline.set(30.0)
    try:
        if fail:
            with pytest.raises(composed_module._ChainTimeSpent):
                await strategy._merge_user_summaries(messages)
        else:
            assert await strategy._merge_user_summaries(messages)
    finally:
        strategy._chain_deadline.reset(token)
    assert len(model.requests) == 1
    assert strategy.user_summaries_merged == int(not fail)
    assert strategy.user_summary_failures == 0
    assert not strategy.user_turns._summary_options_disabled
