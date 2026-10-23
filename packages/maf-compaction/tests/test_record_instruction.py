"""Pinned record instructions remain local to one request, including failed attempts."""

import asyncio
import json
import logging
from copy import deepcopy

import pytest
from agent_framework import (
    CharacterEstimatorTokenizer,
    ChatContext,
    ChatResponse,
    ChatResponseUpdate,
    Content,
    Message,
    ResponseStream,
)
from agent_framework.exceptions import ChatClientException

from maf_compaction import (
    DEFAULT_RECORD_INSTRUCTION,
    RECALL_TOOL_NAME,
    RecallGate,
    ToolResultRecallMiddleware,
    make_recall_tool,
)
from maf_compaction._request import RECALL_PROMPT_KEY
from maf_compaction._toolsummary import _RECALL_INSTRUCTION

pytestmark = pytest.mark.anyio


def recall(**kwargs):
    gate = RecallGate()
    middleware = ToolResultRecallMiddleware(
        max_input_tokens=10_000,
        tokenizer=CharacterEstimatorTokenizer(),
        arm=gate.arm,
        disarm=gate.disarm,
        **kwargs,
    )
    middleware._force_next = True
    return middleware, gate


def record(arguments=None):
    return Content.from_function_call(
        call_id="record",
        name=RECALL_TOOL_NAME,
        arguments={"values": "Namespace limit | 1000"} if arguments is None else arguments,
    )


@pytest.mark.parametrize("instruction", [None, DEFAULT_RECORD_INSTRUCTION, "Keep exact values."])
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("tail_role", ["user", "tool"])
async def test_instruction_is_last_on_the_pinned_request_only(instruction, stream, tail_role):
    middleware, gate = recall(record_instruction=instruction)
    messages = [Message("system", ["instructions"]), Message(tail_role, ["earlier content"])]
    options = {"tools": [make_recall_tool(gate)], "max_tokens": 9000}
    before = json.dumps([m.to_dict() for m in messages])
    context = ChatContext(None, messages, options, stream=stream)
    retained = []

    async def send():
        await context.kwargs["compaction_strategy"](context.messages)
        assert context.options == {
            **options,
            "tool_choice": {"mode": "required", "required_function_name": RECALL_TOOL_NAME},
            "max_tokens": 4000,
        }
        prefix = list(context.messages) if instruction is None else list(context.messages[:-1])
        assert json.dumps([m.to_dict() for m in prefix]) == before
        assert all(left is right for left, right in zip(prefix, messages, strict=True))
        if instruction is None:
            assert context.messages is messages
        else:
            assert context.messages[-1].role == "user"
            assert context.messages[-1].text == instruction
            assert context.messages[-1].additional_properties[RECALL_PROMPT_KEY]
        retained.append(context.messages)
        context.messages = list(context.messages)

        async def updates():
            assert context.messages[-1].text == (instruction or "earlier content")
            yield ChatResponseUpdate(role="assistant", contents=[record()])

        response = ChatResponse(messages=Message("assistant", [record()]))
        context.result = (
            ResponseStream(updates(), finalizer=lambda _: response) if stream else response
        )

    await middleware.process(context, send)
    if stream:
        assert isinstance(context.result, ResponseStream)
        await context.result.get_final_response()
    assert retained[0] == context.messages == messages
    assert json.dumps([m.to_dict() for m in messages]) == before
    assert "record_instruction" not in middleware.session_state()
    assert instruction is None or instruction not in json.dumps(middleware.session_state())
    assert context.options is options

    async def ordinary():
        assert context.messages == messages
        assert context.options is options
        assert not gate.take()
        context.result = ChatResponse(messages=Message("assistant", ["answer"]))

    await middleware.process(context, ordinary)


@pytest.mark.parametrize("declared_unsupported", [False, True])
async def test_prompted_recall_does_not_receive_the_pinned_instruction(declared_unsupported):
    middleware, gate = recall(
        record_instruction=DEFAULT_RECORD_INSTRUCTION,
        supports_tool_choice=not declared_unsupported,
    )
    question = Message("user", ["Only give the titles."])
    context = ChatContext(None, [question], {"tools": [make_recall_tool(gate)]})
    sent = []

    async def send():
        await context.kwargs["compaction_strategy"](context.messages)
        sent.append(context.messages)
        if not declared_unsupported and len(sent) == 1:
            assert context.messages[-1].text == DEFAULT_RECORD_INSTRUCTION
            raise ChatClientException("tool_choice is unsupported")
        assert context.messages[-1].text == _RECALL_INSTRUCTION
        assert len(context.messages) == 2
        context.result = ChatResponse(messages=Message("assistant", [record()]))

    await middleware.process(context, send)
    assert len(sent) == (1 if declared_unsupported else 2)
    assert all(messages == [question] for messages in sent)
    assert context.messages == [question]


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("failure", ["provider", "json", "arguments", "timeout", "cancel"])
async def test_failed_record_cleans_instruction_and_logs_cause(stream, failure, caplog):
    middleware, gate = recall(
        record_instruction=DEFAULT_RECORD_INSTRUCTION,
        forced_call_timeout_seconds=0.1 if failure == "timeout" else None,
    )
    messages = [Message("user", ["Only give the titles."])]
    options = {"tools": [make_recall_tool(gate)]}
    context = ChatContext(None, messages, options, stream=stream)
    sent = []
    error = RuntimeError("provider unavailable")
    error.__cause__ = ConnectionError("upstream closed")
    arguments = '{"values": "unfinished' if failure == "json" else {"values": 17}

    async def fail():
        if failure == "provider":
            raise error
        if failure == "timeout":
            await asyncio.Event().wait()
        if failure == "cancel":
            raise asyncio.CancelledError()

    async def send():
        await context.kwargs["compaction_strategy"](context.messages)
        sent.append(context.messages)
        if len(sent) > 1:
            assert context.messages == messages
            assert context.options is options
            assert not gate.take()
            context.result = ChatResponse(messages=Message("assistant", ["recovered"]))
            return
        assert context.messages[-1].text == DEFAULT_RECORD_INSTRUCTION

        async def updates():
            yield ChatResponseUpdate(role="assistant", contents=[record(arguments)])
            await fail()

        response = ChatResponse(messages=Message("assistant", [record(arguments)]))
        if stream:
            context.result = ResponseStream(updates(), finalizer=lambda _: response)
        else:
            await fail()
            context.result = response

    if failure == "cancel":
        with pytest.raises(asyncio.CancelledError):
            await middleware.process(context, send)
        assert middleware.session_state()["force_next"]
        assert not caplog.records
    else:
        await middleware.process(context, send)
        assert len(sent) == 2
        assert middleware.failed_forced_calls == 1
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        assert ": failed" in warnings[0].message
        assert warnings[0].exc_info is not None
        cause = warnings[0].exc_info[1]
        if failure == "provider":
            assert cause is error
            assert "upstream closed" in caplog.text
        elif failure == "json":
            assert isinstance(cause, json.JSONDecodeError)
        elif failure == "arguments":
            assert isinstance(cause, ValueError)
            assert "string values" in caplog.text
        else:
            assert isinstance(cause, TimeoutError)
    assert all(sent_messages == messages for sent_messages in sent)
    assert context.messages == messages
    assert context.options is options


@pytest.mark.parametrize("disabled", [False, True])
async def test_history_reload_removes_stale_instructions_and_appends_one_last(disabled):
    middleware, gate = recall(record_instruction=DEFAULT_RECORD_INSTRUCTION)
    if disabled:
        middleware._disable_pin("failed")
    stale = Message("user", ["stale instruction"], additional_properties={RECALL_PROMPT_KEY: True})
    question = Message("user", ["The current question."])
    result = Message("tool", ["new result"])
    context = ChatContext(None, [deepcopy(stale), question], {"tools": [make_recall_tool(gate)]})

    async def send():
        context.messages = [deepcopy(stale), *context.messages, result]
        await context.kwargs["compaction_strategy"](context.messages)
        assert context.messages[:2] == [question, result]
        prompts = [m for m in context.messages if m.additional_properties.get(RECALL_PROMPT_KEY)]
        assert len(prompts) == int(not disabled)
        if prompts:
            assert context.messages[-1] is prompts[0]
            assert prompts[0].text == DEFAULT_RECORD_INSTRUCTION
        context.result = ChatResponse(messages=Message("assistant", [record()]))

    await middleware.process(context, send)
    assert all(not m.additional_properties.get(RECALL_PROMPT_KEY) for m in context.messages)
