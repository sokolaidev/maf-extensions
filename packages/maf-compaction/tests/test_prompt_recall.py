"""Prompt recall fallback, request isolation and session backoff."""

import asyncio
import json
import logging
from copy import deepcopy

import pytest
from agent_framework import (
    AgentSession,
    CharacterEstimatorTokenizer,
    ChatContext,
    ChatResponse,
    ChatResponseUpdate,
    Content,
    FunctionTool,
    Message,
    MiddlewareFailure,
    MiddlewareTermination,
    ResponseStream,
)
from agent_framework._compaction import EXCLUDED_KEY, included_token_count
from agent_framework.exceptions import ChatClientInvalidRequestException

from maf_compaction import (
    AnchoredCompactionStrategy,
    CompactionBudgetExceeded,
    MinimumGainAnchoredCompactionStrategy,
    RecallGate,
    ToolResultAnchoredSummarizationCompactionStrategy,
    ToolResultAndUserTurnAnchoredSummarizationCompactionStrategy,
    ToolResultRecallMiddleware,
    UserTurnAnchoredSummarizationCompactionStrategy,
    make_recall_tool,
)
from maf_compaction._media import annotate_token_counts
from maf_compaction._request import RECALL_PROMPT_KEY
from maf_compaction._toolsummary import _RECALL_INSTRUCTION, RECALL_TOOL_NAME, RECORD_MARKER

pytestmark = pytest.mark.anyio
REJECTION = "Ollama does not support tool_choice mode 'required'. Use 'auto' or 'none'."


def recall(**kwargs):
    gate = RecallGate()
    middleware = ToolResultRecallMiddleware(
        max_input_tokens=1000,
        tokenizer=CharacterEstimatorTokenizer(),
        arm=gate.arm,
        disarm=gate.disarm,
        **kwargs,
    )
    middleware._force_next = True
    return middleware, gate


def record_call():
    return Content.from_function_call(
        call_id="record", name=RECALL_TOOL_NAME, arguments={"values": "SKU-123"}
    )


@pytest.mark.parametrize("forced", [False, True])
@pytest.mark.parametrize("disabled", [False, True])
@pytest.mark.parametrize("sequence", [list, tuple])
async def test_retained_prompt_copies_are_removed_before_the_next_request(
    forced, disabled, sequence
):
    middleware, gate = recall(supports_tool_choice=False)
    original = Message("user", [_RECALL_INSTRUCTION])
    context = ChatContext(None, [original], {"tools": [make_recall_tool(gate)]})
    retained = []

    async def first():
        retained.extend(deepcopy(list(context.messages)))
        context.result = ChatResponse(messages=Message("assistant", [record_call()]))

    await middleware.process(context, first)
    assert retained[-1].additional_properties[RECALL_PROMPT_KEY]
    middleware._force_next = forced
    if disabled:
        middleware._disable_pin("failed")
    context.messages = sequence([*retained, deepcopy(retained[-1])])

    async def later():
        prompts = [m for m in context.messages if m.additional_properties.get(RECALL_PROMPT_KEY)]
        assert len(prompts) == int(forced and not disabled)
        assert context.messages[0].text == original.text
        assert not context.messages[0].additional_properties.get(RECALL_PROMPT_KEY)
        context.result = ChatResponse(messages=Message("assistant", ["answer"]))

    await middleware.process(context, later)
    assert len(context.messages) == 1


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("values", ["", " \n\t", [], {}])
async def test_blank_recall_is_value_free_across_restore_without_recording(stream, values):
    middleware, gate = recall(supports_tool_choice=False)
    for index in range(2):
        middleware._force_next = True
        tool = make_recall_tool(gate)
        context = ChatContext(None, [], {"tools": [tool]}, stream=stream)

        async def send():
            call = Content.from_function_call(
                call_id="reused", name=RECALL_TOOL_NAME, arguments={"values": values}
            )
            response = ChatResponse(messages=Message("assistant", [call]))
            if stream:

                async def updates():
                    yield ChatResponseUpdate(role="assistant", contents=[deepcopy(call)])

                context.result = ResponseStream(updates(), finalizer=lambda _: response)
            else:
                context.result = response

        await middleware.process(context, send)
        response = (
            await context.result.get_final_response()
            if isinstance(context.result, ResponseStream)
            else context.result
        )
        assert isinstance(response, ChatResponse)
        arguments = response.messages[0].contents[0].arguments
        assert isinstance(arguments, dict)
        assert RECORD_MARKER not in tool(arguments["values"])
        assert middleware.value_free_record_streak == index + 1
        assert middleware.failed_forced_calls == 0
        snapshot = json.loads(json.dumps(middleware.session_state()))
        middleware, gate = recall(supports_tool_choice=False)
        middleware.restore_session_state(snapshot)
    assert middleware.recall_disabled
    assert middleware.pin_disabled_reason == "value_free"


@pytest.mark.parametrize("values", ["", " \n\t"])
def test_blank_tool_consumes_permission_without_recording(values):
    gate = RecallGate()
    gate.arm()
    tool = make_recall_tool(gate)
    assert RECORD_MARKER not in tool(values)
    assert RECORD_MARKER not in tool("SKU-123")
    assert RECORD_MARKER not in make_recall_tool()(values)


@pytest.mark.parametrize(
    ("stream", "rejection_at"),
    [(False, "request"), (True, "request"), (True, "stream"), (True, "finalizer")],
)
async def test_unsupported_pin_retries_with_prompt_and_keeps_record_armed(
    stream, rejection_at, caplog
):
    middleware, gate = recall()
    original = Message("user", ["question"])
    options = {
        "tools": [make_recall_tool(gate)],
        "instructions": "Keep the host's restrictions.",
        "max_tokens": 12000,
        "temperature": 0.3,
    }
    context = ChatContext(
        None, [original], options, session=AgentSession(session_id="recall-session"), stream=stream
    )
    requests = []
    closed = []

    async def send():
        requests.append(dict(context.options or {}))
        assert context.options is not None
        if len(requests) == 1:
            assert context.options["tool_choice"]["mode"] == "required"
            context.messages = [Message("user", ["loaded"]), original]
            context.kwargs["temporary"] = True
            if rejection_at == "request":
                raise ChatClientInvalidRequestException(REJECTION)

            async def broken_updates():
                try:
                    yield ChatResponseUpdate(
                        role="assistant", contents=[Content.from_text("hidden")]
                    )
                    if rejection_at == "stream":
                        raise ChatClientInvalidRequestException(REJECTION)
                finally:
                    closed.append(True)

            def finalize(_):
                raise ChatClientInvalidRequestException(REJECTION)

            context.result = ResponseStream(broken_updates(), finalizer=finalize)
            return
        assert list(context.messages[:-1]) == [original]
        assert context.messages[-1].role == "user"
        assert context.messages[-1].text == _RECALL_INSTRUCTION
        assert set(context.kwargs) == {"compaction_strategy"}
        assert context.result is None
        assert context.options["tool_choice"] == "auto"
        assert context.options["instructions"] == options["instructions"]
        assert context.options["max_tokens"] == 4000
        assert context.options["temperature"] == 0.3
        response = ChatResponse(messages=Message("assistant", [record_call()]))
        if stream:

            async def updates():
                yield ChatResponseUpdate(role="assistant", contents=[record_call()])

            context.result = ResponseStream(updates(), finalizer=lambda _: response)
        else:
            context.result = response

    with caplog.at_level(logging.WARNING, logger="maf_compaction._toolsummary"):
        await middleware.process(context, send)
    assert len(requests) == 2
    assert context.options is options
    assert context.messages == [original]
    if stream:
        assert isinstance(context.result, ResponseStream)
        assert all([update.text != "hidden" async for update in context.result])
    assert closed == ([] if rejection_at == "request" else [True])
    assert middleware.pin_disabled
    assert middleware.pin_disabled_reason == "unsupported"
    assert not middleware.recall_disabled
    assert middleware.forced_calls == 1
    assert middleware.failed_forced_calls == 0
    assert middleware.session_state()["awaiting_record"]
    assert make_recall_tool(gate)("SKU-123").startswith(RECORD_MARKER)
    warnings = [record for record in caplog.records if record.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "recall-session" in warnings[0].message
    assert "unsupported" in warnings[0].message


@pytest.mark.parametrize("declared", [False, True])
async def test_prompt_mode_survives_restore_and_leaves_ordinary_options_unchanged(declared):
    middleware, gate = recall(supports_tool_choice=not declared)
    options = {"tools": [make_recall_tool()], "instructions": "Host instruction."}
    context = ChatContext(None, [], options)
    calls = 0

    async def send():
        nonlocal calls
        calls += 1
        assert context.options is not None
        if context.options.get("tool_choice") != "auto":
            raise ChatClientInvalidRequestException(REJECTION)
        assert context.options["instructions"] == options["instructions"]
        assert [message.text for message in context.messages] == [_RECALL_INSTRUCTION]
        context.result = ChatResponse(messages=Message("assistant", [record_call()]))

    await middleware.process(context, send)
    assert calls == (1 if declared else 2)
    state = json.loads(json.dumps(middleware.session_state()))
    restored, restored_gate = recall()
    restored.restore_session_state(state)
    assert restored.record_due([])
    restored._force_next = True
    await restored.process(context, send)
    assert calls == (2 if declared else 3)
    assert restored_gate.take()
    assert context.options is options

    async def ordinary():
        assert context.options is options
        assert not restored_gate.take()
        context.result = ChatResponse(messages=Message("assistant", ["answer"]))

    await restored.process(context, ordinary)
    restored.forget_pending()
    assert restored.pin_disabled_reason == "unsupported"
    assert not restored.recall_disabled


@pytest.mark.parametrize(
    "error",
    [
        RuntimeError("tool_choice transport failure"),
        ChatClientInvalidRequestException("max_tokens is unsupported"),
    ],
)
async def test_unrelated_errors_keep_the_ordinary_retry(error, caplog):
    middleware, gate = recall()
    options = {"tools": [make_recall_tool()]}
    context = ChatContext(None, [], options, session=AgentSession(session_id="failure-session"))
    calls = 0

    async def send():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise error
        assert context.options is options
        assert not gate.take()
        context.result = ChatResponse(messages=Message("assistant", ["answer"]))

    await middleware.process(context, send)
    assert calls == 2
    assert middleware.recall_disabled
    assert middleware.pin_disabled_reason == "failed"
    assert "failure-session: failed" in caplog.text


@pytest.mark.parametrize("stream", [False, True])
async def test_failed_prompt_retry_is_buffered_and_has_one_ordinary_retry(stream, caplog):
    middleware, gate = recall()
    options = {"tools": [make_recall_tool()]}
    context = ChatContext(None, [], options, stream=stream)
    calls = 0

    async def send():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ChatClientInvalidRequestException(REJECTION)
        if calls == 2:
            assert context.options is not None
            assert context.options["tool_choice"] == "auto"
            if not stream:
                raise RuntimeError("prompt failed")

            async def updates():
                yield ChatResponseUpdate(role="assistant", contents=[record_call()])
                raise RuntimeError("prompt failed")

            context.result = ResponseStream(
                updates(), finalizer=lambda _: ChatResponse(messages=[])
            )
            return
        assert context.options is options
        assert context.result is None
        assert not context.messages
        assert not gate.take()
        raise RuntimeError("ordinary failed")

    with pytest.raises(RuntimeError, match="ordinary failed"):
        await middleware.process(context, send)
    assert calls == 3
    assert middleware.failed_forced_calls == 1
    assert middleware.pin_disabled_reason == "failed"
    assert middleware.recall_disabled
    assert context.options is options
    assert not context.messages
    assert len([record for record in caplog.records if record.levelno == logging.WARNING]) == 2


@pytest.mark.parametrize(
    "signal", [asyncio.CancelledError, MiddlewareFailure, MiddlewareTermination]
)
async def test_prompt_retry_abort_requeues_without_disabling_recall(signal):
    middleware, gate = recall()
    middleware._reforce_next = True
    options = {"tools": [make_recall_tool()]}
    context = ChatContext(None, [], options)
    calls = 0

    async def send():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ChatClientInvalidRequestException(REJECTION)
        raise signal()

    with pytest.raises(signal):
        await middleware.process(context, send)
    assert calls == 2
    assert middleware.pin_disabled_reason == "unsupported"
    assert not middleware.recall_disabled
    assert middleware.session_state()["force_next"]
    assert middleware.session_state()["reforce_next"]
    assert middleware.forced_calls == middleware.reforced_calls == 0
    assert not context.messages
    assert not gate.take()
    assert context.options is options


@pytest.mark.parametrize("reason", ["ignored", "value_free"])
async def test_prompt_mode_retains_backoff_across_restore(reason, caplog):
    middleware, _ = recall(supports_tool_choice=False)
    options = {"tools": [make_recall_tool()]}
    context = ChatContext(None, [], options, session=AgentSession(session_id="backoff-session"))

    async def send():
        context.result = ChatResponse(
            messages=Message("assistant", ["answer"] if reason == "ignored" else [record_call()])
        )

    for index in range(2):
        middleware._force_next = True
        await middleware.process(context, send)
        if reason == "value_free":
            middleware._observe_record_values(f"record-{index}", False)
        snapshot = json.loads(json.dumps(middleware.session_state()))
        middleware, _ = recall()
        middleware.restore_session_state(snapshot)
    assert middleware.pin_disabled_reason == reason
    assert middleware.recall_disabled
    assert not middleware.record_due([])
    assert f"backoff-session: {reason}" in caplog.text


@pytest.mark.parametrize("choice", ["none", {"mode": "none"}])
async def test_declared_unsupported_route_defers_tools_disabled_requests(choice):
    middleware, gate = recall(supports_tool_choice=False)
    options = {"tools": [make_recall_tool()], "tool_choice": choice}
    context = ChatContext(None, [], options)

    async def send():
        assert context.options is options
        assert not gate.take()
        context.result = ChatResponse(messages=Message("assistant", ["answer"]))

    await middleware.process(context, send)
    assert middleware.session_state()["force_next"]
    assert middleware.forced_calls == 0
    assert not middleware.recall_disabled


async def test_prompt_fallback_uses_the_forced_deadline():
    middleware, gate = recall(forced_call_timeout_seconds=0.01)
    options = {"tools": [make_recall_tool()]}
    context = ChatContext(None, [], options)
    calls = 0
    cancelled = asyncio.Event()

    async def send():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ChatClientInvalidRequestException(REJECTION)
        if calls == 2:
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        assert cancelled.is_set()
        assert context.options is options
        assert not gate.take()
        context.result = ChatResponse(messages=Message("assistant", ["answer"]))

    await middleware.process(context, send)
    assert calls == 3
    assert middleware.recall_disabled
    assert middleware.pin_disabled_reason == "failed"


@pytest.mark.parametrize("form", ["callable", "function_tool", "nested_spec", "flat_spec"])
@pytest.mark.parametrize("feedback", ["", "Retain SKU-123 from lookup."])
@pytest.mark.parametrize("single", [False, True])
@pytest.mark.parametrize("replace_messages", [False, True])
async def test_prompt_advertises_only_recall_without_mutating_registered_tools(
    form, feedback, single, replace_messages
):
    middleware, gate = recall(supports_tool_choice=False)
    middleware._feedback = feedback
    tool = make_recall_tool(gate)
    if form != "callable":
        tool = FunctionTool(name=RECALL_TOOL_NAME, func=tool)
    if form in ("nested_spec", "flat_spec"):
        assert isinstance(tool, FunctionTool)
        tool = tool.to_json_schema_spec()
        if form == "flat_spec":
            tool = tool["function"]
    snapshot = (
        deepcopy(tool) if isinstance(tool, dict) else getattr(tool, "description", tool.__doc__)
    )
    other = FunctionTool(name="lookup", func=lambda: "result")
    options = {"tools": tool if single else [other, tool], "max_tokens": 9000}
    original = Message("tool", [Content.from_function_result(call_id="lookup", result="SKU-123")])
    messages = [original]
    context = ChatContext(None, messages, options)
    retained = []
    summary = Message("assistant", ["compacted history"])

    async def send():
        assert context.options is not None
        assert "instructions" not in context.options
        assert context.options["max_tokens"] == 4000
        advertised = context.options["tools"]
        assert len(advertised) == 1
        selected = advertised[0]
        spec = selected.get("function", selected) if isinstance(selected, dict) else None
        if spec is not None:
            assert spec["name"] == RECALL_TOOL_NAME
            description = spec.get("description", "")
        else:
            assert (
                getattr(selected, "name", getattr(selected, "__name__", None)) == RECALL_TOOL_NAME
            )
            description = getattr(selected, "description", "")
        if feedback:
            assert description.endswith(feedback)
        assert list(context.messages[:-1]) == messages
        assert context.messages[-1].text == _RECALL_INSTRUCTION
        if replace_messages:
            context.messages = list(context.messages)
        retained.append(context.messages)
        assert isinstance(context.messages, list)
        context.messages.insert(0, summary)
        context.result = ChatResponse(messages=Message("assistant", [record_call()]))

    await middleware.process(context, send)
    assert context.options is options
    assert messages == [original]
    assert context.messages == retained[0] == [summary, original]
    assert (
        tool if isinstance(tool, dict) else getattr(tool, "description", tool.__doc__)
    ) == snapshot


@pytest.mark.parametrize("prompted", [False, True])
@pytest.mark.parametrize(
    ("contents", "outcome"),
    [
        ([Content.from_text("private answer")], "text"),
        (
            [
                Content.from_function_call(
                    call_id="secret", name="private_lookup", arguments={"key": "secret"}
                )
            ],
            "other_tool",
        ),
        (
            [
                Content.from_function_call(call_id="secret", name="private_lookup", arguments={}),
                Content.from_text("private answer"),
            ],
            "other_tool+text",
        ),
        ([], "other_or_empty"),
    ],
)
async def test_ignored_warning_identifies_request_mode_and_response_kind(
    prompted, contents, outcome, caplog
):
    middleware, _ = recall(supports_tool_choice=not prompted)
    options = {"tools": [make_recall_tool()]}
    context = ChatContext(None, [], options, session=AgentSession(session_id="diagnostic-session"))

    async def send():
        context.result = ChatResponse(messages=Message("assistant", contents))

    for _ in range(2):
        middleware._force_next = True
        await middleware.process(context, send)
    ignored = [record.message for record in caplog.records if ": ignored" in record.message]
    assert len(ignored) == 1
    mode = "prompted" if prompted else "pinned"
    assert f"diagnostic-session: ignored (mode={mode}, response={outcome})" in ignored[0]
    assert "private" not in caplog.text and "secret" not in caplog.text
    assert middleware.ignored_forced_calls == 2
    assert middleware.recall_disabled


async def test_missing_recall_tool_defers_prompt_without_hiding_other_tools():
    middleware, gate = recall(supports_tool_choice=False)
    options = {"tools": [FunctionTool(name="lookup", func=lambda: "result")]}
    context = ChatContext(None, [], options)

    async def send():
        assert context.options is options
        assert not context.messages
        assert not gate.take()
        context.result = ChatResponse(messages=Message("assistant", ["answer"]))

    await middleware.process(context, send)
    assert middleware.session_state()["force_next"]
    assert middleware.forced_calls == 0


@pytest.mark.parametrize("mode", ["recompact", "boundary", "fold"])
@pytest.mark.parametrize("tail", [1, 2])
async def test_prompt_recall_preserves_real_user_anchors_during_composed_compaction(mode, tail):
    tokenizer = CharacterEstimatorTokenizer()
    requests = []

    class Summarizer:
        async def get_response(self, messages, **kwargs):
            requests.append([message.text for message in messages])
            return ChatResponse(messages=Message("assistant", ["short summary"]))

    users = UserTurnAnchoredSummarizationCompactionStrategy(
        max_input_tokens=3000,
        tokenizer=tokenizer,
        client=Summarizer(),
        keep_head_user_turns=1,
        keep_tail_user_turns=tail,
        min_band_share=0,
        summary_mode=mode,
        remembered_requests=2,
        max_summary_input_tokens=100_000,
    )
    strategy = ToolResultAndUserTurnAnchoredSummarizationCompactionStrategy(
        tokenizer=tokenizer,
        tool_results=ToolResultAnchoredSummarizationCompactionStrategy(
            max_input_tokens=3000, tokenizer=tokenizer
        ),
        user_turns=users,
    )
    middleware, gate = recall(supports_tool_choice=False)
    options = {"tools": [make_recall_tool(gate)]}
    head = Message("user", ["Keep this initial request verbatim."])
    band = [Message("user", [f"Earlier request {index}. " * 300]) for index in range(3)]
    anchors = [
        Message("user", [f"Unanswered question {index}: preserve ACCOUNT-{index:04d} exactly."])
        for index in range(tail)
    ]
    messages = [head, *band, *anchors]
    context = ChatContext(None, messages, options, kwargs={"compaction_strategy": strategy})

    async def send():
        assert isinstance(context.messages, list)
        assert context.messages[-1].text == _RECALL_INSTRUCTION
        assert await context.kwargs["compaction_strategy"](context.messages)
        assert requests and users.user_compactions == 1
        assert all(message.additional_properties.get(EXCLUDED_KEY) for message in band)
        for anchor in [head, *anchors]:
            assert not anchor.additional_properties.get(EXCLUDED_KEY, False)
            assert anchor in context.messages
            assert all(anchor.text not in text for request in requests for text in request)
        assert all(_RECALL_INSTRUCTION not in text for request in requests for text in request)
        assert context.messages[-1].text == _RECALL_INSTRUCTION
        assert not context.messages[-1].additional_properties.get(EXCLUDED_KEY, False)
        context.result = ChatResponse(messages=Message("assistant", [record_call()]))

    await middleware.process(context, send)
    assert middleware.failed_forced_calls == 0
    assert all(message.text != _RECALL_INSTRUCTION for message in context.messages)
    assert context.options is options


async def test_prompt_recall_message_still_counts_towards_the_composed_sending_budget():
    tokenizer = CharacterEstimatorTokenizer()
    middleware, gate = recall(supports_tool_choice=False)
    strategy = ToolResultAndUserTurnAnchoredSummarizationCompactionStrategy(
        tokenizer=tokenizer,
        tool_results=ToolResultAnchoredSummarizationCompactionStrategy(
            max_input_tokens=4000, tokenizer=tokenizer
        ),
        user_turns=UserTurnAnchoredSummarizationCompactionStrategy(
            max_input_tokens=4000, tokenizer=tokenizer, client=None
        ),
    )
    options = {"tools": [make_recall_tool(gate)]}
    budget = strategy.sending_budget(options)
    question = Message("user", ["x" * ((budget - 64) * 4)])
    with_prompt = [question, Message("user", [_RECALL_INSTRUCTION])]
    annotate_token_counts(with_prompt, tokenizer=tokenizer)
    assert included_token_count([question]) < budget < included_token_count(with_prompt)
    context = ChatContext(None, [question], options, kwargs={"compaction_strategy": strategy})
    refusals = []
    calls = 0

    async def send():
        nonlocal calls
        calls += 1
        if calls == 1:
            with pytest.raises(CompactionBudgetExceeded) as failure:
                await context.kwargs["compaction_strategy"](context.messages)
            refusals.append(failure.value)
            raise failure.value
        assert list(context.messages) == [question]
        assert context.options is options
        assert not await context.kwargs["compaction_strategy"](context.messages)
        context.result = ChatResponse(messages=Message("assistant", ["ordinary answer"]))

    await middleware.process(context, send)
    assert calls == 2 and len(refusals) == 1
    assert refusals[0].budget == budget
    assert middleware.failed_forced_calls == 1
    assert not question.additional_properties.get(EXCLUDED_KEY, False)


@pytest.mark.parametrize("composed", [False, True])
@pytest.mark.parametrize(
    "fallback_type", [AnchoredCompactionStrategy, MinimumGainAnchoredCompactionStrategy]
)
@pytest.mark.parametrize("keep_tokens", [32, 100_000], ids=["shortening", "shedding"])
async def test_prompt_recall_preserves_the_latest_tool_group_in_fallback(
    composed, fallback_type, keep_tokens
):
    tokenizer = CharacterEstimatorTokenizer()
    fallback = fallback_type(
        max_input_tokens=4000,
        tokenizer=tokenizer,
        keep_head_groups=1,
        keep_tail_groups=1,
        keep_tokens=keep_tokens,
    )
    records = ToolResultAnchoredSummarizationCompactionStrategy(
        max_input_tokens=4000,
        tokenizer=tokenizer,
        keep_head_groups=1,
        keep_tail_groups=1,
        fallback=fallback,
    )
    strategy = (
        ToolResultAndUserTurnAnchoredSummarizationCompactionStrategy(
            tokenizer=tokenizer,
            tool_results=records,
            user_turns=UserTurnAnchoredSummarizationCompactionStrategy(
                max_input_tokens=4000, tokenizer=tokenizer, client=None
            ),
        )
        if composed
        else records
    )
    payload = "SKU-123 " * 4000
    result = Content.from_function_result(call_id="lookup", result=payload)
    messages = [
        Message("user", ["Read the latest tool result verbatim."]),
        Message(
            "assistant",
            [Content.from_function_call(call_id="lookup", name="lookup", arguments={})],
        ),
        Message("tool", [result]),
    ]
    assert not await fallback(deepcopy(messages))
    middleware, gate = recall(supports_tool_choice=False)
    options = {"tools": [make_recall_tool(gate)]}
    context = ChatContext(None, messages, options, kwargs={"compaction_strategy": strategy})
    completed = []

    async def send():
        await context.kwargs["compaction_strategy"](context.messages)
        completed.append(True)
        context.result = ChatResponse(messages=Message("assistant", [record_call()]))

    if composed:
        with pytest.raises(CompactionBudgetExceeded):
            await middleware.process(context, send)
        assert not completed
        assert middleware.failed_forced_calls == 1
    else:
        await middleware.process(context, send)
        assert completed == [True]
        assert middleware.failed_forced_calls == 0
    assert records.fallbacks_used >= 1
    assert result.result == payload
    assert all(not message.additional_properties.get(EXCLUDED_KEY, False) for message in messages)
    assert list(context.messages) == messages
    assert context.options is options
