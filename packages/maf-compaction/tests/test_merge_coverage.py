"""Opt-in tolerance spends existing answers only after strict split work is exhausted."""

from __future__ import annotations

import copy
import json
import logging
from typing import Any

import pytest
from agent_framework import ChatResponse, Content, Message
from test_background_record_merges import running, tool_reply
from test_record_budget import (
    _VALUE,
    TOKENIZER,
    _record_pair,
    _sent_values,
    _size,
    _strategy,
    _Summarizer,
    _values,
)

from maf_compaction import CompactionBudgetExceeded
from maf_compaction import _composed as composed_module
from maf_compaction import _record_replay as replay_module
from maf_compaction._composed import (
    ToolResultAndUserTurnAnchoredSummarizationCompactionStrategy,
    _ChainTimeSpent,
    _PreparedMerge,
    _RecordAnswer,
    _RecordMergeWork,
)
from maf_compaction._merge_coverage import LossyMerge, MergeCoverage
from maf_compaction._preserve import STANDS_IN_KEY, set_preserved
from maf_compaction._summary_limiter import SummaryRateLimited
from maf_compaction._toolsummary import (
    build_record_message,
    consolidatable_record_groups,
    record_bodies,
)

pytestmark = pytest.mark.anyio


def _lossy(body: str) -> str:
    values = sorted(set(_VALUE.findall(body)))
    return " ".join(value for index, value in enumerate(values) if index % 4 != 3)


def _records(count: int = 4) -> list[Message]:
    messages = [Message(role="system", contents=["Keep the facts."], message_id="system")]
    for index in range(count):
        messages.extend(
            _record_pair(index, body=" ".join(_values(index)) + " details are recorded here." * 50)
        )
    messages.append(Message(role="user", contents=["Compare the facts."], message_id="last"))
    _size(messages)
    return messages


def _configured(
    summarizer: _Summarizer | None = None, *, share: float | None = 0.001, **kwargs: Any
) -> ToolResultAndUserTurnAnchoredSummarizationCompactionStrategy:
    base = _strategy(summarizer or _Summarizer(merge=_lossy), budget=1000, share=share, cap=None)
    base.user_turns.max_summary_input_tokens = 100_000
    return ToolResultAndUserTurnAnchoredSummarizationCompactionStrategy(
        tokenizer=TOKENIZER,
        tool_results=base.tool_results,
        user_turns=base.user_turns,
        harder_attempts=0,
        record_budget_share=share,
        chain_seconds=None,
        **kwargs,
    )


@pytest.mark.parametrize(("step", "accepted_bar"), [(0.05, 0.75), (0.03, 0.74), (0.11, 0.7)])
async def test_splits_precede_relaxation_and_cached_parent_costs_no_request(
    caplog: pytest.LogCaptureFixture, step: float, accepted_bar: float
) -> None:
    summarizer = _Summarizer(merge=_lossy)
    strategy = _configured(summarizer, merge_coverage_floor=0.7, merge_coverage_step=step)
    messages = _records()
    with caplog.at_level(logging.WARNING):
        assert await strategy._merge_records(messages)
    assert len(summarizer.merges) == 3
    assert strategy.records_merged == 1
    assert strategy.current_merge_coverage_share == accepted_bar
    assert strategy.record_consolidations_by_coverage == {accepted_bar: 1}
    assert strategy.record_values_omitted == 4
    assert len(_sent_values(messages)) == 12
    assert not strategy.session_state()["merge_coverage"]["answers"]
    assert "values_total=16 kept=12 missing=4 required=12" in caplog.text
    assert not _VALUE.search(caplog.text)
    assert strategy.tool_results.coverage_share == 0.8


async def test_default_decisions_and_requests_match_disabled_tolerance() -> None:
    results: list[tuple[list[tuple[str, str]], set[str], int]] = []
    for options in ({}, {"merge_coverage_share": 0.8, "merge_coverage_floor": 0.8}):
        summarizer = _Summarizer(merge=_lossy)
        strategy = _configured(summarizer, **options)
        messages = _records()
        assert not await strategy._merge_records(messages)
        assert not await strategy._merge_records(messages)
        assert not strategy.session_state()["merge_coverage"]["answers"]
        results.append((summarizer.requests, _sent_values(messages), strategy.records_merged))
    assert results[0] == results[1]
    assert len(results[0][0]) == 3
    assert len(results[0][1]) == 16


async def test_separate_bar_applies_to_merges_and_rewrites_without_changing_grounding() -> None:
    strategy = _configured(merge_coverage_share=0.75)
    messages = _records()
    groups = consolidatable_record_groups(messages)
    bodies = record_bodies(messages, groups)
    assert strategy._value_coverage(bodies, _lossy(" ".join(bodies))) == (16, 12, 12)
    assert strategy.tool_results.coverage_share == 0.8
    assert await strategy._merge_records(messages)
    assert strategy.record_consolidations_by_coverage == {0.75: 1}


async def test_bar_resets_when_records_fit_and_never_relaxes_without_share_pressure() -> None:
    strategy = _configured(share=0.2, merge_coverage_floor=0.5)
    messages = _records()
    assert await strategy._merge_records(messages)
    assert strategy.current_merge_coverage_share == 0.8
    assert strategy.record_consolidations_by_coverage == {0.75: 1}
    for share in (None, 1.0):
        no_pressure = _configured(share=share, merge_coverage_floor=0.5)
        messages = _records(2)
        if share is not None:
            no_pressure._reshape_records(messages)
            no_pressure.max_input_tokens = 100_000
        assert not await no_pressure._merge_records(messages)
        assert no_pressure.current_merge_coverage_share == 0.8


async def test_below_floor_stays_refused_and_survives_json_restore() -> None:
    summarizer = _Summarizer(merge=_lossy)
    strategy = _configured(summarizer, merge_coverage_floor=0.76)
    messages = _records()
    assert not await strategy._merge_records(messages)
    assert strategy.current_merge_coverage_share == 0.76
    state = json.loads(json.dumps(strategy.session_state()))
    assert len(state["merge_coverage"]["answers"]) == 3
    restored = _configured(summarizer, merge_coverage_floor=0.76)
    restored.restore_session_state(state)
    assert restored.current_merge_coverage_share == 0.76
    assert not await restored._merge_records(messages)
    assert len(summarizer.merges) == 3
    assert restored.session_state()["merge_coverage"] == state["merge_coverage"]


async def test_saved_answers_are_reused_after_restore_without_asking_again() -> None:
    summarizer = _Summarizer(merge=_lossy)
    strategy = _configured(summarizer, merge_coverage_floor=0.7)
    messages = _records()
    assert not await strategy._merge_batches(messages, None, strategy.record_budget_share)
    state = json.loads(json.dumps(strategy.session_state()))
    restored = _configured(summarizer, merge_coverage_floor=0.7)
    restored.restore_session_state(state)
    assert await restored._merge_records(messages)
    assert len(summarizer.merges) == 3
    assert restored.record_values_omitted == 4


@pytest.mark.parametrize("change", ["identity", "body", "prompt", "policy"])
async def test_stale_answers_are_discarded(change: str) -> None:
    strategy = _configured(merge_coverage_floor=0.7)
    messages = _records(2)
    await strategy._merge_batches(messages, None, strategy.record_budget_share)
    assert strategy.session_state()["merge_coverage"]["answers"]
    if change == "identity":
        messages[1].message_id = "replacement"
    elif change == "body":
        messages[1].contents[0].arguments = '{"values":"changed"}'
    elif change == "prompt":
        strategy.merge_prompt += " Keep every identifier."
    else:
        restored = _configured(merge_coverage_floor=0.6)
        restored.restore_session_state(strategy.session_state())
        strategy = restored
    strategy._refresh_merge_coverage(messages)
    assert not strategy.session_state()["merge_coverage"]["answers"]
    assert strategy.current_merge_coverage_share == 0.8


async def test_accepted_halves_supersede_lossy_parent_without_lowering() -> None:
    summarizer = _Summarizer(
        merge=lambda body: (
            _lossy(body)
            if len(set(_VALUE.findall(body))) > 8
            else " ".join(sorted(set(_VALUE.findall(body))))
        )
    )
    strategy = _configured(summarizer, merge_coverage_floor=0.5)
    messages = _records()
    assert await strategy._merge_records(messages)
    assert strategy.current_merge_coverage_share == 0.8
    assert strategy.records_merged == 2
    assert not strategy.session_state()["merge_coverage"]["answers"]
    assert len(_sent_values(messages)) == 16


@pytest.mark.parametrize(
    "kwargs",
    [
        {"merge_coverage_share": -0.1},
        {"merge_coverage_share": float("nan")},
        {"merge_coverage_share": True},
        {"merge_coverage_floor": 0.9},
        {"merge_coverage_floor": -0.1},
        {"merge_coverage_step": 0},
        {"merge_coverage_step": float("inf")},
        {"merge_coverage_step": True},
        {"merge_answer_cache_bytes": -1},
        {"merge_answer_cache_bytes": True},
    ],
)
def test_invalid_settings(kwargs: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match="merge_"):
        _configured(**kwargs)


def test_cache_byte_bound_fifo_and_restore_with_smaller_limit() -> None:
    first = LossyMerge("first", 2, "\u00e9" * 50, 10, 7)
    second = LossyMerge("second", 2, "answer", 10, 7)
    cache = MergeCoverage(0.8, 0.6, 0.05, first.size + second.size - 1)
    cache.bind("prompt")
    cache.remember(first)
    cache.remember(second)
    assert cache.answers == [second]
    assert len(json.dumps(cache.snapshot()["answers"]).encode("utf-8")) <= cache.cache_bytes
    restored = MergeCoverage(0.8, 0.6, 0.05, second.size - 1)
    restored.bind("prompt")
    restored.restore(cache.snapshot())
    assert not restored.answers


def test_decimal_steps_accept_the_exact_value_ratio_and_clamp_at_floor() -> None:
    coverage = MergeCoverage(0.8, 0.53, 0.05, 1000)
    for _ in range(5):
        assert coverage.lower()
    assert coverage.current == 0.55
    assert coverage.required(100) == 55
    assert coverage.lower()
    assert coverage.current == 0.53
    assert not coverage.lower()


def test_next_step_uses_the_earliest_qualifying_answer_and_keeps_cache_order() -> None:
    coverage = MergeCoverage(0.9, 0.6, 0.02, 1000)
    answers = [LossyMerge("older", 2, "first", 4, 3), LossyMerge("newer", 2, "second", 6, 5)]
    for answer in answers:
        coverage.remember(answer)
    assert coverage.lower_to_next_answer()
    assert coverage.current == 0.82
    assert coverage.required(6) == 5
    assert coverage.required(4) == 4
    assert coverage.answers == answers


@pytest.mark.parametrize("retain_below_floor", [False, True])
def test_next_step_without_a_qualifying_answer_lowers_directly_to_floor(
    retain_below_floor: bool,
) -> None:
    coverage = MergeCoverage(0.8, 0.7, 1e-16, 1000)
    if retain_below_floor:
        coverage.remember(LossyMerge("source", 2, "answer", 10, 6))
    assert coverage.lower_to_next_answer()
    assert coverage.current == 0.7
    assert not coverage.lower_to_next_answer()


async def test_empty_history_resets_bar_and_caches() -> None:
    strategy = _configured(merge_coverage_floor=0.7)
    await strategy._merge_records(_records())
    assert strategy.current_merge_coverage_share == 0.75
    assert not await strategy([])
    assert strategy.current_merge_coverage_share == 0.8


async def test_accepted_edit_replays_after_restart_without_recounting_loss() -> None:
    summarizer = _Summarizer(merge=_lossy)
    strategy = _configured(summarizer, share=0.2, merge_coverage_floor=0.7)
    messages = _records()
    original = copy.deepcopy(messages)
    try:
        await strategy(messages)
    except CompactionBudgetExceeded:
        pytest.fail("The cached merge should fit the sending budget.")
    restored = _configured(summarizer, share=0.2, merge_coverage_floor=0.7)
    restored.restore_session_state(json.loads(json.dumps(strategy.session_state())))
    await restored(original)
    assert _sent_values(original) == _sent_values(messages)
    assert len(summarizer.merges) == 3
    assert restored.record_values_omitted == 0


@pytest.mark.parametrize("stop", ["deadline", "failure", "rate_limit"])
async def test_interrupted_split_never_lowers_bar(
    stop: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = 0.0
    monkeypatch.setattr(composed_module, "monotonic", lambda: now)

    class Interrupted(_Summarizer):
        async def get_response(self, messages: list[Message], **kwargs: Any) -> ChatResponse:
            nonlocal now
            if self.requests:
                if stop == "rate_limit":
                    raise SummaryRateLimited
                raise RuntimeError("summary unavailable")
            response = await super().get_response(messages, **kwargs)
            if stop == "deadline":
                now = 2.0
            return response

    summarizer = Interrupted(merge=_lossy)
    strategy = _configured(summarizer, merge_coverage_floor=0.5)
    token = strategy._chain_deadline.set(1.0)
    try:
        if stop == "deadline":
            with pytest.raises(_ChainTimeSpent):
                await strategy._merge_records(_records())
        else:
            assert not await strategy._merge_records(_records())
    finally:
        strategy._chain_deadline.reset(token)
    assert strategy.current_merge_coverage_share == 0.8
    assert strategy.session_state()["merge_coverage"]["answers"]
    assert strategy.session_state()["pending_merges"]


async def test_probe_limit_does_not_relax(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(composed_module, "_MAX_BATCH_PROBES", 1)
    strategy = _configured(merge_coverage_floor=0.5)
    assert not await strategy._merge_records(_records())
    assert strategy.current_merge_coverage_share == 0.8


@pytest.mark.parametrize("kind", ["truncated", "not_smaller", "anonymous", "held"])
async def test_ineligible_answers_cannot_enter_cache(kind: str) -> None:
    strategy = _configured(merge_coverage_floor=0.5, record_merge_split_depth=0)
    messages = _records(2)
    if kind == "anonymous":
        for message in messages:
            message.message_id = None
            for content in message.contents:
                if content.type in {"function_call", "function_result"}:
                    content.call_id = None
    elif kind == "held":
        set_preserved(messages[1], preserved=True, reason="host")
    groups = consolidatable_record_groups(messages)
    if groups:
        answer = _RecordAnswer(
            "word " * 20_000 if kind == "not_smaller" else "VAL-000-0",
            incomplete=kind == "truncated",
        )
        await strategy._consolidate(
            messages,
            groups,
            prompt=strategy.merge_prompt,
            transcript="records",
            answer=answer,
            remember_lossy=True,
        )
    assert not strategy.session_state()["merge_coverage"]["answers"]


async def test_new_external_hold_invalidates_saved_candidate() -> None:
    strategy = _configured(merge_coverage_floor=0.5)
    messages = _records(2)
    await strategy._merge_batches(messages, None, strategy.record_budget_share)
    assert strategy.session_state()["merge_coverage"]["answers"]
    set_preserved(messages[1], preserved=True, reason="host")
    strategy._refresh_merge_coverage(messages)
    assert not strategy.session_state()["merge_coverage"]["answers"]
    assert len(_sent_values(messages)) == 8


@pytest.mark.parametrize("count", [8, 16])
async def test_cache_refresh_fingerprints_messages_once_and_windows_once_per_size(
    count: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    strategy = _configured(merge_coverage_floor=0.7)
    messages = _records(count)
    await strategy._merge_batches(messages, None, strategy.record_budget_share)
    answers = list(strategy._merge_coverage.answers)
    assert len(answers) == 7
    source_calls = message_calls = 0
    source_key, message_key = replay_module.source_key, replay_module._message_key

    def source(*args: Any, **kwargs: Any) -> str | None:
        nonlocal source_calls
        source_calls += 1
        return source_key(*args, **kwargs)

    def message(value: Message) -> str:
        nonlocal message_calls
        message_calls += 1
        return message_key(value)

    monkeypatch.setattr(composed_module, "source_key", source)
    monkeypatch.setattr(replay_module, "_message_key", message)
    matches = strategy._lossy_merge_matches(messages)
    assert len(matches) == len(answers)
    assert source_calls == sum(count - size + 1 for size in {answer.count for answer in answers})
    assert message_calls == count * 2


async def test_cached_answer_with_ambiguous_source_window_is_discarded() -> None:
    strategy = _configured(merge_coverage_floor=0.7)
    messages = [
        build_record_message(" ".join(_values(index)) + " detail" * 100) for index in range(2)
    ]
    for index, message in enumerate(messages):
        message.message_id = f"record-{index}"
        message.additional_properties[STANDS_IN_KEY] = f"source-{index}"
    _size(messages)
    await strategy._merge_batches(messages, None, strategy.record_budget_share)
    assert len(strategy._merge_coverage.answers) == 1
    copies = copy.deepcopy(messages)
    for index, message in enumerate(copies):
        message.message_id = f"copy-{index}"
    messages.extend(copies)
    _size(messages)
    assert len(consolidatable_record_groups(messages)) == 4
    assert not strategy._lossy_merge_matches(messages)
    assert not strategy._merge_coverage.answers


async def test_background_retains_answers_but_foreground_alone_relaxes() -> None:
    summarizer = _Summarizer(merge=_lossy)
    strategy = _configured(summarizer, merge_coverage_floor=0.7)
    messages = _records()
    prepared: list[_PreparedMerge] = []

    def ready(sources: str, count: int, transcript: str, answer: _RecordAnswer) -> None:
        prepared.append(_PreparedMerge(sources, count, transcript, answer, strategy.merge_prompt))

    worker = strategy._record_worker(1000, ready, lambda: True)
    await worker._prepare_record_merges(copy.deepcopy(messages))
    assert len(prepared) == 3
    assert worker.current_merge_coverage_share == strategy.current_merge_coverage_share == 0.8
    assert not strategy.session_state()["merge_coverage"]["answers"]
    assert not await strategy._apply_record_answers(messages, prepared)
    strategy._remember_record_search(worker)
    assert await strategy._merge_records(messages)
    assert len(summarizer.merges) == 3
    assert strategy.current_merge_coverage_share == 0.75
    assert worker.current_merge_coverage_share == 0.8


@pytest.mark.parametrize("step", [0.00001, 1e-16])
@pytest.mark.parametrize("retain_answers", [False, True])
async def test_background_relaxation_does_not_rescan_history_for_empty_steps(
    step: float, retain_answers: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    summarizer = _Summarizer(merge=_lossy)
    strategy = _configured(
        summarizer,
        share=0.00001,
        merge_coverage_floor=0.7,
        merge_coverage_step=step,
        merge_answer_cache_bytes=1_048_576 if retain_answers else 0,
    )
    strategy.max_input_tokens = 100_000
    messages = _records()
    work = _RecordMergeWork()
    work.bind(strategy, None)
    work.remember(messages, strategy.max_input_tokens)
    work.start(tool_reply())
    try:
        await running(work)
        assert not strategy._over(messages)
        assert len(summarizer.merges) == 3
        original = strategy._records_over_share
        checks = 0

        def over_share(history: list[Message]) -> bool:
            nonlocal checks
            checks += 1
            assert checks <= 20, "empty coverage steps repeatedly scan the foreground history"
            return original(history)

        monkeypatch.setattr(strategy, "_records_over_share", over_share)
        assert await strategy._last_resort(messages) is retain_answers
        assert strategy.current_merge_coverage_share == (0.75 if retain_answers else 0.7)
        assert strategy.record_values_omitted == (4 if retain_answers else 0)
        assert len(summarizer.merges) == 3
    finally:
        await work.close()


@pytest.mark.parametrize("restore", [False, True])
@pytest.mark.parametrize("policy", ["shed", "raise"])
async def test_cached_answers_follow_over_budget_reasoning_policy(
    restore: bool, policy: str
) -> None:
    summarizer = _Summarizer(merge=_lossy)
    strategy = _configured(summarizer, merge_coverage_floor=0.7)
    messages = _records()
    strategy._reshape_records(messages)
    assert not await strategy._merge_batches(messages, None, strategy.record_budget_share)
    assert len(summarizer.merges) == 3
    if restore:
        state = json.loads(json.dumps(strategy.session_state()))
        strategy = _configured(summarizer, merge_coverage_floor=0.7)
        strategy.restore_session_state(state)
    strategy.overflow_policy = policy
    messages.append(
        Message("assistant", [Content.from_text_reasoning(text="Compare the records.")])
    )
    _size(messages)
    assert strategy._over(messages)
    await strategy._last_resort(messages)
    assert strategy.records_merged == (1 if policy == "shed" else 0)
    assert len(summarizer.merges) == 3
    assert len(_sent_values(messages)) == (12 if policy == "shed" else 16)


@pytest.mark.parametrize(
    "change", [None, "sources", "held", "prompt", "coverage", "split_depth", "limit"]
)
@pytest.mark.parametrize("budget", [1000, 100_000])
async def test_completed_background_sweep_relaxes_only_its_current_sources_and_policy(
    change: str | None,
    budget: int,
) -> None:
    summarizer = _Summarizer(merge=_lossy)
    strategy = _configured(summarizer, share=0.00001, merge_coverage_floor=0.7)
    strategy.max_input_tokens = budget
    messages = _records()
    work = _RecordMergeWork()
    work.bind(strategy, None)
    work.remember(messages, budget)
    work.start(tool_reply())
    try:
        await running(work)
        assert work._worker is not None
        assert work._worker.current_merge_coverage_share == 0.8
        assert len(summarizer.merges) == 3
        if change == "sources":
            messages[-1:-1] = _record_pair(9)
        elif change == "held":
            for group in consolidatable_record_groups(messages):
                for message in messages[group["start_index"] : group["end_index"] + 1]:
                    set_preserved(message, preserved=True, reason="host")
        elif change == "prompt":
            strategy.merge_prompt += " Preserve attribution."
        elif change == "coverage":
            strategy.tool_results.coverage_share = 0.9
        elif change == "split_depth":
            strategy.record_merge_split_depth += 1
        elif change == "limit":
            strategy.user_turns.max_summary_input_tokens -= 1
        _size(messages)
        await strategy._last_resort(messages)
        assert strategy.records_merged == (1 if change is None else 0)
        assert strategy.current_merge_coverage_share == (
            0.75 if change is None else 0.9 if change == "coverage" else 0.8
        )
        assert len(summarizer.merges) == 3
        assert work.defer_merges
        assert work._task is None
    finally:
        await work.close()


@pytest.mark.parametrize("stop", ["deadline", "failure", "rate_limit", "probe_limit"])
async def test_interrupted_background_sweep_does_not_authorize_relaxation(
    stop: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = 0.0
    monkeypatch.setattr(composed_module, "monotonic", lambda: now)

    class Interrupted(_Summarizer):
        async def get_response(self, messages: list[Message], **kwargs: Any) -> ChatResponse:
            nonlocal now
            if self.requests and stop in {"failure", "rate_limit"}:
                if stop == "rate_limit":
                    raise SummaryRateLimited
                raise RuntimeError("summary unavailable")
            response = await super().get_response(messages, **kwargs)
            if stop == "deadline":
                now = 2.0
            return response

    strategy = _configured(Interrupted(merge=_lossy), merge_coverage_floor=0.7)
    strategy.chain_seconds = 1.0
    if stop == "probe_limit":
        monkeypatch.setattr(composed_module, "_MAX_BATCH_PROBES", 1)
    messages = _records()
    work = _RecordMergeWork()
    work.bind(strategy, None)
    work.remember(messages, 1000)
    work.start(tool_reply())
    try:
        await running(work)
        await strategy._last_resort(messages)
        assert strategy.current_merge_coverage_share == 0.8
        assert strategy.records_merged == 0
    finally:
        await work.close()


async def test_snapshot_scores_do_not_override_live_coverage_check() -> None:
    strategy = _configured(merge_coverage_floor=0.7)
    messages = _records(2)
    await strategy._merge_batches(messages, None, strategy.record_budget_share)
    state = strategy.session_state()
    state["merge_coverage"]["answers"][0]["text"] = "only one VAL-000-0"
    state["merge_coverage"]["answers"][0]["kept"] = 8
    strategy.restore_session_state(state)
    assert not await strategy._merge_records(messages)
    assert strategy.record_values_omitted == 0
    assert len(_sent_values(messages)) == 8


async def test_rewrite_uses_separate_bar_without_retaining_a_refused_rewrite() -> None:
    for bar, accepted in ((0.8, False), (0.75, True)):
        strategy = _configured(merge_coverage_share=bar, merge_coverage_floor=0.5)
        strategy.tool_results.coverage_share = 1.0
        messages = _records(2)
        groups = consolidatable_record_groups(messages)
        body = " ".join(record_bodies(messages, groups))
        outcome = await strategy._consolidate(
            messages,
            groups,
            prompt=strategy.merge_prompt,
            transcript=body,
            min_gain_fraction=0.1,
            answer=_RecordAnswer(_lossy(body)),
        )
        assert (outcome == "accepted") == accepted
        assert not strategy.session_state()["merge_coverage"]["answers"]
        assert strategy.record_values_omitted == (2 if accepted else 0)
