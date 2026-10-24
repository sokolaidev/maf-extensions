"""Session isolation, replica-switch replay and framework wiring contracts."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import nullcontext
from copy import deepcopy
from typing import Any

import pytest
from agent_framework import (
    AgentSession,
    BaseChatClient,
    CharacterEstimatorTokenizer,
    ChatContext,
    ChatMiddlewareLayer,
    ChatResponse,
    ChatResponseUpdate,
    Content,
    FunctionInvocationContext,
    FunctionInvocationLayer,
    Message,
    ResponseStream,
    SessionContext,
    create_harness_agent,
    tool,
)
from agent_framework._compaction import (
    annotate_message_groups,
    annotate_token_counts,
    project_included_messages,
)

from maf_compaction import (
    DEFAULT_RECORD_INSTRUCTION,
    ChainDecisions,
    CompactionBudgetExceeded,
    CompactionStack,
    RecallGate,
    RecordDecisions,
    SessionCompactionProvider,
    SessionCompactionRegistry,
    SessionToolResultRecallMiddleware,
    ToolResultAnchoredSummarizationCompactionStrategy,
    ToolResultAndUserTurnAnchoredSummarizationCompactionStrategy,
    ToolResultRecallMiddleware,
    UserTurnAnchoredSummarizationCompactionStrategy,
    _usersummary,
    make_recall_tool,
    make_session_recall_tool,
)
from maf_compaction._preserve import set_preserved
from maf_compaction._toolsummary import (
    PRESERVE_REASON_UNRECORDED,
    RECORD_MARKER,
    _Reforce,
    build_record_message,
)

pytestmark = pytest.mark.anyio
TOKENIZER = CharacterEstimatorTokenizer()


class Summarizer:
    def __init__(self, text="short summary"):
        self.text = text
        self.requests = []

    async def get_response(self, messages, **kwargs):
        self.requests.append([message.text for message in messages])
        return ChatResponse(messages=Message("assistant", [self.text]))


def build_stack(
    summarizer=None,
    *,
    budget=2000,
    composed=True,
    reforce="bound",
    record_instruction=None,
    **user_options,
):
    record = ToolResultAnchoredSummarizationCompactionStrategy(
        max_input_tokens=budget, tokenizer=TOKENIZER, keep_head_groups=1
    )
    strategy = record
    if composed:
        users = UserTurnAnchoredSummarizationCompactionStrategy(
            max_input_tokens=budget,
            tokenizer=TOKENIZER,
            client=summarizer or Summarizer(),
            max_summary_input_tokens=100_000,
            summary_mode="boundary",
            remembered_requests=2,
            **user_options,
        )
        strategy = ToolResultAndUserTurnAnchoredSummarizationCompactionStrategy(
            tokenizer=TOKENIZER, tool_results=record, user_turns=users
        )
    gate = RecallGate()
    middleware = ToolResultRecallMiddleware(
        max_input_tokens=budget,
        tokenizer=TOKENIZER,
        arm=gate.arm,
        disarm=gate.disarm,
        record_instruction=record_instruction,
        reforce=(
            record.take_reforce
            if reforce == "bound"
            else (lambda: record.take_reforce())
            if reforce == "wrapped"
            else None
        ),
    )
    return CompactionStack(strategy, gate, middleware)


@pytest.mark.parametrize("composed", [False, True])
@pytest.mark.parametrize("reforce", ["bound", "wrapped", None])
def test_stack_keeps_reforce_hooks_optional(composed, reforce):
    stack = build_stack(composed=composed, reforce=reforce)
    record = (
        stack.strategy.tool_results
        if isinstance(stack.strategy, ToolResultAndUserTurnAnchoredSummarizationCompactionStrategy)
        else stack.strategy
    )
    assert (record._recall_due is None) == (reforce is None)
    assert (stack.middleware.reforce_feedback is None) == (reforce is None)
    assert (stack.middleware.reforce is None) == (reforce is None)


def round_trip(value):
    return json.loads(json.dumps(value))


def test_decisions_are_independent_json_values():
    record = RecordDecisions(
        frozenset({"a", "b"}),
        frozenset({"c"}),
        frozenset({"d"}),
        _Reforce(frozenset({"a"}), 2, 1, True),
        frozenset({"VALUE-123"}),
        "anchor",
        1,
        (("earlier", False),),
    )
    chain = ChainDecisions(3, "anchor", 4, "declined", record)
    restored = ChainDecisions.from_dict(round_trip(chain.to_dict()))
    assert restored == chain
    assert restored.records.reforce is not record.reforce
    restored.records.reforce.passes += 1
    assert record.reforce.passes == 1
    empty = RecordDecisions(frozenset(), frozenset(), frozenset(), None)
    assert RecordDecisions.from_dict(round_trip(empty.to_dict())) == empty


@pytest.mark.parametrize("version", [1, 2, 3, 4])
@pytest.mark.parametrize("field", ["anchor", "declined_behind"])
def test_legacy_recall_identity_is_rejected_before_restoring_pending_state(version, field):
    summarizer = Summarizer()
    stack = build_stack(summarizer)
    snapshot = round_trip(stack.to_dict())
    snapshot["version"] = version
    decisions = snapshot["strategy"]["decisions"]
    decisions.pop("record_identity_version", None)
    decisions[field] = "call:shared"
    decisions["wait_since"] = 3
    snapshot["middleware"]["awaiting_record"] = True
    history = [
        Message(
            "assistant",
            [
                Content.from_function_call(
                    call_id="shared", name="recall_values", arguments="{}", id="old"
                )
            ],
            message_id="recall-call",
        ),
        Message(
            "tool",
            [
                Content.from_function_result(
                    call_id="shared", result=f"{RECORD_MARKER} old", id="old"
                )
            ],
        ),
    ]
    session = AgentSession()
    session.state.update({"maf_compaction": snapshot, "history": [m.to_dict() for m in history]})
    before = round_trip(session.state)
    registry = SessionCompactionRegistry(lambda: build_stack(summarizer))
    with pytest.raises(ValueError, match="legacy recall identities"):
        with registry.activate(session):
            pytest.fail("An incompatible pending state must not reach compaction")
    assert session.state == before
    assert not summarizer.requests


def test_legacy_recall_identity_is_rejected_by_direct_decision_restore():
    stack = build_stack()
    snapshot = stack.to_dict()["strategy"]["decisions"]
    snapshot.pop("record_identity_version", None)
    snapshot["anchor"] = "call:shared"
    with pytest.raises(ValueError, match="legacy recall identities"):
        ChainDecisions.from_dict(snapshot)


@pytest.mark.parametrize("anchor", ["", "text:written record", "call:shared"])
def test_current_identity_schema_restores_wait_decisions(anchor):
    stack = build_stack()
    snapshot = stack.to_dict()
    decisions = snapshot["strategy"]["decisions"]
    decisions.update(anchor=anchor, wait_since=3, declined_behind=anchor)
    stack.restore(round_trip(snapshot))
    assert stack.to_dict() == snapshot


@pytest.mark.parametrize("anchor", ["", "text:written record"])
def test_legacy_snapshot_without_recall_identity_remains_readable(anchor):
    stack = build_stack()
    snapshot = stack.to_dict()
    snapshot["version"] = 4
    decisions = snapshot["strategy"]["decisions"]
    decisions.pop("record_identity_version")
    decisions.update(anchor=anchor, wait_since=3, declined_behind=anchor)
    stack.restore(snapshot)
    restored = stack.to_dict()["strategy"]["decisions"]
    assert restored == decisions | {"record_identity_version": 2}


def test_unknown_record_identity_schema_is_rejected_before_restore():
    stack = build_stack()
    before = round_trip(stack.to_dict())
    snapshot = round_trip(before)
    snapshot["strategy"]["decisions"]["record_identity_version"] = 999
    with pytest.raises(ValueError, match="record identity version"):
        stack.restore(snapshot)
    assert stack.to_dict() == before


@pytest.mark.parametrize("composed", [False, True])
def test_stack_writes_v5_snapshots_and_restores_v1(composed):
    stack = build_stack(composed=composed)
    stack.gate.arm()
    stack.middleware._force_next = True
    snapshot = round_trip(stack.to_dict())
    assert snapshot["version"] == 5
    legacy = snapshot | {"version": 1}
    restored = build_stack(composed=composed)
    restored.restore(legacy)
    assert restored.to_dict() == snapshot


@pytest.mark.parametrize("version", [1, 2, 3, 4])
async def test_stack_restores_fold_keys_and_replays_without_a_request(version):
    summarizer = Summarizer()
    stack = build_stack(summarizer)
    assert isinstance(stack.strategy, ToolResultAndUserTurnAnchoredSummarizationCompactionStrategy)
    users = stack.strategy.user_turns
    messages = [
        users._summary_message(
            [Message("user", ["source"], message_id=f"source{index}")],
            [{"group_id": f"group{index}"}],
            f"summary {index} " * 150,
            summary_id=f"user_summary_{index}",
        )
        for index in range(3)
    ]
    annotate_token_counts(messages, tokenizer=TOKENIZER)
    stored = deepcopy(messages)
    assert await users.fold_if_smaller(messages) == "folded"
    snapshot = round_trip(stack.to_dict())
    snapshot["version"] = version
    folds = snapshot["strategy"]["user_turns"]["kept_folds"]
    if version == 1:
        snapshot["strategy"]["user_turns"]["kept_folds"] = {
            entry["transcript"]: entry for entry in folds.values()
        }
    restored = build_stack(summarizer)
    restored.restore(snapshot)
    assert isinstance(
        restored.strategy, ToolResultAndUserTurnAnchoredSummarizationCompactionStrategy
    )
    restored_users = restored.strategy.user_turns
    assert set(restored_users.session_state()["kept_folds"]) == {
        entry["summary_id"] for entry in folds.values()
    }
    assert restored_users.refold(stored)
    assert [(m.message_id, m.text) for m in project_included_messages(stored)] == [
        (m.message_id, m.text) for m in project_included_messages(messages)
    ]
    assert not restored_users.refold(stored)
    assert len(summarizer.requests) == 1
    assert restored.to_dict()["version"] == 5


def test_cache_is_bounded_and_session_state_wins_on_hits_and_resets():
    builds = []

    def build():
        stack = build_stack()
        builds.append(stack)
        return stack

    registry = SessionCompactionRegistry(build, max_cached_sessions=1)
    a, b = AgentSession(session_id="a"), AgentSession(session_id="b")
    with registry.activate(a) as first:
        first.gate.arm()
    initial = round_trip(a.state)
    with registry.activate(a) as cached:
        assert cached is first
        assert cached.gate.take()
    a.state = initial
    with registry.activate(a) as cached:
        assert cached is first
        assert cached.gate.take()
    with registry.activate(b):
        pass
    assert len(registry._cache) == 1
    with registry.activate(a) as rebuilt:
        assert rebuilt is not first
        assert not rebuilt.gate.take()
    del a.state[registry.state_key]
    with registry.activate(a) as reset:
        assert reset is not rebuilt
    assert len(builds) == 4
    with pytest.raises(RuntimeError, match="No active"):
        registry.current_stack()


@pytest.mark.parametrize("cache", ["cached", "disabled", "evicted", "replica"])
@pytest.mark.parametrize("threshold", [1, 2])
async def test_summary_cooldown_follows_session_between_phases(monkeypatch, cache, threshold):
    now = 1_700_000_000.0
    monkeypatch.setattr(_usersummary, "time", lambda: now)

    class FailingSummarizer:
        calls = 0

        async def get_response(self, *args, **kwargs):
            self.calls += 1
            raise RuntimeError("summary unavailable")

    client = FailingSummarizer()

    def build():
        stack = build_stack(
            client,
            budget=10_000,
            summary_timeout_seconds=10,
            max_consecutive_summary_failures=threshold,
            summary_backoff_seconds=60,
        )
        assert isinstance(
            stack.strategy, ToolResultAndUserTurnAnchoredSummarizationCompactionStrategy
        )
        stack.strategy.user_trigger_fraction = 0.01
        return stack

    registry = SessionCompactionRegistry(build, max_cached_sessions=0 if cache == "disabled" else 1)
    session = AgentSession(session_id="cooling")

    async def phase():
        nonlocal registry, session
        with registry.activate(session) as stack:
            messages = [Message("user", [f"turn {i} " * 100]) for i in range(8)]
            assert not await stack.strategy(messages)
        if cache == "replica":
            stored = round_trip(session.state)
            session = AgentSession(session_id="cooling")
            session.state = stored
            registry = SessionCompactionRegistry(build)
        elif cache == "evicted":
            with registry.activate(AgentSession(session_id="other")):
                pass

    for _ in range(threshold):
        await phase()
    assert client.calls == threshold
    now += 59
    await phase()
    assert client.calls == threshold
    saved = session.state[registry.state_key]["strategy"]["user_turns"]
    assert saved["summary_failures"] == threshold
    assert saved["summary_backoff_until"] == 1_700_000_060.0
    now += 1
    await phase()
    assert client.calls == threshold + 1
    await phase()
    assert client.calls == threshold + 1


@pytest.mark.parametrize("error", [RuntimeError, asyncio.CancelledError])
async def test_failure_persists_pin_verdict_and_clears_context(error):
    registry = SessionCompactionRegistry(build_stack)
    session = AgentSession()
    with registry.activate(session) as stack:
        stack.middleware._force_next = True
        stack.middleware._reforce_next = True

    async def fail():
        raise error()

    context = ChatContext(
        client=None, messages=[], options={"tools": [make_recall_tool()]}, session=session
    )
    with pytest.raises(error):
        await SessionToolResultRecallMiddleware(registry).process(context, fail)
    cold = SessionCompactionRegistry(build_stack)
    with cold.activate(session) as stack:
        assert stack.middleware._force_next == (error is asyncio.CancelledError)
        assert stack.middleware._reforce_next == (error is asyncio.CancelledError)
        assert stack.middleware.pin_disabled == (error is RuntimeError)
        assert not stack.gate.take()
    with pytest.raises(RuntimeError, match="No active"):
        registry.current_stack()


@pytest.mark.parametrize("failure", ["error", "cancel", "finalizer", "success"])
@pytest.mark.parametrize("cached", [0, 1])
@pytest.mark.parametrize("session_scoped", [False, True])
async def test_forced_stream_completion_persists_degradation(failure, cached, session_scoped):
    registry = SessionCompactionRegistry(build_stack, max_cached_sessions=cached)
    session = AgentSession()
    with registry.activate(session) as stack:
        stack.middleware._force_next = True
        stack.middleware._reforce_next = True
    attempts = []
    closed = []

    async def install():
        attempts.append(dict(context.options or {}))
        pinned = len(attempts) == 1

        async def updates() -> AsyncIterator[ChatResponseUpdate]:
            yield ChatResponseUpdate(
                role="assistant", contents=[Content.from_text("partial" if pinned else "answer")]
            )
            if pinned and failure == "error":
                raise RuntimeError("stream failed")
            if pinned and failure == "cancel":
                raise asyncio.CancelledError()

        def finalize(_):
            if pinned and failure == "finalizer":
                raise RuntimeError("finalizer failed")
            return ChatResponse(
                messages=Message(
                    "assistant",
                    [
                        Content.from_function_call(
                            call_id="rec",
                            name="recall_earlier_tool_results",
                            arguments='{"values":"a123"}',
                        )
                    ]
                    if pinned
                    else ["answer"],
                )
            )

        async def cleanup():
            closed.append(pinned)

        context.result = ResponseStream(updates(), finalizer=finalize).with_cleanup_hook(cleanup)

    options = {
        "tools": [make_recall_tool()],
        "max_tokens": 12000,
        "temperature": 0.3,
        "tool_choice": "auto",
    }
    context = ChatContext(None, [], options, session=session, stream=True)
    middleware = SessionToolResultRecallMiddleware(registry) if session_scoped else stack.middleware
    if failure == "cancel":
        with pytest.raises(asyncio.CancelledError):
            await middleware.process(context, install)
    else:
        await middleware.process(context, install)
        result = context.result
        assert isinstance(result, ResponseStream)
        texts = [update.text async for update in result]
        assert texts == (["partial"] if failure == "success" else ["answer"])
        assert (await result.get_final_response()).text == (
            "" if failure == "success" else "answer"
        )
    assert closed[0] is True
    assert context.options is options
    assert len(attempts) == (2 if failure in ("error", "finalizer") else 1)
    if len(attempts) == 2:
        assert attempts[1] == options
    if not session_scoped:
        session.state[registry.state_key] = stack.to_dict()
        assert stack.middleware.forced_calls == (failure != "cancel")
    session.state = round_trip(session.state)
    cold = SessionCompactionRegistry(build_stack)
    with cold.activate(session) as restored:
        assert restored.middleware._force_next == (failure == "cancel")
        assert restored.middleware._reforce_next == (failure == "cancel")
        assert restored.middleware._awaiting_record == (failure == "success")
        assert restored.middleware.pin_disabled == (failure in ("error", "finalizer"))
        assert restored.gate.take() == (failure == "success")


async def test_gate_is_persisted_and_tool_uses_its_injected_session():
    a, b = AgentSession(session_id="a"), AgentSession(session_id="b")
    registry = SessionCompactionRegistry(build_stack)
    with registry.activate(a) as stack:
        stack.gate.arm()
    cold = SessionCompactionRegistry(build_stack)
    recall = tool(make_session_recall_tool(cold))
    assert set(recall.parameters()["properties"]) == {"values"}
    with registry.activate(a):
        denied = await recall.invoke(
            arguments={"values": "b123"},
            context=FunctionInvocationContext(recall, {}, session=b),
        )
    assert RECORD_MARKER not in denied[0].text
    result = await recall.invoke(
        arguments={"values": "a123"},
        context=FunctionInvocationContext(recall, {}, session=a),
    )
    assert RECORD_MARKER in result[0].text
    assert not a.state[cold.state_key]["gate"]["armed"]
    again = await recall.invoke(
        arguments={"values": "a123"},
        context=FunctionInvocationContext(recall, {}, session=a),
    )
    assert RECORD_MARKER not in again[0].text


@pytest.mark.parametrize("accepted", [False, True])
async def test_cold_chain_replays_content_decisions_without_summarizer_requests(accepted):
    summarizer = Summarizer("tiny" if accepted else "x" * 50000)

    def build():
        return build_stack(summarizer, budget=1000)

    registry = SessionCompactionRegistry(build)
    session = AgentSession()
    messages = [Message("user", ["task"], message_id="task")]
    messages += [build_record_message("a123 " * 900), build_record_message("b456 " * 900)]
    for index, message in enumerate(messages):
        message.message_id = f"m{index}"
    original = deepcopy(messages)
    with registry.activate(session) as stack:
        with nullcontext() if accepted else pytest.raises(CompactionBudgetExceeded):
            await stack.strategy(messages)
    assert summarizer.requests
    requests = deepcopy(summarizer.requests)
    expected = [message.to_dict() for message in messages]
    session.state = round_trip(session.state)
    cold = SessionCompactionRegistry(build)
    with cold.activate(session) as stack:
        with nullcontext() if accepted else pytest.raises(CompactionBudgetExceeded):
            await stack.strategy(original)
    assert summarizer.requests == requests
    assert [message.to_dict() for message in original] == expected


async def test_cold_user_summary_replays_same_text_and_id():
    summarizer = Summarizer()

    def build():
        return build_stack(summarizer, budget=1000)

    registry = SessionCompactionRegistry(build)
    session = AgentSession()
    messages = []
    for index in range(5):
        messages.extend(
            [
                Message("user", [f"request {index} " * 200], message_id=f"u{index}"),
                Message("assistant", ["answer"], message_id=f"a{index}"),
            ]
        )
    original = deepcopy(messages)
    with registry.activate(session) as stack:
        # The record wait is spent; the next pass may summarize the user band.
        assert isinstance(
            stack.strategy, ToolResultAndUserTurnAnchoredSummarizationCompactionStrategy
        )
        stack.strategy._declined_behind = ""
        with pytest.raises(CompactionBudgetExceeded):
            await stack.strategy(messages)
    assert summarizer.requests
    count = len(summarizer.requests)
    session.state = round_trip(session.state)
    cold = SessionCompactionRegistry(build)
    with cold.activate(session) as stack:
        with pytest.raises(CompactionBudgetExceeded):
            await stack.strategy(original)
    assert len(summarizer.requests) == count
    assert [m.to_dict() for m in original] == [m.to_dict() for m in messages]


async def test_provider_compacts_loaded_context_and_stored_history():
    registry = SessionCompactionRegistry(lambda: build_stack(budget=1000))
    provider = SessionCompactionProvider(registry, tokenizer=TOKENIZER)
    session = AgentSession()
    messages = [build_record_message("fact123 " * 900), build_record_message("fact456 " * 900)]
    session.state["in_memory"] = {"messages": deepcopy(messages)}
    context = SessionContext(input_messages=[], context_messages={"in_memory": messages})
    await provider.before_run(agent=None, session=session, context=context, state={})
    assert len(context.get_messages()) == 1
    await provider.after_run(agent=None, session=session, context=context, state={})
    assert session.state[registry.state_key]["strategy"]["kept_records"]
    with pytest.raises(RuntimeError, match="No active"):
        registry.current_stack()


def test_record_only_stack_restores_and_keeps_the_session_guard():
    registry = SessionCompactionRegistry(lambda: build_stack(composed=False), max_cached_sessions=0)
    session = AgentSession(session_id="record-only")
    with registry.activate(session) as stack:
        assert isinstance(stack.strategy, ToolResultAnchoredSummarizationCompactionStrategy)
        stack.strategy._reforce = _Reforce(frozenset({"missing123"}), 2, 1, True)
        stack.middleware._force_next = True
    session.state = round_trip(session.state)
    with registry.activate(session) as restored:
        assert restored is not stack
        assert restored.strategy.decision_state() == stack.strategy.decision_state()
        assert restored.middleware._force_next
        restored.middleware._bind_session(ChatContext(None, [], {}, session=session))
        with pytest.raises(RuntimeError, match="bound to session"):
            restored.middleware._bind_session(ChatContext(None, [], {}, session=AgentSession()))


class Model(FunctionInvocationLayer, ChatMiddlewareLayer, BaseChatClient):
    def __init__(self):
        super().__init__()
        self.prompts = []

    def _inner_get_response(self, *, messages, stream, options, **kwargs) -> Any:
        texts = [
            m.text + " ".join(str(c.result) for c in m.contents if c.type == "function_result")
            for m in messages
        ]
        self.prompts.append((texts, options.get("tool_choice")))
        choice = options.get("tool_choice")
        required = choice.get("required_function_name") if isinstance(choice, dict) else None
        if required:
            contents = [
                Content.from_function_call(
                    call_id="recall",
                    name=required,
                    arguments={"values": "recorded fact123"},
                    id=f"recall-{len(messages)}",
                )
            ]
        else:
            contents = [Content.from_text("answer")]
        response = ChatResponse(messages=Message("assistant", contents, message_id="answer"))
        if stream:

            async def updates() -> AsyncIterator[ChatResponseUpdate]:
                await asyncio.sleep(0)
                yield ChatResponseUpdate(role="assistant", contents=contents, message_id="answer")

            return ResponseStream(updates(), finalizer=lambda _: response)

        async def answer():
            await asyncio.sleep(0)
            return response

        return answer()


def harness(registry, model):
    provider = SessionCompactionProvider(registry, tokenizer=TOKENIZER, before=False)
    agent = create_harness_agent(
        model,
        name="shared",
        disable_compaction=True,
        disable_file_memory=True,
        disable_web_search=True,
        disable_tool_auto_approval=True,
        context_providers=[provider],
        middleware=[SessionToolResultRecallMiddleware(registry, compact_before=True)],
        tools=[make_session_recall_tool(registry)],
    )
    agent.context_providers.remove(provider)
    agent.context_providers.insert(1, provider)
    assert agent.context_providers[1] is provider
    return agent


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("values", [["SKU-123"], {"sku": "SKU-123"}, " \n\t"])
async def test_harness_prompt_recall_normalizes_values_and_strips_stale_asks(stream, values):
    from maf_compaction._request import RECALL_PROMPT_KEY
    from maf_compaction._toolsummary import _RECALL_INSTRUCTION, RECALL_TOOL_NAME

    def build():
        stack = build_stack(budget=100_000)
        stack.middleware.supports_tool_choice = False
        return stack

    class PromptModel(Model):
        def _inner_get_response(self, *, messages, stream, options, **kwargs):
            self.prompts.append(deepcopy(list(messages)))
            if len(self.prompts) == 1:
                assert messages[-1].text == _RECALL_INSTRUCTION
                contents = [
                    Content.from_function_call(
                        call_id="recall", name=RECALL_TOOL_NAME, arguments={"values": values}
                    )
                ]
            else:
                assert len(self.prompts) == 2
                assert all(not m.additional_properties.get(RECALL_PROMPT_KEY) for m in messages)
                results = [
                    c.result for m in messages for c in m.contents if c.type == "function_result"
                ]
                assert any(RECORD_MARKER in str(result) for result in results) == bool(
                    values != " \n\t"
                )
                assert not any("Error" in str(result) for result in results)
                contents = [Content.from_text("answer")]
            response = ChatResponse(messages=Message("assistant", contents))
            if stream:

                async def updates():
                    if contents[0].type == "function_call":
                        arguments = json.dumps({"values": values})
                        split = len(arguments) // 2
                        for part in (arguments[:split], arguments[split:]):
                            yield ChatResponseUpdate(
                                role="assistant",
                                contents=[
                                    Content.from_function_call(
                                        call_id="recall", name=RECALL_TOOL_NAME, arguments=part
                                    )
                                ],
                            )
                    else:
                        yield ChatResponseUpdate(role="assistant", contents=deepcopy(contents))

                return ResponseStream(updates(), finalizer=lambda _: response)

            async def answer():
                return response

            return answer()

    registry = SessionCompactionRegistry(build)
    model = PromptModel()
    agent = harness(registry, model)
    session = AgentSession()
    with registry.activate(session) as stack:
        stack.middleware._force_next = True
    result = agent.run("Recall SKU-123.", session=session, stream=stream)
    response = (
        await result.get_final_response() if isinstance(result, ResponseStream) else await result
    )
    assert response.text == "answer"
    assert len(model.prompts) == 2
    with registry.activate(session) as stack:
        assert stack.middleware.failed_forced_calls == 0
        assert stack.middleware.value_free_record_streak == int(values == " \n\t")
        assert stack.middleware.records_forced == int(values != " \n\t")
        assert not stack.middleware.recall_disabled


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("prompted", [False, True])
async def test_harness_never_executes_or_persists_refused_recall(stream, prompted):
    from maf_compaction._toolsummary import RECALL_TOOL_NAME

    class RefusingModel(Model):
        def _inner_get_response(self, *, messages, stream, options, **kwargs):
            self.prompts.append(deepcopy(list(messages)))
            contents = [Content.from_text("answer")]
            if len(self.prompts) == 1:
                contents = [
                    Content.from_function_call(
                        call_id="refused", name=RECALL_TOOL_NAME, arguments={"values": "SKU-123"}
                    ),
                    Content.from_text(
                        "provider refusal",
                        additional_properties={"core": {"model_output_kind": "refusal"}},
                    ),
                ]
            response = ChatResponse(messages=Message("assistant", contents))
            if stream:

                async def updates():
                    for content in contents:
                        yield ChatResponseUpdate(role="assistant", contents=[content])

                return ResponseStream(updates(), finalizer=lambda _: response)

            async def answer():
                return response

            return answer()

    registry = SessionCompactionRegistry(
        lambda: build_stack(budget=100_000, record_instruction=DEFAULT_RECORD_INSTRUCTION)
    )
    model = RefusingModel()
    agent = harness(registry, model)
    session = AgentSession()
    with registry.activate(session) as stack:
        stack.middleware.supports_tool_choice = not prompted
        stack.middleware._force_next = True
    result = agent.run("Recall SKU-123.", session=session, stream=stream)
    response = (
        await result.get_final_response() if isinstance(result, ResponseStream) else await result
    )
    assert response.text == "answer"
    assert len(model.prompts) == 2
    history = json.dumps(session.to_dict())
    assert "provider refusal" not in history
    assert '"call_id": "refused"' not in history
    assert RECORD_MARKER not in history
    assert all(c.type != "function_call" for m in response.messages for c in m.contents)
    with registry.activate(session) as stack:
        assert stack.middleware.failed_forced_calls == 1
        assert stack.middleware.value_free_record_streak == 1
        assert stack.middleware.records_forced == 0
        assert not stack.gate.take()


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("composed", [False, True])
async def test_harness_record_instruction_stays_out_of_history_and_cold_snapshots(stream, composed):
    def build():
        return build_stack(
            budget=100_000, composed=composed, record_instruction=DEFAULT_RECORD_INSTRUCTION
        )

    session = AgentSession()
    for _ in range(2):
        registry = SessionCompactionRegistry(build, max_cached_sessions=0)
        model = Model()
        agent = harness(registry, model)
        with registry.activate(session) as stack:
            stack.middleware._force_next = True
        result = agent.run("Only give the titles.", session=session, stream=stream)
        response = (
            await result.get_final_response()
            if isinstance(result, ResponseStream)
            else await result
        )
        assert response.text == "answer"
        assert len(model.prompts) == 2
        assert model.prompts[0][0][-1] == DEFAULT_RECORD_INSTRUCTION
        assert DEFAULT_RECORD_INSTRUCTION not in model.prompts[1][0]
        snapshot = session.to_dict()
        assert DEFAULT_RECORD_INSTRUCTION not in json.dumps(snapshot)
        assert "record_instruction" not in json.dumps(snapshot)
        session = AgentSession.from_dict(round_trip(snapshot))


@pytest.mark.parametrize("stream", [False, True])
async def test_harness_refuses_irreducible_overflow_before_contacting_model(stream):
    registry = SessionCompactionRegistry(lambda: build_stack(Summarizer("x" * 50_000), budget=4000))
    model = Model()
    agent = harness(registry, model)
    session = AgentSession()
    session.state["in_memory"] = {"messages": [build_record_message("fact123 " * 3000)]}
    with pytest.raises(CompactionBudgetExceeded):
        result = agent.run("continue", session=session, stream=stream)
        if stream:
            assert isinstance(result, ResponseStream)
            await result.get_final_response()
        else:
            await result
    assert model.prompts == []


@pytest.mark.parametrize("stream", [False, True])
async def test_harness_compacts_loaded_history_before_the_first_model_request(stream):
    summarizer = Summarizer("merged facts")
    registry = SessionCompactionRegistry(lambda: build_stack(summarizer, budget=4000))
    model = Model()
    agent = harness(registry, model)
    session = AgentSession()
    session.state["in_memory"] = {
        "messages": [build_record_message("fact123 " * 900), build_record_message("fact456 " * 900)]
    }
    result = agent.run("continue", session=session, stream=stream)
    if stream:
        assert isinstance(result, ResponseStream)
        await result.get_final_response()
    else:
        await result
    assert summarizer.requests
    first_prompt = "\n".join(model.prompts[0][0])
    assert "merged facts" in first_prompt
    assert "fact123" not in first_prompt
    assert "fact456" not in first_prompt


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("cold", [False, True])
async def test_interleaved_sessions_on_one_harness_match_isolated_runs(stream, cold):
    async def exercise(names, restart=cold):
        def build():
            stack = build_stack(budget=4000)
            stack.middleware.trigger_fraction = 0.2
            return stack

        registry = SessionCompactionRegistry(build)
        model = Model()
        agent = harness(registry, model)
        sessions = {name: AgentSession(session_id=name) for name in names}
        traces = {name: [] for name in names}
        for turn in range(3):
            for name in names:
                start = len(model.prompts)
                text = f"{name} request {turn} " * (450 if turn == 0 and name == "a" else 1)
                result = agent.run(
                    Message("user", [text], message_id=f"u{turn}"),
                    session=sessions[name],
                    stream=stream,
                )
                if stream:
                    assert isinstance(result, ResponseStream)
                    await result.get_final_response()
                else:
                    await result
                traces[name].extend(model.prompts[start:])
            if restart:
                sessions = {
                    name: AgentSession.from_dict(round_trip(session.to_dict()))
                    for name, session in sessions.items()
                }
                registry = SessionCompactionRegistry(build)
                agent = harness(registry, model)
        return traces, {
            name: session.state[registry.state_key] for name, session in sessions.items()
        }

    together, states = await exercise(["a", "b"])
    for name in ("a", "b"):
        alone, alone_states = await exercise([name], restart=False)
        assert together[name] == alone[name]
        assert states[name] == alone_states[name]
        assert any(isinstance(choice, dict) for _, choice in together[name]) == (name == "a")
        assert any(RECORD_MARKER in text for texts, _ in together[name] for text in texts) == (
            name == "a"
        )


async def test_distinct_sessions_can_overlap():
    registry = SessionCompactionRegistry(build_stack)
    entered, release = asyncio.Event(), asyncio.Event()
    a, b = AgentSession(session_id="a"), AgentSession(session_id="b")

    async def first():
        with registry.activate(a) as stack:
            stack.gate.arm()
            entered.set()
            await release.wait()
            assert registry.current_stack() is stack

    task = asyncio.create_task(first())
    await asyncio.wait_for(entered.wait(), timeout=5)
    with registry.activate(b) as other:
        assert not other.gate.take()
    with pytest.raises(RuntimeError, match="Overlapping"):
        with registry.activate(a):
            pass
    release.set()
    await task


def test_unknown_schema_is_rejected_without_overwriting_state():
    registry = SessionCompactionRegistry(build_stack)
    session = AgentSession()
    session.state[registry.state_key] = {"version": 999}
    with pytest.raises(ValueError, match="Unsupported"):
        with registry.activate(session):
            pass
    assert session.state[registry.state_key] == {"version": 999}


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("honours_between", [False, True])
async def test_ignored_pins_degrade_only_their_session_across_cold_restores(
    stream, honours_between
):
    session = AgentSession()
    options = {"tools": [make_recall_tool()], "max_tokens": 12000, "tool_choice": "auto"}
    registry = SessionCompactionRegistry(build_stack, max_cached_sessions=0)
    sequence = [False, True, False, False] if honours_between else [False, False]
    for index, honours in enumerate(sequence):
        registry = SessionCompactionRegistry(build_stack, max_cached_sessions=0)
        with registry.activate(session) as stack:
            stack.middleware._force_next = True
        context = ChatContext(None, [], options, session=session, stream=stream)

        async def respond():
            assert (context.options or {})["max_tokens"] == 4000
            contents = (
                [
                    Content.from_function_call(
                        call_id="rec",
                        name="recall_earlier_tool_results",
                        arguments='{"values":"a123"}',
                    )
                ]
                if honours
                else [Content.from_text("ignored")]
            )
            response = ChatResponse(messages=Message("assistant", contents))
            if stream:

                async def updates() -> AsyncIterator[ChatResponseUpdate]:
                    yield ChatResponseUpdate(role="assistant", contents=contents)

                context.result = ResponseStream(updates(), finalizer=lambda _: response)
            else:
                context.result = response

        await SessionToolResultRecallMiddleware(registry).process(context, respond)
        session.state = round_trip(session.state)
        with registry.activate(session) as stack:
            disabled = index == len(sequence) - 1
            assert stack.middleware.pin_disabled == disabled
            assert stack.middleware.pin_disabled_reason == ("ignored" if disabled else None)
            assert stack.middleware._ignored_streak == (0 if honours else (2 if disabled else 1))
            if disabled:
                assert not stack.gate.take()
                assert not stack.middleware.record_due([])
                assert isinstance(
                    stack.strategy, ToolResultAndUserTurnAnchoredSummarizationCompactionStrategy
                )
                stack.strategy.tool_results._reforce = _Reforce(frozenset({"a"}), 2, 1, True)
                assert not stack.strategy.tool_results.record_pending([])
    other = AgentSession()
    with registry.activate(other) as fresh:
        assert not fresh.middleware.pin_disabled
    # A new provider call keeps the caller's cap even when history triggers recall.
    context = ChatContext(None, [Message("user", ["large " * 10000])], options, session=session)

    async def ordinary():
        assert context.options is options
        context.result = ChatResponse(messages=Message("assistant", ["answer"]))

    await SessionToolResultRecallMiddleware(registry).process(context, ordinary)
    with registry.activate(session) as stack:
        assert not stack.middleware._force_next


@pytest.mark.parametrize("error_name", ["MiddlewareFailure", "MiddlewareTermination"])
async def test_forced_requests_preserve_framework_abort_signals(error_name):
    import agent_framework

    current = build_stack()
    current.middleware._force_next = True
    context = ChatContext(None, [], {"max_tokens": 12000, "tools": [make_recall_tool()]})
    calls = 0
    error = getattr(agent_framework, error_name)

    async def abort():
        nonlocal calls
        calls += 1
        raise error("stop")

    with pytest.raises(error):
        await current.middleware.process(context, abort)
    assert calls == 1
    assert current.middleware.failed_forced_calls == 0
    assert not current.gate.take()


@pytest.mark.parametrize(
    "original_options", [{}, {"max_tokens": 12000, "tool_choice": "auto", "temperature": 0.2}]
)
async def test_nonstream_retry_restores_request_and_is_bounded(original_options):
    current = build_stack()
    current.middleware._force_next = True
    original = Message("user", ["question"])
    original_options = {**original_options, "tools": [make_recall_tool()]}
    context = ChatContext(None, [original], original_options)
    seen = []

    async def respond():
        seen.append(dict(context.options or {}))
        assert context.messages == [original]
        if len(seen) == 1:
            context.messages = [Message("user", ["loaded history"]), original]
            context.kwargs["temporary"] = True
            raise RuntimeError("pin rejected")
        assert set(context.kwargs) == {"compaction_strategy"}
        assert not current.gate.take()
        context.result = ChatResponse(messages=Message("assistant", ["answer"]))

    await current.middleware.process(context, respond)
    assert len(seen) == 2
    assert seen[1] == original_options
    assert context.options is original_options
    assert isinstance(context.result, ChatResponse)
    assert context.result.text == "answer"
    assert current.middleware.failed_forced_calls == 1
    assert current.middleware.pin_disabled
    assert current.middleware.pin_disabled_reason == "failed"


async def test_legacy_middleware_state_starts_with_supported_pin():
    current = build_stack()
    state = current.middleware.session_state()
    state.pop("pin_disabled")
    state.pop("ignored_streak")
    current.middleware.restore_session_state(state)
    assert not current.middleware.pin_disabled
    assert current.middleware._ignored_streak == 0


@pytest.mark.parametrize("composed", [False, True])
async def test_prompt_recall_keeps_strategy_waiting_across_cold_restore(composed):
    current = build_stack(composed=composed)
    current.middleware.supports_tool_choice = False
    current.middleware._force_next = True
    messages = [
        Message("user", ["question"]),
        Message(
            "assistant", [Content.from_function_call(call_id="lookup", name="lookup", arguments={})]
        ),
        Message("tool", [Content.from_function_result(call_id="lookup", result="SKU-123")]),
    ]
    annotate_message_groups(messages)
    annotate_token_counts(messages, tokenizer=TOKENIZER)
    context = ChatContext(None, messages, {"tools": [make_recall_tool()]})

    async def respond():
        context.result = ChatResponse(
            messages=Message(
                "assistant",
                [
                    Content.from_function_call(
                        call_id="record",
                        name="recall_earlier_tool_results",
                        arguments={"values": "SKU-123"},
                    )
                ],
            )
        )

    await current.middleware.process(context, respond)
    restored = build_stack(composed=composed)
    restored.restore(round_trip(current.to_dict()))
    record = (
        restored.strategy.tool_results
        if isinstance(
            restored.strategy, ToolResultAndUserTurnAnchoredSummarizationCompactionStrategy
        )
        else restored.strategy
    )
    assert restored.middleware.pin_disabled
    assert not restored.middleware.recall_disabled
    assert record.record_pending(messages)
    restored.middleware._disable_pin("ignored")
    assert not record.record_pending(messages)


@pytest.mark.parametrize("stream", [False, True])
async def test_harness_retries_a_failed_pin_without_persisting_partial_output(stream):
    class FailingPin(Model):
        def __init__(self):
            super().__init__()
            self.options = []

        def _inner_get_response(self, *, messages, stream, options, **kwargs):
            self.options.append(dict(options))
            if isinstance(options.get("tool_choice"), dict):
                if stream:

                    async def updates() -> AsyncIterator[ChatResponseUpdate]:
                        yield ChatResponseUpdate(
                            role="assistant", contents=[Content.from_text("discard me")]
                        )
                        raise RuntimeError("provider rejected pin")

                    return ResponseStream(updates(), finalizer=lambda _: ChatResponse(messages=[]))

                async def fail():
                    raise RuntimeError("provider rejected pin")

                return fail()
            return super()._inner_get_response(
                messages=messages, stream=stream, options=options, **kwargs
            )

    registry = SessionCompactionRegistry(lambda: build_stack(budget=4000), max_cached_sessions=0)
    session = AgentSession()
    with registry.activate(session) as stack:
        stack.middleware._force_next = True
    model = FailingPin()
    agent = harness(registry, model)
    result = agent.run("continue", session=session, stream=stream, options={"max_tokens": 12000})
    if stream:
        assert isinstance(result, ResponseStream)
        response = await result.get_final_response()
    else:
        response = await result
    assert response.text == "answer"
    assert len(model.options) == 2
    assert model.options[0]["max_tokens"] == 4000
    assert model.options[1]["max_tokens"] == 12000
    assert model.options[1].get("tool_choice") == "auto"
    assert "discard me" not in json.dumps(session.to_dict())
    with registry.activate(session) as restored:
        assert restored.middleware.pin_disabled
        assert not restored.gate.take()


async def test_unpinned_stream_failure_propagates_without_a_third_request():
    current = build_stack()
    current.middleware._force_next = True
    context = ChatContext(None, [], {"tools": [make_recall_tool()]}, stream=True)
    calls = 0

    async def install():
        nonlocal calls
        calls += 1

        async def updates() -> AsyncIterator[ChatResponseUpdate]:
            raise RuntimeError("provider unavailable")
            yield ChatResponseUpdate()

        context.result = ResponseStream(updates(), finalizer=lambda _: ChatResponse(messages=[]))

    await current.middleware.process(context, install)
    assert isinstance(context.result, ResponseStream)
    with pytest.raises(RuntimeError, match="provider unavailable"):
        await context.result.get_final_response()
    assert calls == 2
    assert current.middleware.pin_disabled


@pytest.mark.parametrize("session_scoped", [False, True])
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("overhead", [970, 1100])
async def test_post_call_budget_refusal_preserves_delivered_response(
    session_scoped, stream, overhead
):
    def factory():
        stack = build_stack(budget=1000)
        assert isinstance(
            stack.strategy, ToolResultAndUserTurnAnchoredSummarizationCompactionStrategy
        )
        stack.strategy.estimate_margin = 0.05
        return stack

    stack = factory()
    registry = SessionCompactionRegistry(factory)
    context = ChatContext(
        None,
        [Message("user", ["question"])],
        {"instructions": "x" * (overhead * 4)},
        session=AgentSession(),
        stream=stream,
    )
    middleware = (
        SessionToolResultRecallMiddleware(registry, compact_before=True)
        if session_scoped
        else stack.middleware
    )
    if not session_scoped:
        context.kwargs["compaction_strategy"] = stack.strategy
    response = ChatResponse(messages=Message("assistant", ["delivered"]))

    async def updates():
        yield ChatResponseUpdate(role="assistant", contents=[Content.from_text("delivered")])

    result = ResponseStream(updates(), finalizer=lambda _: response) if stream else response
    calls = 0

    async def send_anyway():
        nonlocal calls
        calls += 1
        with pytest.raises(CompactionBudgetExceeded):
            await context.kwargs["compaction_strategy"](context.messages)
        context.result = result

    await middleware.process(context, send_anyway)
    assert calls == 1
    assert context.result is result
    if stream:
        assert isinstance(result, ResponseStream)
        assert (await result.get_final_response()).text == "delivered"
    with registry.activate(context.session) if session_scoped else nullcontext(stack) as current:
        assert not current.middleware._force_next
        assert not current.middleware.pin_disabled


@pytest.mark.parametrize("composed", [False, True])
@pytest.mark.parametrize("with_value_between", [False, True])
@pytest.mark.parametrize("trigger_fraction", [0.0001, 0.8])
@pytest.mark.parametrize("prompt_recall", [False, True])
@pytest.mark.parametrize("reforce", ["bound", "wrapped", None])
async def test_value_free_records_disable_forcing_across_cold_restores(
    composed, with_value_between, trigger_fraction, prompt_recall, reforce, caplog
):
    def factory():
        stack = build_stack(budget=100_000, composed=composed, reforce=reforce)
        record = (
            stack.strategy.tool_results
            if isinstance(
                stack.strategy, ToolResultAndUserTurnAnchoredSummarizationCompactionStrategy
            )
            else stack.strategy
        )
        record.trigger_fraction = trigger_fraction
        stack.middleware.max_groups_before_record = 1
        stack.middleware.supports_tool_choice = not prompt_recall
        return stack

    session = AgentSession()
    messages = [
        Message("user", ["Return only titles."], message_id="head"),
        Message(
            "assistant",
            [Content.from_function_call(call_id="lookup", name="fetch", arguments={})],
            message_id="lookup-call",
        ),
        Message(
            "tool",
            [
                Content.from_function_result(
                    call_id="lookup", result="Limit=VALUE-123; Region=REGION-456"
                )
            ],
            message_id="lookup-result",
        ),
    ]
    options = {"tools": [make_recall_tool()], "tool_choice": "auto"}
    answers = (
        ["Titles only", "VALUE-123", "Titles only", "Titles only"]
        if with_value_between
        else ["Titles only", "Titles only"]
    )
    streak = 0
    for index, answer in enumerate(answers):
        registry = SessionCompactionRegistry(factory, max_cached_sessions=0)
        with registry.activate(session) as stack:
            assert not stack.middleware.recall_disabled
            stack.middleware._force_next = True
        context = ChatContext(None, list(messages), options, session=session)
        call_id = f"recall-{index}"
        call = Message(
            "assistant",
            [
                Content.from_function_call(
                    call_id=call_id,
                    name="recall_earlier_tool_results",
                    arguments={"values": answer},
                )
            ],
            message_id=f"{call_id}-call",
        )

        async def respond():
            assert context.options is not None
            if prompt_recall:
                assert context.options["tool_choice"] == "auto"
                assert "instructions" not in context.options
                assert "recall_earlier_tool_results" in context.messages[-1].text
            else:
                assert context.options["tool_choice"]["mode"] == "required"
            context.result = ChatResponse(messages=call)

        await SessionToolResultRecallMiddleware(registry).process(context, respond)
        assert list(context.messages) == messages
        messages.extend(
            [
                call,
                Message(
                    "tool",
                    [
                        Content.from_function_result(
                            call_id=call_id, result=f"{RECORD_MARKER} {answer}"
                        )
                    ],
                    message_id=f"{call_id}-result",
                ),
            ]
        )
        streak = 0 if answer == "VALUE-123" else streak + 1
        for _ in range(2):
            registry = SessionCompactionRegistry(factory, max_cached_sessions=0)
            with registry.activate(session) as stack:
                record = (
                    stack.strategy.tool_results
                    if isinstance(
                        stack.strategy, ToolResultAndUserTurnAnchoredSummarizationCompactionStrategy
                    )
                    else stack.strategy
                )
                await record(deepcopy(messages))
                assert stack.middleware.value_free_record_streak == streak
                assert stack.middleware.recall_disabled == (streak == 2)
                assert stack.middleware.pin_disabled_reason == (
                    "value_free" if streak == 2 else "unsupported" if prompt_recall else None
                )
            session.state = round_trip(session.state)
    registry = SessionCompactionRegistry(factory, max_cached_sessions=0)
    with registry.activate(session) as stack:
        assert stack.middleware.pin_disabled
        assert not stack.middleware.record_due(messages)
        stack.middleware.forget_pending()
        assert stack.middleware.pin_disabled_reason == "value_free"
    context = ChatContext(None, messages, options, session=session)

    async def unpinned():
        assert context.options is options
        context.result = ChatResponse(messages=Message("assistant", ["answer"]))

    await SessionToolResultRecallMiddleware(registry).process(context, unpinned)
    assert f"session {session.session_id}: value_free" in caplog.text
    with registry.activate(AgentSession()) as fresh:
        assert not fresh.middleware.pin_disabled
        assert fresh.middleware.value_free_record_streak == 0


@pytest.mark.parametrize("shed", ["never", "before_force", "during_force"])
@pytest.mark.parametrize(
    "answers",
    [
        ["Titles only", "Titles only"],
        ["Titles only", "VALUE-123", "Titles only", "Titles only"],
        ["VALUE-123", "VALUE-123"],
    ],
)
@pytest.mark.parametrize("reforce", ["bound", None])
async def test_recall_values_survive_overflow_before_record_and_cold_restore(
    shed, answers, reforce
):
    def factory():
        stack = build_stack(budget=2000, reforce=reforce)
        stack.middleware.max_groups_before_record = 1
        assert isinstance(
            stack.strategy, ToolResultAndUserTurnAnchoredSummarizationCompactionStrategy
        )
        stack.strategy.tool_results.fallback.keep_tail_groups = 0
        return stack

    session = AgentSession()
    messages = [Message("user", ["Return only titles."], message_id="head")]
    streak = 0
    for index, answer in enumerate(answers):
        call_id = f"lookup-{index}"
        messages.extend(
            [
                Message(
                    "assistant",
                    [Content.from_function_call(call_id=call_id, name="fetch", arguments={})],
                ),
                Message(
                    "tool",
                    [Content.from_function_result(call_id=call_id, result="VALUE-123 " * 1500)],
                ),
                Message("user", ["Continue."], message_id=f"tail-{index}"),
            ]
        )
        registry = SessionCompactionRegistry(factory, max_cached_sessions=0)
        with registry.activate(session) as stack:
            stack.middleware._force_next = True
        for message in messages[-3:-1]:
            set_preserved(message, preserved=True, reason=PRESERVE_REASON_UNRECORDED)
        if shed == "before_force":
            with registry.activate(session) as stack:
                await stack.strategy(messages)
            messages[:] = project_included_messages(messages)
            session.state = round_trip(session.state)
            registry = SessionCompactionRegistry(factory, max_cached_sessions=0)
        context = ChatContext(
            None, [messages[-1]], {"tools": [make_recall_tool()]}, session=session
        )

        async def respond():
            context.messages = messages
            if shed == "during_force":
                await context.kwargs["compaction_strategy"](messages)
                messages[:] = project_included_messages(messages)
            if shed != "never":
                assert "VALUE-123" not in " ".join(
                    str(c.result)
                    for m in project_included_messages(messages)
                    for c in m.contents
                    if c.type == "function_result" and c.call_id == call_id
                )
            context.result = ChatResponse(
                messages=Message(
                    "assistant",
                    [
                        Content.from_function_call(
                            call_id=f"recall-{index}",
                            name="recall_earlier_tool_results",
                            arguments={"values": answer},
                        )
                    ],
                )
            )

        await SessionToolResultRecallMiddleware(registry, compact_before=True).process(
            context, respond
        )
        assert isinstance(context.result, ChatResponse)
        messages.extend(context.result.messages)
        messages.append(
            Message(
                "tool",
                [
                    Content.from_function_result(
                        call_id=f"recall-{index}", result=f"{RECORD_MARKER} {answer}"
                    )
                ],
            )
        )
        session.state = round_trip(session.state)
        streak = streak + 1 if answer == "Titles only" else 0
        for _ in range(2):
            registry = SessionCompactionRegistry(factory, max_cached_sessions=0)
            with registry.activate(session) as stack:
                assert isinstance(
                    stack.strategy, ToolResultAndUserTurnAnchoredSummarizationCompactionStrategy
                )
                await stack.strategy.tool_results.compact_against(
                    messages, prompt_tokens=0, trigger_tokens=1
                )
                assert stack.strategy.tool_results.last_record_value_judgment == (
                    "value_free" if answer == "Titles only" else "has_values"
                )
                assert stack.middleware.value_free_record_streak == streak
                assert stack.middleware.pin_disabled_reason == (
                    "value_free" if streak == 2 else None
                )
            session.state = round_trip(session.state)


async def test_value_judgment_replays_after_restoring_the_same_stack():
    stack = build_stack(budget=100_000, composed=False)
    assert isinstance(stack.strategy, ToolResultAnchoredSummarizationCompactionStrategy)
    stack.middleware._force_next = True
    messages = [
        Message("user", ["Return only titles."]),
        Message(
            "assistant", [Content.from_function_call(call_id="fetch", name="fetch", arguments={})]
        ),
        Message("tool", [Content.from_function_result(call_id="fetch", result="VALUE-123")]),
    ]
    await stack.strategy(messages)
    before_record = round_trip(stack.to_dict())
    messages = [
        messages[0],
        Message(
            "assistant",
            [
                Content.from_function_call(
                    call_id="recall",
                    name="recall_earlier_tool_results",
                    arguments={"values": "Titles only"},
                )
            ],
        ),
        Message(
            "tool",
            [Content.from_function_result(call_id="recall", result=f"{RECORD_MARKER} Titles only")],
        ),
    ]
    for _ in range(2):
        stack.restore(before_record)
        await stack.strategy(messages)
        assert stack.middleware.value_free_record_streak == 1
        assert stack.strategy.last_record_value_judgment == "value_free"
