"""Background answers must wait for foreground validation and stay inside their run."""

import asyncio
from copy import deepcopy

import pytest
from agent_framework import (
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
from agent_framework._compaction import EXCLUDED_KEY
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
    ToolResultAndUserTurnAnchoredSummarizationCompactionStrategy,
    make_session_recall_tool,
    make_session_record_merge_middleware,
)
from maf_compaction import _composed as composed_module
from maf_compaction._composed import _RecordMergeWork
from maf_compaction._preserve import set_preserved

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
    work.start(tool_reply())
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


async def test_next_pass_does_not_wait_for_unfinished_merge():
    entered, release = asyncio.Event(), asyncio.Event()

    class Waiting(_Summarizer):
        async def get_response(self, messages, **kwargs):
            entered.set()
            await release.wait()
            return await super().get_response(messages, **kwargs)

    strategy, work, messages = await prepared(Waiting())
    work.start(tool_reply())
    await entered.wait()
    assert not await strategy(messages)
    assert not release.is_set() and not running(work).done()
    assert strategy.records_merged == 0
    release.set()
    await running(work)
    assert await strategy(messages)
    assert strategy.records_merged == 1
    await work.close()


@pytest.mark.parametrize("change", ["content", "identity", "hold", "reasoning", "excluded"])
async def test_stale_or_newly_protected_sources_refuse_background_answer(change):
    strategy, work, messages = await prepared()
    work.start(tool_reply())
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
    work.start(tool_reply())
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
    work.start(tool_reply())
    await entered.wait()
    assert await strategy(messages)
    assert strategy.records_merged == 1
    release.set()
    await running(work)
    assert await strategy(messages)
    assert strategy.records_merged == 2
    assert [_batch_size(body) for body in model.merges] == [4, 2, 2]
    await work.close()


async def test_cancelled_lossy_answer_cannot_start_child_requests():
    entered = asyncio.Event()

    class Waiting(_Summarizer):
        async def get_response(self, messages, **kwargs):
            entered.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                pass
            return await super().get_response(messages, **kwargs)

    model = Waiting(merge=lambda body: _VALUE.findall(body)[0])
    _, work, _ = await prepared(model)
    work.start(tool_reply())
    await entered.wait()
    await asyncio.wait_for(work.close(), 2)
    assert len(model.requests) == 1
    assert not work._ready


def background_harness(registry, model, lookup):
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
    assert cancelled.is_set() != ready
    assert len(summarizer.requests) == int(ready)


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("ending", ["tool_error", "model_error", "cancel"])
async def test_agent_failure_or_cancellation_cleans_up_work(stream, ending):
    entered, tool_entered, cancelled = asyncio.Event(), asyncio.Event(), asyncio.Event()

    class Waiting(_Summarizer):
        async def get_response(self, messages, **kwargs):
            entered.set()
            try:
                await asyncio.Future()
            finally:
                cancelled.set()

    def build():
        stack = build_stack(Waiting(), budget=100_000)
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
    assert cancelled.is_set()
    assert not registry._merge_work
    with registry.activate(session) as stack:
        assert isinstance(
            stack.strategy, ToolResultAndUserTurnAnchoredSummarizationCompactionStrategy
        )
        assert not stack.strategy._kept_records
        assert stack.strategy._background is None


async def test_early_stream_close_cancels_pending_merge():
    entered, cancelled = asyncio.Event(), asyncio.Event()

    class Waiting(_Summarizer):
        async def get_response(self, messages, **kwargs):
            entered.set()
            try:
                await asyncio.Future()
            finally:
                cancelled.set()

    def build():
        stack = build_stack(Waiting(), budget=100_000)
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
    work.start(tool_reply())
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
    work.start(tool_reply())
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
    resumed.start(tool_reply())
    await running(resumed)
    assert await restored(messages)
    assert restored.records_merged == 1
    assert [_batch_size(body) for body in model.merges] == [4, 2, 2]
    assert _sent_values(messages) == {v for i in range(4) for v in _values(i)}
    await resumed.close()


async def test_background_batch_search_yields_to_the_next_pass():
    entered = asyncio.Event()

    class NoBatchFits(CharacterEstimatorTokenizer):
        probes = 0

        def count_tokens(self, text):
            if '"role": "user"' in text and '"text": "1. ' in text and "\\n2. " in text:
                self.probes += 1
                if self.probes == 3:
                    entered.set()
                return 100_000
            return super().count_tokens(text)

    tokenizer = NoBatchFits()
    model = _Summarizer()
    strategy = _strategy(model, budget=100_000, share=0.001, cap=None, tokenizer=tokenizer)
    work = _RecordMergeWork()
    work.bind(strategy, None)
    messages = _history(30)
    await strategy(messages)
    work.start(tool_reply())
    await asyncio.wait_for(entered.wait(), 2)
    assert not await strategy(messages)
    assert tokenizer.probes < 10
    assert not running(work).done()
    await work.close()
    assert not model.requests


async def test_pending_background_work_does_not_bypass_the_sending_budget():
    entered = asyncio.Event()

    class Waiting(_Summarizer):
        async def get_response(self, messages, **kwargs):
            entered.set()
            await asyncio.Future()

    strategy, work, messages = await prepared(Waiting())
    work.start(tool_reply())
    await entered.wait()
    token = strategy._request_budget.set(50)
    try:
        with pytest.raises(CompactionBudgetExceeded):
            await asyncio.wait_for(strategy(messages), 2)
        assert not running(work).done()
    finally:
        strategy._request_budget.reset(token)
        await work.close()
