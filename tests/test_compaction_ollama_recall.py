"""Ollama option validation and recall execution with an offline transport."""

import json
from unittest.mock import AsyncMock

import pytest
from agent_framework import (
    CharacterEstimatorTokenizer,
    Content,
    FunctionTool,
    Message,
    ResponseStream,
)
from agent_framework.ollama import OllamaChatClient
from maf_compaction import RecallGate, ToolResultRecallMiddleware, make_recall_tool
from maf_compaction._toolsummary import _RECALL_INSTRUCTION, RECALL_TOOL_NAME, RECORD_MARKER
from ollama import AsyncClient, ChatResponse

pytestmark = pytest.mark.anyio


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("declared", [False, True])
@pytest.mark.parametrize("last_role", ["user", "tool"])
async def test_ollama_executes_prompt_recall_without_persisting_the_instruction(
    monkeypatch, stream, declared, last_role
):
    gate = RecallGate()
    middleware = ToolResultRecallMiddleware(
        max_input_tokens=100_000,
        tokenizer=CharacterEstimatorTokenizer(),
        arm=gate.arm,
        disarm=gate.disarm,
        supports_tool_choice=not declared,
    )
    middleware._force_next = True
    requests = []

    async def chat(**kwargs):
        requests.append(kwargs)
        if len(requests) == 1:
            assert kwargs["messages"][0]["role"] == "system"
            assert kwargs["messages"][0]["content"] == "Keep access restrictions."
            assert kwargs["messages"][-1]["role"] == "user"
            assert kwargs["messages"][-1]["content"] == _RECALL_INSTRUCTION
            assert kwargs["messages"][-2]["role"] == last_role
            assert [tool["function"]["name"] for tool in kwargs["tools"]] == [RECALL_TOOL_NAME]
            assert "tool_choice" not in kwargs
            assert kwargs["options"]["num_predict"] == 4000
            message = {
                "role": "assistant",
                "tool_calls": [
                    {"function": {"name": RECALL_TOOL_NAME, "arguments": {"values": "SKU-123"}}}
                ],
            }
        else:
            assert len(requests) == 2
            assert all(
                _RECALL_INSTRUCTION not in (message.get("content") or "")
                for message in kwargs["messages"]
            )
            assert {tool["function"]["name"] for tool in kwargs["tools"]} == {
                RECALL_TOOL_NAME,
                "lookup",
            }
            assert any(
                RECORD_MARKER in (message.get("content") or "") for message in kwargs["messages"]
            )
            assert all(
                RECALL_TOOL_NAME not in (message.get("content") or "")
                for message in kwargs["messages"]
                if message["role"] == "system"
            )
            message = {"role": "assistant", "content": "answer"}
        response = ChatResponse.model_validate(
            {"model": "test-model", "message": message, "done": True}
        )
        if stream:

            async def updates():
                yield response

            return updates()
        return response

    transport = AsyncClient(host="http://localhost:11434")
    monkeypatch.setattr(transport, "chat", AsyncMock(side_effect=chat))
    client = OllamaChatClient(client=transport, model="test-model", middleware=[middleware])
    try:
        messages = [Message("user", ["Continue from SKU-123."])]
        if last_role == "tool":
            messages.extend(
                [
                    Message(
                        "assistant",
                        [Content.from_function_call(call_id="lookup", name="lookup", arguments={})],
                    ),
                    Message(
                        "tool", [Content.from_function_result(call_id="lookup", result="SKU-123")]
                    ),
                ]
            )
        result = client.get_response(
            messages,
            options={
                "tools": [
                    make_recall_tool(gate),
                    FunctionTool(name="lookup", func=lambda: "SKU-123"),
                ],
                "instructions": "Keep access restrictions.",
            },
            stream=stream,
        )
        response = (
            await result.get_final_response()
            if isinstance(result, ResponseStream)
            else await result
        )
        serialized = json.dumps([message.to_dict() for message in response.messages])
        assert RECORD_MARKER in serialized
        assert "Before continuing, call" not in serialized
        assert _RECALL_INSTRUCTION not in json.dumps([message.to_dict() for message in messages])
        assert response.text == "answer"
        assert middleware.pin_disabled_reason == "unsupported"
        assert not middleware.recall_disabled
        assert middleware.records_forced == 1
        assert middleware.failed_forced_calls == 0
        assert len(requests) == 2
    finally:
        await transport._client.aclose()
