"""Provider parsing and replay through the installed 1.25 adapter cohort."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from agent_framework import Message
from agent_framework_foundry import FoundryChatClient
from agent_framework_ollama import OllamaChatClient
from agent_framework_openai import OpenAIChatClient
from maf_cachebench import serialize_message
from openai.types.responses.response_function_shell_tool_call import ResponseFunctionShellToolCall
from openai.types.responses.response_output_message import ResponseOutputMessage
from openai.types.responses.response_output_text import ResponseOutputText
from openai.types.responses.response_reasoning_item import ResponseReasoningItem


def test_foundry_reasoning_keeps_its_owning_message_after_reload():
    client = FoundryChatClient(project_client=MagicMock(), model="test")
    messages = [
        ResponseOutputMessage.model_construct(
            type="message",
            id=f"message-{index}",
            role="assistant",
            status="completed",
            content=[
                ResponseOutputText.model_construct(type="output_text", text=text, annotations=[])
            ],
        )
        for index, text in enumerate(["preamble", "answer"])
    ]
    reasoning = ResponseReasoningItem.model_construct(
        type="reasoning", id="reasoning", response_id="response", summary=[], content=[]
    )
    response = client._parse_response_from_openai(
        SimpleNamespace(
            id="response",
            model="test",
            created_at=0,
            metadata={},
            output_parsed=None,
            output=[*messages, reasoning],
            usage=None,
            finish_reason=None,
            conversation=None,
            status="completed",
        ),
        options={},
    )
    restored = [
        Message.from_dict(json.loads(json.dumps(message.to_dict())))
        for message in response.messages
    ]
    assert [message.text for message in restored] == ["preamble", "answer"]
    owners = [
        (message.message_id, content)
        for message in restored
        for content in message.contents
        if content.type == "text_reasoning"
    ]
    assert len(owners) == 1
    assert owners[0][0] == "message-1"
    assert (
        owners[0][1].additional_properties["__foundry_reasoning_replay_item__"]["id"] == "reasoning"
    )
    assert [serialize_message(message) for message in restored] == [
        serialize_message(message) for message in response.messages
    ]


@pytest.mark.parametrize("service_history", [False, True])
def test_empty_shell_result_survives_history_and_service_continuation(service_history):
    client = OpenAIChatClient(model="test", api_key="test")
    item = ResponseFunctionShellToolCall.model_validate(
        {
            "type": "shell_call",
            "id": "shell",
            "call_id": "empty",
            "status": "completed",
            "action": {"commands": [], "max_output_length": 512},
            "environment": {"type": "local"},
        }
    )
    contents = client._shell_item_to_contents(item, "run_shell")
    assert all(content.type != "function_call" for content in contents)
    restored = Message.from_json(Message("assistant", contents).to_json())
    if not service_history:
        from agent_framework.exceptions import ChatClientInvalidRequestException

        with pytest.raises(ChatClientInvalidRequestException, match="Stateless replay"):
            client._prepare_messages_for_openai([restored], request_uses_service_side_storage=False)
        restored.contents[0] = contents[0]
    wire = client._prepare_messages_for_openai(
        [restored, Message("user", "continue")], request_uses_service_side_storage=service_history
    )
    assert [entry for entry in wire if entry.get("type") == "shell_call_output"] == [
        {
            "type": "shell_call_output",
            "call_id": "empty",
            "output": [],
            "max_output_length": 512,
        }
    ]
    assert len([entry for entry in wire if entry.get("type") == "shell_call"]) == int(
        not service_history
    )


@pytest.mark.anyio
@pytest.mark.parametrize("terminal,expected", [("stop", "tool_calls"), ("length", "length")])
async def test_ollama_tool_call_survives_a_terminal_chunk_without_calls(terminal, expected):
    from ollama import AsyncClient, ChatResponse

    async def chunks():
        yield ChatResponse(
            model="test",
            done=False,
            message={
                "role": "assistant",
                "content": "",
                "tool_calls": [{"function": {"name": "probe", "arguments": {"value": 1}}}],
            },
        )
        yield ChatResponse(
            model="test",
            done=True,
            done_reason=terminal,
            message={"role": "assistant", "content": ""},
        )

    sdk = AsyncClient(host="http://127.0.0.1:11434")
    sdk.chat = AsyncMock(return_value=chunks())
    try:
        client = OllamaChatClient(
            client=sdk, model="test", function_invocation_configuration={"enabled": False}
        )
        stream = client.get_response([Message("user", "test")], stream=True)
        async for _ in stream:
            pass
        result = await stream.get_final_response()
        assert result.finish_reason == expected
        calls = [
            content
            for message in result.messages
            for content in message.contents
            if content.type == "function_call"
        ]
        assert len(calls) == 1 and calls[0].name == "probe"
        restored = [Message.from_json(message.to_json()) for message in result.messages]
        assert [serialize_message(message) for message in restored] == [
            serialize_message(message) for message in result.messages
        ]
    finally:
        await sdk._client.aclose()


@pytest.mark.anyio
@pytest.mark.parametrize("responses", [False, True])
@pytest.mark.parametrize("explicit", [None, "explicit-deployment"])
async def test_injected_azure_route_is_not_replaced_by_ambient_model(
    monkeypatch, responses, explicit
):
    from agent_framework_openai import OpenAIChatCompletionClient
    from openai import AsyncAzureOpenAI

    for name in [
        "AZURE_OPENAI_MODEL",
        "AZURE_OPENAI_CHAT_MODEL",
        "AZURE_OPENAI_CHAT_COMPLETION_MODEL",
    ]:
        monkeypatch.setenv(name, "ambient-deployment")
    async with AsyncAzureOpenAI(
        api_key="test",
        azure_endpoint="https://example.openai.azure.com",
        azure_deployment="injected-deployment",
        api_version="2025-01-01-preview",
    ) as sdk:
        client_type = OpenAIChatClient if responses else OpenAIChatCompletionClient
        client = client_type(async_client=sdk, **({"model": explicit} if explicit else {}))
        assert client.model == (explicit or "injected-deployment")
        assert client.client is sdk


def test_schema_mapping_order_does_not_change_provider_wire_or_reorder_enum_values():
    from agent_framework import FunctionTool

    schemas = [
        {
            "type": "object",
            "properties": {"choice": {"enum": ["z", "a"], "type": "string"}},
            "required": ["choice"],
        },
        {
            "required": ["choice"],
            "properties": {"choice": {"type": "string", "enum": ["z", "a"]}},
            "type": "object",
        },
    ]
    client = OpenAIChatClient(model="test", api_key="test")
    wires = [
        client._prepare_tools_for_openai([FunctionTool(name="choose", input_model=schema)])
        for schema in schemas
    ]
    assert json.dumps(wires[0]) == json.dumps(wires[1])
    assert wires[0][0]["parameters"]["properties"]["choice"]["enum"] == ["z", "a"]
