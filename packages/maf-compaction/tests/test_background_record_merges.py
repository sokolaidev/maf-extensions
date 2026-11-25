"""Background answers require foreground validation against the session's current records."""

import asyncio
import logging
from contextlib import suppress
from contextvars import ContextVar
from copy import deepcopy

import pytest
from agent_framework import (
    AgentMiddleware,
    AgentSession,
    CharacterEstimatorTokenizer,
    ChatResponse,
    ChatResponseUpdate,
    Content,
    Message,
    ResponseStream,
    create_harness_agent,
    tool,
)
from agent_framework._compaction import EXCLUDED_KEY, annotate_message_groups
from agent_framework.observability import OBSERVABILITY_SETTINGS
from test_record_budget import (
    _VALUE,
    _history,
    _sent,
    _sent_values,
    _strategy,
    _Summarizer,
    _values,
)
from test_record_merge_limits import _batch_size
from test_session import TOKENIZER, Model, build_stack

from maf_compaction import (
    CompactionBudgetExceeded,
    SessionCompactionProvider,
    SessionCompactionRegistry,
    SessionToolResultRecallMiddleware,
    SummaryRequestLimiter,
    ToolResultAndUserTurnAnchoredSummarizationCompactionStrategy,
    make_session_recall_tool,
    make_session_record_merge_middleware,
    observe_record_merges,
)
from maf_compaction import _composed as composed_module
from maf_compaction._composed import _RecordAnswer, _RecordMergeWork, _transcript
from maf_compaction._preserve import set_preserved
from maf_compaction._toolsummary import (
    _distinctive_tokens,
    consolidatable_record_groups,
    record_bodies,
)

pytestmark = pytest.mark.anyio


def running(work: _RecordMergeWork) -> asyncio.Task[None]:
    assert work._task is not None
    return work._task


def tool_reply():
    return [
        Message(
            "assistant",
            [Content.from_function_call(call_id="lookup", name="lookup", arguments={})],
            message_id="lookup-call",
        )
    ]


async def prepared(model=None):
    strategy = _strategy(model or _Summarizer(), budget=100_000, share=0.001, cap=None)
    work = _RecordMergeWork()
    work.bind(strategy, None)
    messages = _history(4)
    await strategy(messages)
    return strategy, work, messages


async def test_background_does_not_mutate_live_history_or_decisions():
    strategy, work, messages = await prepared()
    before = deepcopy(strategy.session_state()), deepcopy(messages)
    await work.start(tool_reply())
    await running(work)
    assert strategy.session_state() == before[0]
    assert [m.to_dict() for m in messages] == [m.to_dict() for m in before[1]]
    assert await strategy(messages)
    assert strategy.records_merged == 1
    assert _sent_values(messages) == {v for i in range(4) for v in _values(i)}
    accepted = _sent(messages)
    restored = _strategy(_Summarizer(), budget=100_000, share=0.001, cap=None)
    restored.restore_session_state(deepcopy(strategy.session_state()))
    original = _history(4)
    await restored(original)
    assert _sent(original) == accepted
    await work.close()


@pytest.mark.parametrize("change_gap", [False, True])
async def test_long_intervening_history_becomes_one_gap_and_is_revalidated(change_gap):
    strategy, work, messages = await prepared()
    gap = [Message("user", ["old payload"], message_id=f"gap-{i}") for i in range(1000)]
    for message in gap:
        message.additional_properties[EXCLUDED_KEY] = True
    messages[4:4] = gap
    await work.start(tool_reply())
    try:
        assert work._worker is not None
        snapshot = work._worker._background_records
        assert snapshot is not None
        # Four records, the task before them, the intervening gap and the trailing turn.
        assert len(snapshot.messages) == 7
        assert all("old payload" not in m.text for m in snapshot.messages)
        assert work._history is None
        await running(work)
        assert len(work._ready) == 1
        if change_gap:
            gap[500].contents = [Content.from_text("changed after preparation")]
        assert await work.apply_ready(messages) is (not change_gap)
        assert strategy.records_merged == int(not change_gap)
    finally:
        await work.close()


async def test_merge_observer_separates_preparation_and_application():
    events = []
    with observe_record_merges(
        lambda phase, event, details: events.append((phase, event, details)),
        tracked_values=_values(0),
    ):
        strategy, work, messages = await prepared()
        await work.start(tool_reply())
        await running(work)
        assert ("background", "answer_prepared") in [(p, e) for p, e, _ in events]
        assert all(p != "apply" for p, _, _ in events)
        await strategy(messages)
    survey = next(d for p, e, d in events if p == "background" and e == "survey")
    assert survey["pressure"] == 1 and survey["eligible"] == 4
    assert survey["record_tokens"] > survey["record_limit"]
    accepted = next(d for p, e, d in events if p == "apply" and e == "answer_accepted")
    prepared_batch = next(d["batch"] for p, e, d in events if e == "answer_prepared")
    assert accepted["tracked_values"] == accepted["kept_tracked_values"] == len(_values(0))
    assert accepted["batch"] == prepared_batch
    assert accepted["records"] == 4
    await work.close()


async def test_observer_failure_preserves_merge_journal_and_worker_cleanup():
    def fail(phase, event, details):
        if (phase, event) in {("apply", "answer_accepted"), ("background", "worker_closed")}:
            raise RuntimeError("observer failure")

    with observe_record_merges(fail):
        strategy, work, messages = await prepared()
        await work.start(tool_reply())
        await running(work)
        assert await strategy(messages)
        work.remember(messages, strategy.max_input_tokens)
        await work.start(tool_reply())
        task = running(work)
        await work.close()
    assert task.done()
    assert strategy._background is None
    assert len(strategy.session_state()["record_order"]) == 1
    assert len(strategy.session_state()["kept_records"]) == 1


async def test_tracked_values_require_whole_identifiers_and_rewrites_emit_no_merge_events():
    model = _Summarizer()
    strategy = _strategy(model, budget=100_000)
    messages = _history(6)
    annotate_message_groups(messages)
    groups = consolidatable_record_groups(messages)
    bodies = record_bodies(messages, groups)
    values = set().union(*(_distinctive_tokens(body) for body in bodies))
    marker = next(value for value in _values(0) if value in values)
    replacement = " ".join(sorted((values - {marker}) | {f"{marker}9"}))
    events = []
    with observe_record_merges(lambda p, e, d: events.append((e, d)), tracked_values=[marker]):
        await strategy._ask(
            prompt=strategy.merge_prompt, transcript="synthetic", operation="rewrite"
        )
        assert not events
        outcome = await strategy._consolidate(
            messages,
            groups,
            prompt=strategy.merge_prompt,
            transcript=_transcript(messages, groups),
            answer=_RecordAnswer(replacement),
        )
    assert outcome == "accepted"
    accepted = next(data for event, data in events if event == "answer_accepted")
    assert accepted["tracked_values"] == 1
    assert accepted["kept_tracked_values"] == 0


async def test_next_pass_does_not_wait_for_unfinished_merge():
    entered, release = asyncio.Event(), asyncio.Event()

    class Waiting(_Summarizer):
        async def get_response(self, messages, **kwargs):
            entered.set()
            await release.wait()
            return await super().get_response(messages, **kwargs)

    strategy, work, messages = await prepared(Waiting())
    await work.start(tool_reply())
    await entered.wait()
    assert not await strategy(messages)
    assert not release.is_set() and not running(work).done()
    assert strategy.records_merged == 0
    release.set()
    await running(work)
    assert await strategy(messages)
    assert strategy.records_merged == 1
    await work.close()


@pytest.mark.parametrize("complete", [False, True])
async def test_prompt_change_discards_background_answer_and_search(complete):
    entered, release = asyncio.Event(), asyncio.Event()

    class Waiting(_Summarizer):
        async def get_response(self, messages, **kwargs):
            entered.set()
            await release.wait()
            return await super().get_response(messages, **kwargs)

    strategy, work, messages = await prepared(Waiting())
    await work.start(tool_reply())
    await entered.wait()
    if complete:
        release.set()
        await running(work)
    strategy.merge_prompt += " Preserve the source attribution."
    release.set()
    await running(work)
    assert work._ready
    before = _sent(messages)
    assert not await strategy(messages)
    assert _sent(messages) == before
    assert not strategy._kept_records
    assert not strategy.session_state()["searched_records"]
    assert not strategy.session_state()["refused_merges"]
    await work.close()


@pytest.mark.parametrize("restore", [False, True])
async def test_partial_search_after_prompt_change_drops_old_batches(monkeypatch, restore):
    class PolicyTokenizer(CharacterEstimatorTokenizer):
        def count_tokens(self, text):
            if '"role": "system"' in text and "compaction records" in text:
                return 100 if "New merge policy." in text else 49_000
            if '"role": "user"' in text and '"text": "1. ' in text and "\\n2. " in text:
                return 1500
            return super().count_tokens(text)

    model = _Summarizer()
    tokenizer = PolicyTokenizer()
    model.rewrite = model.merge
    strategy = _strategy(model, budget=100_000, share=0.001, cap=None, tokenizer=tokenizer)
    work = _RecordMergeWork()
    work.bind(strategy, None)
    messages = _history(4)
    await strategy(messages)
    await work.start(tool_reply())
    await running(work)
    assert not await strategy(messages)
    assert len(strategy.session_state()["searched_records"]) > 1
    assert not model.requests
    if restore:
        state = deepcopy(strategy.session_state())
        await work.close()
        strategy = _strategy(model, budget=100_000, share=0.001, cap=None, tokenizer=tokenizer)
        strategy.restore_session_state(state)
        work = _RecordMergeWork()
        work.bind(strategy, None)
        await strategy(messages)
    strategy.merge_prompt += " New merge policy."
    with monkeypatch.context() as limited:
        limited.setattr(composed_module, "_MAX_BATCH_PROBES", 1)
        await work.start(tool_reply())
        await running(work)
        assert work._worker is not None
        partial = work._worker.session_state()
        assert len(partial["searched_records"]) == 1
        assert not await strategy(messages)
    adopted = strategy.session_state()
    await work.start(tool_reply())
    await running(work)
    assert await strategy(messages)
    assert strategy.records_merged == 2
    assert [_batch_size(body) for _, body in model.requests] == [2, 2]
    assert _sent_values(messages) == {v for i in range(4) for v in _values(i)}
    assert adopted["search_prompt"] == partial["search_prompt"]
    assert adopted["searched_records"] == partial["searched_records"]
    await work.close()


@pytest.mark.parametrize("change", ["content", "identity", "hold", "reasoning", "excluded"])
async def test_stale_or_newly_protected_sources_refuse_background_answer(change):
    strategy, work, messages = await prepared()
    await work.start(tool_reply())
    await running(work)
    if change == "content":
        messages[3].contents[0].result += " changed"
    elif change == "identity":
        messages[3].message_id = "different-source"
    elif change == "hold":
        set_preserved(messages[3], preserved=True, reason="host")
    elif change == "excluded":
        messages[3].additional_properties[EXCLUDED_KEY] = True
    else:
        messages.append(Message("assistant", [Content.from_text_reasoning(text="later thought")]))
    await strategy(messages)
    assert strategy.records_merged == 0
    assert not strategy._kept_records
    await work.close()


@pytest.mark.parametrize("ignore_cancellation", [False, True])
async def test_cancelled_worker_cannot_publish_late_results(ignore_cancellation):
    entered = asyncio.Event()

    class Waiting(_Summarizer):
        async def get_response(self, messages, **kwargs):
            entered.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                if not ignore_cancellation:
                    raise
            return await super().get_response(messages, **kwargs)

    strategy, work, messages = await prepared(Waiting())
    await work.start(tool_reply())
    await entered.wait()
    await work.close()
    assert not work._ready
    assert not strategy._kept_records
    assert strategy._background is None
    assert not await work.apply_ready(messages)


async def test_completed_child_can_apply_while_its_sibling_is_running():
    entered, release = asyncio.Event(), asyncio.Event()

    class Waiting(_Summarizer):
        async def get_response(self, messages, **kwargs):
            if len(self.requests) == 2:
                entered.set()
                await release.wait()
            return await super().get_response(messages, **kwargs)

    model = Waiting(
        merge=lambda body: (
            _VALUE.findall(body)[0]
            if _batch_size(body) > 2
            else " ".join(sorted(set(_VALUE.findall(body))))
        )
    )
    strategy, work, messages = await prepared(model)
    await work.start(tool_reply())
    await entered.wait()
    assert await strategy(messages)
    assert strategy.records_merged == 1
    release.set()
    await running(work)
    assert await strategy(messages)
    assert strategy.records_merged == 2
    assert [_batch_size(body) for body in model.merges] == [4, 2, 2]
    await work.close()


async def test_running_split_sibling_survives_run_end_and_restore():
    entered, release = asyncio.Event(), asyncio.Event()

    class Waiting(_Summarizer):
        async def get_response(self, messages, **kwargs):
            if len(self.requests) == 2:
                entered.set()
                await release.wait()
            return await super().get_response(messages, **kwargs)

    model = Waiting(
        merge=lambda body: (
            _VALUE.findall(body)[0]
            if _batch_size(body) > 2
            else " ".join(sorted(set(_VALUE.findall(body))))
        )
    )
    strategy, work, messages = await prepared(model)
    await work.start(tool_reply())
    await entered.wait()
    assert await strategy(messages)
    assert strategy.records_merged == 1
    state = deepcopy(strategy.session_state())
    assert work._worker is not None
    worker_state = work._worker.session_state()
    await work.close()
    assert state["pending_merges"] == worker_state["pending_merges"]
    assert len(state["pending_merges"]) == 1
    assert state["pending_merges"][0][2] == 1
    assert state["searched_records"] == worker_state["searched_records"]

    restored = _strategy(model, budget=100_000, share=0.001, cap=None)
    restored.restore_session_state(state)
    resumed = _RecordMergeWork()
    resumed.bind(restored, None)
    await restored(messages)
    release.set()
    await resumed.start(tool_reply())
    await running(resumed)
    assert await restored(messages)
    assert restored.records_merged == 1
    assert [_batch_size(body) for body in model.merges] == [4, 2, 2]
    assert _sent_values(messages) == {v for i in range(4) for v in _values(i)}
    await resumed.close()


@pytest.mark.parametrize("budget", [50, 100_000])
async def test_disabled_share_only_merges_background_records_over_budget(budget):
    model = _Summarizer()
    strategy, work, messages = await prepared(model)
    strategy.record_budget_share = None
    work.remember(messages, budget)
    await work.start(tool_reply())
    await running(work)
    assert bool(model.requests) == (budget == 50)
    await strategy(messages)
    assert strategy.records_merged == int(budget == 50)
    await work.close()


OVER_BUDGET = 50


async def test_an_over_budget_background_merge_reaches_records_behind_the_loops_reasoning():
    """#2706: a tool-calling reply carries its reasoning after every record.

    Over budget, the worker still prepares the merge, and the next pass, whose history now holds
    that reasoning too, still applies it.
    """
    model = _Summarizer()
    strategy, work, messages = await prepared(model)
    strategy.record_budget_share = None
    strategy.overflow_policy = "shed"
    reply = [
        Message(
            "assistant",
            [
                Content.from_text_reasoning(text="Compare the lookups."),
                Content.from_function_call(call_id="lookup", name="lookup", arguments={}),
            ],
            message_id="lookup-call",
        )
    ]
    work.remember(messages, OVER_BUDGET)
    await work.start(reply)
    await running(work)
    assert model.merges, "the worker prepared a merge behind the reasoning"

    live = [
        *messages,
        *deepcopy(reply),
        Message(
            "tool",
            [Content.from_function_result(call_id="lookup", result="done")],
            message_id="lookup-result",
        ),
    ]
    token = strategy._request_budget.set(OVER_BUDGET)
    try:
        with suppress(CompactionBudgetExceeded):
            await strategy(live)
    finally:
        strategy._request_budget.reset(token)
    assert strategy.records_merged >= 1, "the next pass applied it"
    await work.close()


async def over_budget_pass(strategy, messages):
    """One foreground pass that ends over budget, as on a host every pass of which does."""
    token = strategy._request_budget.set(OVER_BUDGET)
    try:
        with pytest.raises(CompactionBudgetExceeded):
            await strategy(messages)
    finally:
        strategy._request_budget.reset(token)


async def test_a_pass_that_ends_over_budget_still_hands_the_worker_its_history():
    """A pass that ends over budget still hands the worker its list, so a tool round starts it."""
    model = _Summarizer()
    strategy = _strategy(model, budget=100_000, share=None, cap=None, policy="shed")
    work = _RecordMergeWork()
    work.bind(strategy, None)
    messages = _history(4)
    # An earlier tool round left the merges to the worker, so this pass merges nothing itself.
    work.defer_merges = True

    await over_budget_pass(strategy, messages)
    assert not model.merges
    await work.start(tool_reply())
    await running(work)
    assert model.merges, "the worker started from the over-budget pass's list"

    await over_budget_pass(strategy, messages)
    assert strategy.records_merged >= 1, "the next pass applied what the worker prepared"
    await work.close()


async def test_a_worker_that_cannot_start_leaves_the_merges_to_the_foreground():
    """While no worker can start, a tool-calling reply leaves the merges to the foreground."""
    model = _Summarizer()
    strategy = _strategy(model, budget=100_000, share=None, cap=None, policy="shed")
    work = _RecordMergeWork()
    work.bind(strategy, None)
    messages = _history(4)

    await work.start(tool_reply())
    assert work._task is None
    assert not work.defer_merges
    await over_budget_pass(strategy, messages)
    assert strategy.records_merged >= 1, "the foreground merged, as without the bundle"
    await work.close()


async def test_snapshot_failure_reaches_run_observer_from_reader_task(monkeypatch):
    tag = ContextVar("conversation", default="reader")
    seen = []

    def fail(*args):
        seen.append(("snapshot", tag.get()))
        tag.set("snapshot")
        raise ValueError("snapshot failed")

    def observe(phase, event, details):
        if phase == "background":
            seen.append((event, tag.get()))

    token = tag.set("run")
    try:
        with observe_record_merges(observe):
            _, work, _ = await prepared()
    finally:
        tag.reset(token)
    monkeypatch.setattr(composed_module, "counted_history", fail)
    await asyncio.create_task(work.start(tool_reply()))
    assert seen == [("snapshot", "run"), ("worker_failed", "run")]
    assert not work.defer_merges
    assert work._task is None
    await work.close()


async def test_a_worker_stopped_at_its_deadline_says_so(caplog, monkeypatch):
    events = []
    with observe_record_merges(lambda phase, event, details: events.append((phase, event))):
        strategy, work, _ = await prepared()
    strategy.chain_seconds = 1.0
    clock = iter([0.0])
    monkeypatch.setattr(composed_module, "monotonic", lambda: next(clock, 2.0))
    with caplog.at_level(logging.INFO, logger="maf_compaction"):
        await work.start(tool_reply())
        await running(work)
    assert "Background record merge stopped at its" in caplog.text
    assert ("background", "deadline") in events
    await work.close()


async def test_cancelled_lossy_answer_cannot_start_child_requests():
    entered = asyncio.Event()

    class Waiting(_Summarizer):
        async def get_response(self, messages, **kwargs):
            entered.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                # Ignoring cancellation must not allow child requests to start.
                pass
            return await super().get_response(messages, **kwargs)

    model = Waiting(merge=lambda body: _VALUE.findall(body)[0])
    _, work, _ = await prepared(model)
    await work.start(tool_reply())
    await entered.wait()
    await asyncio.wait_for(work.close(), 2)
    assert len(model.requests) == 1
    assert not work._ready


def background_harness(registry, model, lookup, *outer_middleware):
    provider = SessionCompactionProvider(registry, tokenizer=TOKENIZER, before=False)
    agent = create_harness_agent(
        model,
        name="background-merges",
        disable_compaction=True,
        disable_file_memory=True,
        disable_web_search=True,
        disable_tool_auto_approval=True,
        context_providers=[provider],
        middleware=[
            *outer_middleware,
            make_session_record_merge_middleware(registry),
            SessionToolResultRecallMiddleware(registry, compact_before=True),
        ],
        tools=[make_session_recall_tool(registry), lookup],
    )
    agent.context_providers.remove(provider)
    agent.context_providers.insert(1, provider)
    return agent


class ToolModel(Model):
    def _inner_get_response(self, *, messages, stream, options, **kwargs):
        self.prompts.append(deepcopy(list(messages)))
        contents = (
            tool_reply()[0].contents if len(self.prompts) == 1 else [Content.from_text("answer")]
        )
        response = ChatResponse(
            messages=Message("assistant", contents, message_id=f"reply-{len(self.prompts)}")
        )
        if stream:

            async def updates():
                yield ChatResponseUpdate(role="assistant", contents=contents)

            return ResponseStream(updates(), finalizer=lambda _: response)

        async def answer():
            return response

        return answer()


@pytest.mark.parametrize("early_close", [False, True])
async def test_streamed_worker_uses_run_context(caplog, monkeypatch, early_close):
    if early_close:
        # Framework telemetry resets its own tokens in the first reader's task.
        monkeypatch.setattr(OBSERVABILITY_SETTINGS, "enable_instrumentation", False)
    tag = ContextVar("conversation", default="reader")
    background, reader_events, requests, close_logs = [], [], [], []

    def observe(phase, event, details):
        if phase == "background":
            background.append((event, tag.get(), details))

    class BindRun(AgentMiddleware):
        async def process(self, context, call_next):
            token = tag.set("run")
            try:
                with observe_record_merges(observe):
                    await call_next()
            finally:
                tag.reset(token)

    class Summarizer(_Summarizer):
        async def get_response(self, messages, **kwargs):
            requests.append(tag.get())
            return await super().get_response(messages, **kwargs)

    def build():
        stack = build_stack(Summarizer(), budget=100_000)
        assert isinstance(
            stack.strategy, ToolResultAndUserTurnAnchoredSummarizationCompactionStrategy
        )
        stack.strategy.record_budget_share = 0.001
        return stack

    def collect_close_log(record):
        if record.getMessage().startswith("Background record merge closed"):
            close_logs.append(tag.get())
        return True

    registry = SessionCompactionRegistry(build)
    session = AgentSession()
    session.state["in_memory"] = {"messages": _history(4)}

    @tool
    async def lookup() -> str:
        """Look up one fact."""
        await running(registry._merge_work[session.session_id][1])
        return "lookup complete"

    agent = background_harness(registry, ToolModel(), lookup, BindRun())
    stream = agent.run("Continue.", session=session, stream=True)

    async def close():
        tag.set("closer")
        await stream.close()
        assert tag.get() == "closer"

    with (
        caplog.at_level(logging.INFO, logger="maf_compaction"),
        caplog.filtering(collect_close_log),
        observe_record_merges(lambda p, e, d: reader_events.append(p)),
    ):
        async with asyncio.timeout(10), stream:
            if early_close:
                async for update in stream:
                    if any(c.type == "function_result" for c in update.contents):
                        break
                await asyncio.create_task(close())
            else:
                assert (await stream.get_final_response()).text == "answer"
            assert tag.get() == "reader"

    assert requests == ["run"]
    assert {"worker_started", "answer_prepared"} <= {e for e, _, _ in background}
    assert all(value == "run" for _, value, _ in background)
    assert "background" not in reader_events
    if early_close:
        assert background[-1] == ("worker_closed", "run", {"pending": 0, "discarded": 1})
        assert close_logs == ["run"]
    else:
        assert "apply" in reader_events
    assert not registry._merge_work


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("ready", [False, True])
async def test_agent_merges_during_tools_without_waiting_for_pending_work(stream, ready):
    entered, release, cancelled = asyncio.Event(), asyncio.Event(), asyncio.Event()

    class Waiting(_Summarizer):
        async def get_response(self, messages, **kwargs):
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise
            return await super().get_response(messages, **kwargs)

    summarizer = Waiting()

    def build():
        stack = build_stack(summarizer, budget=100_000)
        assert isinstance(
            stack.strategy, ToolResultAndUserTurnAnchoredSummarizationCompactionStrategy
        )
        stack.strategy.record_budget_share = 0.001
        return stack

    registry = SessionCompactionRegistry(build)
    session = AgentSession()
    session.state["in_memory"] = {"messages": _history(4)}

    @tool
    async def lookup() -> str:
        """Look up one fact."""
        await asyncio.wait_for(entered.wait(), 2)
        work = registry._merge_work[session.session_id][1]
        assert not running(work).done()
        if ready:
            release.set()
            await running(work)
        return "lookup complete"

    model = ToolModel()
    agent = background_harness(registry, model, lookup)
    result = agent.run("Continue.", session=session, stream=stream)
    response = await asyncio.wait_for(
        result.get_final_response() if isinstance(result, ResponseStream) else result, 10
    )
    assert response.text == "answer"
    assert len(model.prompts) == 2
    assert not registry._merge_work
    with registry.activate(session) as stack:
        assert isinstance(
            stack.strategy, ToolResultAndUserTurnAnchoredSummarizationCompactionStrategy
        )
        assert stack.strategy.records_merged == int(ready)
        assert stack.strategy._background is None
        assert bool(stack.strategy._kept_records) == ready
    assert _sent_values(model.prompts[1]) == {v for i in range(4) for v in _values(i)}
    assert not cancelled.is_set()
    assert len(summarizer.requests) == int(ready)
    if not ready:
        # A second run can finish even while the original request still owns the batch.
        work = registry._retained[session.session_id]
        pending = running(work)
        async with asyncio.timeout(10):
            await agent.run("Continue while pending.", session=session)
        assert registry._retained[session.session_id] is work
        saved = deepcopy(session.to_dict())
        release.set()
        await pending
        assert session.to_dict() == saved
        session = AgentSession.from_dict(saved)
        await agent.run("Apply the late answer.", session=session)
        assert not registry._retained
        with registry.activate(session) as stack:
            assert isinstance(
                stack.strategy, ToolResultAndUserTurnAnchoredSummarizationCompactionStrategy
            )
            assert stack.strategy.records_merged == 1
        assert len(summarizer.requests) == 1


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("ending", ["done", "tool_error", "model_error", "cancel"])
@pytest.mark.parametrize("parallel", [1, 3])
async def test_agent_failure_or_cancellation_cleans_up_work(stream, ending, parallel):
    entered, tool_entered, cancelled = asyncio.Event(), asyncio.Event(), asyncio.Event()
    started_count = cancelled_count = 0

    class Waiting(_Summarizer):
        async def get_response(self, messages, **kwargs):
            nonlocal started_count, cancelled_count
            started_count += 1
            if started_count == parallel:
                entered.set()
            try:
                await asyncio.Future()
            finally:
                cancelled_count += 1
                cancelled.set()

    def build():
        stack = build_stack(Waiting(), budget=100_000)
        assert isinstance(
            stack.strategy, ToolResultAndUserTurnAnchoredSummarizationCompactionStrategy
        )
        stack.strategy.record_budget_share = 0.001
        if parallel > 1:
            stack.strategy.user_turns.summary_options = {"max_tokens": 1_200}
            stack.strategy.user_turns.summary_limiter = SummaryRequestLimiter(
                10_000_000, max_parallel_summary_requests=parallel
            )
        return stack

    registry = SessionCompactionRegistry(build)
    session = AgentSession()
    session.state["in_memory"] = {"messages": _history(18 if parallel > 1 else 4)}

    @tool
    async def lookup() -> str:
        """Look up one fact."""
        await asyncio.wait_for(entered.wait(), 2)
        tool_entered.set()
        if ending == "cancel":
            await asyncio.Future()
        if ending == "tool_error":
            raise ValueError("tool unavailable")
        return "lookup complete"

    class FailingModel(ToolModel):
        def _inner_get_response(self, **kwargs):
            if self.prompts and ending == "model_error":
                raise ValueError("model unavailable")
            return super()._inner_get_response(**kwargs)

    agent = background_harness(registry, FailingModel(), lookup)
    result = agent.run("Continue.", session=session, stream=stream)
    task = asyncio.ensure_future(
        result.get_final_response() if isinstance(result, ResponseStream) else result
    )
    if ending == "cancel":
        await asyncio.wait_for(tool_entered.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    elif ending == "model_error":
        with pytest.raises(ValueError, match="model unavailable"):
            await asyncio.wait_for(task, 10)
    else:
        await asyncio.wait_for(task, 10)
        # The framework delivers a handled tool error as a normal completed run.
        assert not cancelled.is_set()
        pending = running(registry._retained[session.session_id])
        registry.release(session.session_id)
        await asyncio.gather(pending, return_exceptions=True)
    assert cancelled.is_set()
    assert started_count == cancelled_count == parallel
    assert not registry._merge_work
    with registry.activate(session) as stack:
        assert isinstance(
            stack.strategy, ToolResultAndUserTurnAnchoredSummarizationCompactionStrategy
        )
        assert not stack.strategy._kept_records
        assert stack.strategy._background is None


@pytest.mark.parametrize("change", ["evict", "reset", "replace"])
async def test_cached_worker_is_discarded_when_its_session_or_stack_changes(change):
    entered = asyncio.Event()

    class Waiting(_Summarizer):
        async def get_response(self, messages, **kwargs):
            entered.set()
            # A provider can still return an answer after its worker loses the session.
            with suppress(asyncio.CancelledError):
                await asyncio.Future()
            return await super().get_response(messages, **kwargs)

    def build():
        stack = build_stack(Waiting(), budget=100_000)
        assert isinstance(
            stack.strategy, ToolResultAndUserTurnAnchoredSummarizationCompactionStrategy
        )
        stack.strategy.record_budget_share = 0.001
        return stack

    registry = SessionCompactionRegistry(build, max_cached_sessions=1)
    session = AgentSession()
    session.state["in_memory"] = {"messages": _history(4)}

    @tool
    async def lookup() -> str:
        """Look up one fact."""
        await asyncio.wait_for(entered.wait(), 2)
        return "lookup complete"

    await background_harness(registry, ToolModel(), lookup).run("Continue.", session=session)
    work = registry._retained[session.session_id]
    pending = running(work)
    if change == "reset":
        del session.state[registry.state_key]
    elif change == "replace":
        session.state[registry.state_key]["strategy"]["refused_merges"] = [
            {"transcript": "different", "source_count": 2}
        ]
    with registry.activate(AgentSession() if change == "evict" else session):
        pass
    await asyncio.wait_for(asyncio.gather(pending, return_exceptions=True), 2)
    assert not registry._retained
    assert not work._ready


@pytest.mark.parametrize("ending", ["timeout", "provider_error"])
async def test_retained_parallel_wave_keeps_its_answer_when_a_sibling_fails(ending):
    entered, answered, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    requests = 0

    class Waiting(_Summarizer):
        async def get_response(self, messages, **kwargs):
            nonlocal requests
            requests += 1
            if requests == 1:
                entered.set()
                # Exercise a provider that returns its answer even after timeout cancellation.
                with suppress(asyncio.CancelledError):
                    await release.wait()
                if ending == "provider_error":
                    raise RuntimeError("summary unavailable")
            response = await super().get_response(messages, **kwargs)
            answered.set()
            return response

    model = Waiting()
    strategy, work, _ = await prepared(model)
    strategy.record_merge_max_records = 2
    strategy.user_turns.summary_timeout_seconds = 0.02 if ending == "timeout" else 5
    strategy.user_turns.summary_options = {"max_tokens": 1_200}
    strategy.user_turns.retry_without_summary_options = False
    strategy.user_turns.summary_limiter = SummaryRequestLimiter(
        10_000_000, max_parallel_summary_requests=2
    )
    messages = _history(12)
    await strategy(messages)
    await work.start(tool_reply())
    async with asyncio.timeout(5):
        await entered.wait()
        await answered.wait()
        assert work.retain()
        if ending == "provider_error":
            release.set()
        await running(work)
    work.bind(strategy, None)
    assert await work.apply_ready(messages)
    assert strategy.records_merged == 1
    assert requests == 2
    await work.close()


@pytest.mark.parametrize("parallel", [1, 3])
async def test_early_stream_close_cancels_pending_merge(parallel):
    entered, cancelled = asyncio.Event(), asyncio.Event()
    started_count = cancelled_count = 0

    class Waiting(_Summarizer):
        async def get_response(self, messages, **kwargs):
            nonlocal started_count, cancelled_count
            started_count += 1
            if started_count == parallel:
                entered.set()
            try:
                await asyncio.Future()
            finally:
                cancelled_count += 1
                cancelled.set()

    def build():
        stack = build_stack(Waiting(), budget=100_000)
        assert isinstance(
            stack.strategy, ToolResultAndUserTurnAnchoredSummarizationCompactionStrategy
        )
        stack.strategy.record_budget_share = 0.001
        if parallel > 1:
            stack.strategy.user_turns.summary_options = {"max_tokens": 1_200}
            stack.strategy.user_turns.summary_limiter = SummaryRequestLimiter(
                10_000_000, max_parallel_summary_requests=parallel
            )
        return stack

    registry = SessionCompactionRegistry(build)
    session = AgentSession()
    session.state["in_memory"] = {"messages": _history(18 if parallel > 1 else 4)}

    @tool
    async def lookup() -> str:
        """Look up one fact."""
        await asyncio.wait_for(entered.wait(), 2)
        return "lookup complete"

    model = ToolModel()
    agent = background_harness(registry, model, lookup)
    async with asyncio.timeout(10):
        async with agent.run("Continue.", session=session, stream=True) as response:
            async for update in response:
                if any(c.type == "function_result" for c in update.contents):
                    break
    assert len(model.prompts) == 1
    assert cancelled.is_set()
    assert started_count == cancelled_count == parallel
    assert not registry._merge_work


@pytest.mark.parametrize("change", ["reset", "state", "stack"])
@pytest.mark.parametrize("complete", [False, True])
async def test_authoritative_state_or_stack_changes_discard_background_work(change, complete):
    entered, release = asyncio.Event(), asyncio.Event()

    class Waiting(_Summarizer):
        async def get_response(self, messages, **kwargs):
            entered.set()
            await release.wait()
            return await super().get_response(messages, **kwargs)

    model = Waiting()

    def build():
        stack = build_stack(model, budget=100_000)
        assert isinstance(
            stack.strategy, ToolResultAndUserTurnAnchoredSummarizationCompactionStrategy
        )
        stack.strategy.record_budget_share = 0.001
        return stack

    registry = SessionCompactionRegistry(build)
    session = AgentSession()
    work = _RecordMergeWork()
    registry._merge_work[session.session_id] = session, work
    messages = _history(4)
    with registry.activate(session) as first:
        await first.strategy(messages)
    await work.start(tool_reply())
    await entered.wait()
    if complete:
        release.set()
        await running(work)
    if change == "reset":
        session.state.pop(registry.state_key)
    elif change == "state":
        session.state[registry.state_key]["strategy"]["refused_merges"] = [
            {"transcript": "different", "source_count": 2}
        ]
    else:
        registry._cache.clear()
    with registry.activate(session) as stack:
        assert isinstance(
            stack.strategy, ToolResultAndUserTurnAnchoredSummarizationCompactionStrategy
        )
        await stack.strategy(messages)
        assert stack.strategy.records_merged == 0
        assert not stack.strategy._kept_records
    await work.close()
    assert not work._ready
    assert not work._tasks


async def test_background_deadline_keeps_search_and_unfinished_sibling_across_restore(monkeypatch):
    now = 0.0
    monkeypatch.setattr(composed_module, "monotonic", lambda: now)

    class Late(_Summarizer):
        async def get_response(self, messages, **kwargs):
            nonlocal now
            answer = await super().get_response(messages, **kwargs)
            if len(self.requests) == 2:
                now = 31.0
            return answer

    model = Late(
        merge=lambda body: (
            _VALUE.findall(body)[0]
            if _batch_size(body) > 2
            else " ".join(sorted(set(_VALUE.findall(body))))
        )
    )
    strategy, work, messages = await prepared(model)
    await work.start(tool_reply())
    await running(work)
    assert work._worker is not None
    worker_state = work._worker.session_state()
    assert await strategy(messages)
    assert strategy.records_merged == 1
    state = deepcopy(strategy.session_state())
    assert state["pending_merges"] == worker_state["pending_merges"]
    assert state["pending_merges"]
    assert state["searched_records"] == worker_state["searched_records"]
    assert state["search_prompt"] == worker_state["search_prompt"]
    await work.close()

    restored = _strategy(model, budget=100_000, share=0.001, cap=None)
    restored.restore_session_state(state)
    resumed = _RecordMergeWork()
    resumed.bind(restored, None)
    await restored(messages)
    await resumed.start(tool_reply())
    await running(resumed)
    assert await restored(messages)
    assert restored.records_merged == 1
    assert [_batch_size(body) for body in model.merges] == [4, 2, 2]
    assert _sent_values(messages) == {v for i in range(4) for v in _values(i)}
    await resumed.close()


@pytest.mark.parametrize("batch_fits", [False, True])
@pytest.mark.parametrize("share", [None, 0.001])
async def test_background_batch_search_yields_to_the_next_pass(batch_fits, share):
    entered = asyncio.Event()
    # The worker counts tokens on a worker thread, where an asyncio.Event may not be set.
    loop = asyncio.get_running_loop()

    class BatchTokenizer(CharacterEstimatorTokenizer):
        probes = 0

        def count_tokens(self, text):
            if '"role": "user"' in text and '"text": "1. ' in text and "\\n2. " in text:
                self.probes += 1
                if self.probes == 3:
                    loop.call_soon_threadsafe(entered.set)
                return 100 if batch_fits else 100_000
            return super().count_tokens(text)

    tokenizer = BatchTokenizer()
    model = _Summarizer()
    strategy = _strategy(model, budget=100_000, share=share, cap=None, tokenizer=tokenizer)
    requests: list[str] = []

    def observe(phase: str, event: str, counts: object) -> None:
        if event == "request_prepared":
            requests.append(phase)

    with observe_record_merges(observe):
        work = _RecordMergeWork()
        work.bind(strategy, None)
        messages = _history(30)
        await strategy(messages)
        if share is None:
            work.remember(messages, 50)
        try:
            await work.start(tool_reply())
            worker = running(work)
            await asyncio.wait_for(entered.wait(), 2)
            # The search runs beside the pass on its own state: the pass is not held up waiting for
            # it, and leaves the merging to it.
            await asyncio.wait_for(strategy(messages), 2)
            await asyncio.wait_for(worker, 2)
        finally:
            await work.close()
    assert set(requests) <= {"background"}
    assert len(model.requests) == len(requests)


async def test_pending_background_work_does_not_bypass_the_sending_budget():
    entered = asyncio.Event()

    class Waiting(_Summarizer):
        async def get_response(self, messages, **kwargs):
            entered.set()
            await asyncio.Future()

    strategy, work, messages = await prepared(Waiting())
    await work.start(tool_reply())
    await entered.wait()
    token = strategy._request_budget.set(50)
    try:
        with pytest.raises(CompactionBudgetExceeded):
            await asyncio.wait_for(strategy(messages), 2)
        assert not running(work).done()
    finally:
        strategy._request_budget.reset(token)
        await work.close()


async def test_background_option_retirement_is_reused_by_later_requests():
    options = []

    class RejectingOptions(_Summarizer):
        async def get_response(self, messages, **kwargs):
            options.append(kwargs["options"])
            if kwargs["options"]:
                raise ValueError("Unsupported option")
            return await super().get_response(messages, **kwargs)

    strategy, work, messages = await prepared(RejectingOptions())
    strategy.user_turns.summary_options = {"reasoning_effort": "minimal"}
    strategy.user_turns.retry_without_summary_options = True
    await work.start(tool_reply())
    await running(work)
    assert await strategy(messages)
    assert strategy.user_turns._summary_options_disabled
    await strategy._ask(prompt=strategy.merge_prompt, transcript="1. VAL-001-0")
    assert options == [{"reasoning_effort": "minimal"}, {}, {}]
    await work.close()
