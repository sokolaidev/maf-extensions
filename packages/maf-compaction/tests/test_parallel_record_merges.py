"""Parallel requests retain ordered edits, source guards and run-scoped cancellation."""

from __future__ import annotations

import asyncio
import weakref
from copy import deepcopy

import pytest
from agent_framework import Content, Message
from agent_framework._compaction import EXCLUDED_KEY
from test_background_record_merges import running, tool_reply
from test_record_budget import _VALUE, _body, _history, _sent, _sent_values, _strategy, _Summarizer
from test_record_merge_limits import _batch_size
from test_summary_limiter import Clock, RateLimit

from maf_compaction import SummaryRequestLimiter
from maf_compaction import _composed as module
from maf_compaction import _summary_limiter as limiter_module
from maf_compaction._composed import _ChainTimeSpent, _RecordMergeWork
from maf_compaction._offload import steps_inline
from maf_compaction._preserve import set_preserved
from maf_compaction._record_replay import source_key
from maf_compaction._summary_limiter import SummaryRateLimited
from maf_compaction._toolsummary import build_record_message, consolidatable_record_groups

pytestmark = pytest.mark.anyio


def configured(model, *, parallel=3, limiter=None, cap=1_200):
    strategy = _strategy(model, budget=100_000, share=0.001, cap=cap)
    strategy.user_turns.max_summary_input_tokens = 12_000
    strategy.user_turns.summary_limiter = limiter or SummaryRequestLimiter(
        10_000_000, max_parallel_summary_requests=parallel
    )
    return strategy


class Ordered(_Summarizer):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.started = []
        self.finished = []
        self.release = [asyncio.Event() for _ in range(3)]
        self.complete = [asyncio.Event() for _ in range(3)]
        self.full = asyncio.Event()

    async def get_response(self, messages, **kwargs):
        index = len(self.started)
        self.started.append(messages[-1].text)
        if len(self.started) == 3:
            self.full.set()
        if index < 3:
            await self.release[index].wait()
        response = await super().get_response(messages, **kwargs)
        self.finished.append(index)
        if index < 3:
            self.complete[index].set()
        return response

    async def finish_reversed(self):
        await asyncio.wait_for(self.full.wait(), 2)
        for index in (2, 1, 0):
            self.release[index].set()
            await asyncio.wait_for(self.complete[index].wait(), 2)


def unidentified_history(kind):
    messages = _history(18)
    if kind == "records":
        records = [build_record_message(_body(index)) for index in range(18)]
        for record in records:
            record.message_id = None
        messages[2:-1] = records
    else:
        messages.insert(
            -1,
            Message(
                "assistant",
                [Content.from_text_reasoning(text="retained")],
                additional_properties={EXCLUDED_KEY: True},
            ),
        )
    assert source_key(messages, consolidatable_record_groups(messages)) is None
    return messages


@pytest.mark.parametrize("kind", ["records", "reasoning"])
@pytest.mark.parametrize("lossy", [False, True])
async def test_unidentified_sources_fall_back_to_sequential_merges(kind, lossy):
    messages = unidentified_history(kind)
    sequential_messages = deepcopy(messages)
    original_values = _sent_values(messages)

    def answer(body):
        values = sorted(set(_VALUE.findall(body)))
        return values[0] if lossy and _batch_size(body) > 2 else " ".join(values)

    model, sequential_model = _Summarizer(merge=answer), _Summarizer(merge=answer)
    strategy, sequential = configured(model), configured(sequential_model, parallel=1)
    assert await strategy._merge_batches(messages, None, 0.001)
    assert await sequential._merge_batches(sequential_messages, None, 0.001)
    assert _sent(messages) == _sent(sequential_messages)
    assert _sent_values(messages) == original_values
    assert strategy.records_merged == sequential.records_merged
    assert model.requests == sequential_model.requests
    assert strategy.user_turns.summary_limiter.stats.peak_concurrency == 1
    assert not strategy._pending_merges


@pytest.mark.parametrize("kind", ["records", "reasoning"])
async def test_background_leaves_unidentified_sources_for_foreground(kind):
    model = _Summarizer()
    strategy = configured(model)
    messages = unidentified_history(kind)
    work = _RecordMergeWork()
    work.bind(strategy, None)
    work.remember(messages, 100_000)
    await work.start(tool_reply())
    await asyncio.wait_for(running(work), 2)
    assert not model.requests
    assert not work._ready
    await work.close()
    assert await strategy._merge_batches(messages, None, 0.001)
    assert _sent_values(messages) == _sent_values(unidentified_history(kind))


@pytest.mark.parametrize("lossy", [False, True])
@pytest.mark.parametrize("recycle_ids", [False, True])
async def test_out_of_order_answers_match_sequential_edits_and_replay(
    lossy, recycle_ids, monkeypatch
):
    if recycle_ids:
        identities = []

        def recycled_id(value):
            if not isinstance(value, Message):
                return id(value)
            for index, reference in enumerate(identities):
                if reference() is value:
                    return index
            for index, reference in enumerate(identities):
                if reference() is None:
                    identities[index] = weakref.ref(value)
                    return index
            identities.append(weakref.ref(value))
            return len(identities) - 1

        monkeypatch.setattr(module, "id", recycled_id, raising=False)
    messages = _history(18)
    sequential_messages = deepcopy(messages)

    def answer(body):
        values = sorted(set(_VALUE.findall(body)))
        return values[0] if lossy and _batch_size(body) > 2 else " ".join(values)

    model = Ordered(merge=answer)
    strategy = configured(model)
    sequential = _strategy(_Summarizer(merge=answer), budget=100_000, share=0.001)
    task = asyncio.create_task(strategy._merge_batches(messages, None, 0.001))
    await model.finish_reversed()
    assert await task
    assert model.finished[:3] == [2, 1, 0]
    assert await sequential._merge_batches(sequential_messages, None, 0.001)
    assert _sent(messages) == _sent(sequential_messages)
    assert strategy.records_merged == sequential.records_merged
    assert strategy.session_state()["record_order"] == sequential.session_state()["record_order"]
    assert strategy.user_turns.summary_limiter.stats.peak_concurrency == 3
    assert _sent_values(messages) == _sent_values(_history(18))
    restored = configured(_Summarizer())
    restored.restore_session_state(strategy.session_state())
    replay = _history(18)
    restored._keep_decisions(replay)
    assert _sent(replay) == _sent(messages)


@pytest.mark.parametrize("change", ["payload", "preserved", "prompt", "reasoning"])
async def test_stale_parallel_answers_are_discarded(change):
    model = Ordered()
    strategy = configured(model)
    messages = _history(18)
    task = asyncio.create_task(strategy._merge_batches(messages, None, 0.001))
    await asyncio.wait_for(model.full.wait(), 2)
    if change == "payload":
        messages[3].contents[0].result = "changed source"
    elif change == "preserved":
        set_preserved(messages[3], preserved=True, reason="host hold")
    elif change == "prompt":
        strategy.merge_prompt += " Changed."
    else:
        messages.insert(4, Message("assistant", [Content.from_text_reasoning(text="retained")]))
    for event in model.release:
        event.set()
    await task
    assert all("VAL-000-0" not in str(edit) for edit in strategy._kept_records)
    assert strategy.user_turns.summary_limiter.stats.current_concurrency == 0


async def test_independent_refused_halves_run_together():
    entered = asyncio.Event()
    release = asyncio.Event()

    class Split(_Summarizer):
        active = peak = 0

        async def get_response(self, messages, **kwargs):
            if _batch_size(messages[-1].text) == 2:
                self.active += 1
                self.peak = max(self.peak, self.active)
                if self.active == 2:
                    entered.set()
                await release.wait()
                self.active -= 1
            return await super().get_response(messages, **kwargs)

    model = Split(
        merge=lambda body: (
            _VALUE.findall(body)[0]
            if _batch_size(body) > 2
            else " ".join(sorted(set(_VALUE.findall(body))))
        )
    )
    strategy = configured(model, cap=10_000)
    messages = _history(4)
    task = asyncio.create_task(strategy._merge_batches(messages, None, 0.001))
    await asyncio.wait_for(entered.wait(), 2)
    release.set()
    assert await task
    assert model.peak == 2
    assert strategy.records_merged == 2
    assert not strategy._pending_merges
    assert _sent_values(messages) == _sent_values(_history(4))


@pytest.mark.parametrize("background", [False, True])
async def test_cancellation_drains_every_request(background):
    entered = asyncio.Event()

    class Blocking(_Summarizer):
        started = cancelled = 0

        async def get_response(self, messages, **kwargs):
            self.started += 1
            if self.started == 3:
                entered.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                self.cancelled += 1
                raise

    model = Blocking()
    strategy = configured(model)
    messages = _history(18)
    work = _RecordMergeWork()
    if background:
        work.bind(strategy, None)
        work.remember(messages, 100_000)
        await work.start(tool_reply())
        task = running(work)
    else:
        task = asyncio.create_task(strategy._merge_batches(messages, None, 0.001))
    await asyncio.wait_for(entered.wait(), 2)
    if background:
        await asyncio.wait_for(work.close(), 2)
    else:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert task.done()
    assert model.started == model.cancelled == 3
    assert not work._ready
    assert strategy.records_merged == 0
    stats = strategy.user_turns.summary_limiter.stats
    assert stats.requests_started == stats.requests_finished == 3
    assert stats.current_concurrency == 0


async def test_shared_limiter_bounds_two_stacks_and_user_summaries():
    model = Ordered()
    limiter = SummaryRequestLimiter(10_000_000, max_parallel_summary_requests=3)
    first, second = configured(model, limiter=limiter), configured(model, limiter=limiter)
    tasks = [
        asyncio.create_task(first._merge_batches(_history(18), None, 0.001)),
        asyncio.create_task(second._merge_batches(_history(18), None, 0.001)),
        asyncio.create_task(first.user_turns._generate("Some requirements", prompt="Summarize")),
    ]
    await asyncio.wait_for(model.full.wait(), 2)
    assert limiter.stats.current_concurrency == len(model.started) == 3
    for event in model.release:
        event.set()
    await asyncio.gather(*tasks)
    assert limiter.stats.peak_concurrency == 3
    assert limiter.stats.requests_started == limiter.stats.requests_finished == len(model.started)


async def test_rate_limited_batch_remains_eligible_without_failure_or_option_retry():
    class Limited(_Summarizer):
        limited = True

        async def get_response(self, messages, **kwargs):
            if self.limited:
                self.limited = False
                raise RateLimit()
            return await super().get_response(messages, **kwargs)

    model = Limited()
    strategy = configured(model)
    strategy.user_turns.retry_without_summary_options = True
    messages = _history(18)
    await strategy._merge_batches(messages, None, 0.001)
    assert strategy.records_merged > 0
    assert strategy.record_summary_failures == strategy.record_merges_rejected == 0
    assert not strategy._refused_merges
    assert not strategy.user_turns._summary_options_disabled
    assert strategy.user_turns.summary_limiter.stats.rate_limit_events == 1
    assert await strategy._merge_batches(messages, None, 0.001)
    assert "VAL-000-0" in str(strategy._kept_records)


async def test_ordinary_failure_ends_sweep_without_starting_another_wave():
    class Failing(_Summarizer):
        started = 0

        async def get_response(self, messages, **kwargs):
            self.started += 1
            await asyncio.sleep(0)
            raise ValueError("unavailable")

    model = Failing()
    strategy = configured(model)
    assert not await strategy._merge_batches(_history(18), None, 0.001)
    assert model.started == strategy.record_summary_failures == 3
    assert not strategy._refused_merges


async def test_rate_limited_split_retains_other_admitted_batch_answers():
    class LimitedSplit(_Summarizer):
        async def get_response(self, messages, **kwargs):
            body = messages[-1].text
            if "VAL-000-0" in body and _batch_size(body) <= 2:
                raise RateLimit()
            return await super().get_response(messages, **kwargs)

    model = LimitedSplit(
        merge=lambda body: (
            _VALUE.findall(body)[0]
            if "VAL-000-0" in body and _batch_size(body) > 2
            else " ".join(sorted(set(_VALUE.findall(body))))
        )
    )
    strategy = configured(model)
    messages = _history(8)
    assert await strategy._merge_batches(messages, None, 0.001)
    assert "VAL-007-0" in str(strategy._kept_records)
    assert strategy.record_summary_failures == 0
    assert strategy.user_turns.summary_limiter.stats.rate_limit_events == 1
    assert strategy._pending_merges


async def test_inflight_answers_apply_after_chain_admission_deadline(monkeypatch):
    now = 100.0
    monkeypatch.setattr(module, "monotonic", lambda: now)
    model = Ordered()
    strategy = configured(model)
    messages = _history(18)
    token = strategy._chain_deadline.set(101)
    try:
        task = asyncio.create_task(strategy._merge_batches(messages, None, 0.001))
        await asyncio.wait_for(model.full.wait(), 2)
        now = 102
        for event in model.release:
            event.set()
        with pytest.raises(_ChainTimeSpent):
            await task
    finally:
        strategy._chain_deadline.reset(token)
    assert len(model.started) == strategy.records_merged == 3
    assert strategy.user_turns.summary_limiter.stats.requests_finished == 3


async def test_user_rate_limit_does_not_increment_failure_backoff():
    class Limited(_Summarizer):
        async def get_response(self, messages, **kwargs):
            raise RateLimit()

    strategy = configured(Limited())
    strategy.user_turns.retry_without_summary_options = True
    assert await strategy.user_turns._summarize("Some requirements", prompt="Summarize") is None
    assert strategy.user_turns.user_summary_failures == 0
    assert strategy.user_turns.summary_limiter.stats.requests_started == 1


@pytest.mark.parametrize("chunked", [False, True])
async def test_retry_after_wait_timeout_skips_without_failure_backoff(monkeypatch, chunked):
    clock = Clock()
    monkeypatch.setattr(limiter_module, "monotonic", clock)
    model = _Summarizer()
    strategy = configured(model).user_turns
    strategy.summary_timeout_seconds = 30
    strategy.max_consecutive_summary_failures = 1
    strategy.summary_backoff_seconds = 60
    strategy.retry_without_summary_options = True
    strategy.max_summary_input_tokens = 1_000
    units = [f"{index}: " + "requirements " * 100 for index in range(8)] if chunked else None
    transcript = "\n".join(units) if units else "Some requirements"
    limiter = strategy.summary_limiter
    limiter.configure_request(12_000, {"max_tokens": 1_200})
    with pytest.raises(SummaryRateLimited):
        async with limiter.request():
            raise RateLimit("120")
    # Make the Retry-After wait the first suspension under the short deadline.
    with steps_inline(), monkeypatch.context() as blocked:
        blocked.setattr(strategy, "summary_timeout_seconds", 0.01)
        assert await strategy._summarize(transcript, prompt="Summarize", units=units) is None
    assert strategy.user_summary_skips == 1
    assert strategy.user_summary_failures == 0
    assert strategy.session_state()["summary_failures"] == 0
    assert strategy.session_state()["summary_backoff_until"] == 0
    assert not strategy._summary_options_disabled
    assert not model.requests
    assert limiter.stats.requests_started == limiter.stats.requests_finished == 1
    clock.now += 120
    assert (
        await strategy._summarize(transcript, prompt="Summarize", units=units) == "Short summary."
    )
    assert len(model.requests) > 1 if chunked else len(model.requests) == 1


@pytest.mark.parametrize("cancel", [False, True])
async def test_capacity_wait_timeout_or_cancellation_is_not_a_provider_failure(cancel):
    model = _Summarizer()
    strategy = configured(model, parallel=1).user_turns
    strategy.summary_timeout_seconds = 60 if cancel else 0.01
    strategy.max_consecutive_summary_failures = 1
    strategy.summary_backoff_seconds = 60
    limiter = strategy.summary_limiter
    async with limiter.request():
        task = asyncio.create_task(strategy._summarize("Some requirements", prompt="Summarize"))
        if cancel:
            await asyncio.sleep(0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            assert await task is None
    assert strategy.user_summary_skips == (0 if cancel else 1)
    assert strategy.user_summary_failures == 0
    assert strategy.session_state()["summary_backoff_until"] == 0
    assert not model.requests
    assert limiter.stats.requests_started == limiter.stats.requests_finished == 1
