"""Ollama option validation and recall execution with an offline transport."""

import json
from unittest.mock import AsyncMock

import pytest
from agent_framework import CharacterEstimatorTokenizer, Message, ResponseStream
from agent_framework.ollama import OllamaChatClient
from maf_compaction import RecallGate, ToolResultRecallMiddleware, make_recall_tool
from maf_compaction._toolsummary import RECALL_TOOL_NAME, RECORD_MARKER
from ollama import AsyncClient, ChatResponse

pytestmark = pytest.mark.anyio


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("declared", [False, True])
async def test_ollama_executes_prompt_recall_without_persisting_the_instruction(
    monkeypatch, stream, declared
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
            assert RECALL_TOOL_NAME in kwargs["messages"][0]["content"]
            assert kwargs["options"]["num_predict"] == 4000
            message = {
                "role": "assistant",
                "tool_calls": [
                    {"function": {"name": RECALL_TOOL_NAME, "arguments": {"values": "SKU-123"}}}
                ],
            }
        else:
            assert len(requests) == 2
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
        result = client.get_response(
            [Message("user", ["Continue from SKU-123."])],
            options={
                "tools": [make_recall_tool(gate)],
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
        assert response.text == "answer"
        assert middleware.pin_disabled_reason == "unsupported"
        assert not middleware.recall_disabled
        assert middleware.records_forced == 1
        assert middleware.failed_forced_calls == 0
        assert len(requests) == 2
    finally:
        await transport._client.aclose()
