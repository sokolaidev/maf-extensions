"""Prompt recall fallback, request isolation and session backoff."""

import asyncio
import json
import logging

import pytest
from agent_framework import (
    AgentSession,
    CharacterEstimatorTokenizer,
    ChatContext,
    ChatResponse,
    ChatResponseUpdate,
    Content,
    Message,
    MiddlewareFailure,
    MiddlewareTermination,
    ResponseStream,
)
from agent_framework.exceptions import ChatClientInvalidRequestException

from maf_compaction import RecallGate, ToolResultRecallMiddleware, make_recall_tool
from maf_compaction._toolsummary import RECALL_TOOL_NAME, RECORD_MARKER

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
        assert context.messages == [original]
        assert context.kwargs == {}
        assert context.result is None
        assert context.options["tool_choice"] == "auto"
        assert context.options["instructions"].startswith(options["instructions"] + "\n\n")
        assert RECALL_TOOL_NAME in context.options["instructions"]
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
        assert context.options["instructions"].count(RECALL_TOOL_NAME) == 1
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
        assert not gate.take()
        raise RuntimeError("ordinary failed")

    with pytest.raises(RuntimeError, match="ordinary failed"):
        await middleware.process(context, send)
    assert calls == 3
    assert middleware.failed_forced_calls == 1
    assert middleware.pin_disabled_reason == "failed"
    assert middleware.recall_disabled
    assert context.options is options
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
