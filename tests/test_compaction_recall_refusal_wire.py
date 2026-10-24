"""Provider refusal fields must invalidate otherwise complete recall calls."""

import json

import pytest
from agent_framework import (
    CharacterEstimatorTokenizer,
    ChatContext,
    ChatResponse,
    Message,
    ResponseStream,
)
from agent_framework.openai import OpenAIChatCompletionClient
from maf_compaction import RecallGate, ToolResultRecallMiddleware, make_recall_tool
from maf_compaction._toolsummary import RECALL_TOOL_NAME
from openai.types.chat import ChatCompletion, ChatCompletionChunk

pytestmark = pytest.mark.anyio


@pytest.mark.parametrize("stream", [False, True])
async def test_chat_completions_refusal_with_tool_call_cannot_become_a_record(stream):
    client = OpenAIChatCompletionClient(model="test-model", api_key="unused")
    gate = RecallGate()
    middleware = ToolResultRecallMiddleware(
        max_input_tokens=1000,
        tokenizer=CharacterEstimatorTokenizer(),
        arm=gate.arm,
        disarm=gate.disarm,
        supports_tool_choice=False,
    )
    middleware._force_next = True
    context = ChatContext(None, [], {"tools": [make_recall_tool(gate)]}, stream=stream)
    calls = 0

    async def send():
        nonlocal calls
        calls += 1
        if calls == 2:
            context.result = ChatResponse(messages=Message("assistant", ["answer"]))
            return
        tool_call = {
            "id": "record",
            "type": "function",
            "function": {"name": RECALL_TOOL_NAME, "arguments": json.dumps({"values": "SKU-123"})},
        }
        if stream:

            async def updates():
                for delta in [
                    {"role": "assistant", "tool_calls": [{"index": 0, **tool_call}]},
                    {"refusal": "Provider declined the recall request."},
                ]:
                    chunk = ChatCompletionChunk.model_validate(
                        {
                            "id": "response",
                            "created": 0,
                            "model": "test-model",
                            "object": "chat.completion.chunk",
                            "choices": [{"index": 0, "delta": delta}],
                        }
                    )
                    yield client._parse_response_update_from_openai(chunk)

            context.result = ResponseStream(updates(), finalizer=ChatResponse.from_updates)
        else:
            response = ChatCompletion.model_validate(
                {
                    "id": "response",
                    "created": 0,
                    "model": "test-model",
                    "object": "chat.completion",
                    "choices": [
                        {
                            "index": 0,
                            "finish_reason": "tool_calls",
                            "message": {
                                "role": "assistant",
                                "refusal": "Provider declined the recall request.",
                                "tool_calls": [tool_call],
                            },
                        }
                    ],
                }
            )
            context.result = client._parse_response_from_openai(response, {})

    await middleware.process(context, send)
    assert calls == 2
    assert isinstance(context.result, ChatResponse)
    assert context.result.text == "answer"
    assert middleware.failed_forced_calls == 1
    assert middleware.value_free_record_streak == 1
    assert not gate.take()
