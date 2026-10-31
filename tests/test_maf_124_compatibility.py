"""Installed framework adapters preserve suite persistence and retry boundaries."""

import json
from types import SimpleNamespace
from typing import cast

import pytest
from agent_framework import ChatResponse, Content, FileHistoryProvider, Message
from agent_framework.exceptions import ChatClientException
from agent_framework_openai import OpenAIChatClient
from maf_cachebench import serialize_message
from maf_cachebench._runner import is_rate_limited, unsupported_option
from maf_compaction._tool_result import plan_result_items
from openai.types.responses import ResponseErrorEvent


@pytest.mark.anyio
async def test_hosted_search_groups_are_preserved_or_shed_whole_after_reload():
    from agent_framework import CharacterEstimatorTokenizer
    from agent_framework._compaction import group_messages, project_included_messages
    from maf_compaction import AnchoredCompactionStrategy
    from maf_compaction._preserve import set_preserved

    messages = [Message("system", "instructions"), Message("user", "research")]
    for index in range(5):
        messages.append(
            Message(
                "assistant",
                [
                    Content.from_search_tool_call(f"search-{index}", tool_name="web_search"),
                    Content.from_search_tool_result(
                        f"search-{index}", tool_name="web_search", result="evidence " * 1000
                    ),
                ],
                message_id=f"search-message-{index}",
            )
        )
    messages.append(Message("assistant", "answer", message_id="answer"))
    messages = [
        Message.from_dict(json.loads(json.dumps(message.to_dict()))) for message in messages
    ]
    assert [group["kind"] for group in group_messages(messages)] == [
        "system",
        "user",
        *(["tool_call"] * 5),
        "assistant_text",
    ]
    protected = messages[4]
    before = [content.to_dict() for content in protected.contents]
    set_preserved(protected, preserved=True, reason="retained evidence")
    strategy = AnchoredCompactionStrategy(
        max_input_tokens=120,
        tokenizer=CharacterEstimatorTokenizer(),
        keep_head_groups=2,
        keep_tail_groups=1,
    )
    assert await strategy(messages)
    included = project_included_messages(messages)
    assert protected in included
    assert [content.to_dict() for content in protected.contents] == before
    search_contents = [
        content
        for message in included
        for content in message.contents
        if content.type in {"search_tool_call", "search_tool_result"}
    ]
    assert [(content.type, content.call_id) for content in search_contents] == [
        ("search_tool_call", "search-2"),
        ("search_tool_result", "search-2"),
    ]


@pytest.mark.anyio
@pytest.mark.parametrize("coordinate_space", ["source", "content"])
async def test_cited_tool_results_survive_reload_and_refuse_lossy_shortening(coordinate_space):
    citation = {
        "type": "citation",
        "url": "https://example.com/source",
        "title": "source",
        "annotated_regions_coordinate_space": coordinate_space,
        "annotated_regions": [{"type": "text_span", "start_index": 2, "end_index": 6}],
    }
    result = Content.from_function_result(
        call_id="call",
        result="cited text",
    )
    result.items = [Content.from_text("cited text", annotations=[citation])]
    message = Message("tool", [result])
    restored = Message.from_dict(json.loads(json.dumps(message.to_dict())))
    assert serialize_message(restored) == serialize_message(message)
    assert plan_result_items(restored.contents[0], "shorter", None) is None
    assert restored.contents[0].items is not None
    assert restored.contents[0].items[0].annotations == [citation]


@pytest.mark.anyio
async def test_msgpack_refuses_an_entire_oversized_batch_without_poisoning_history(
    tmp_path, monkeypatch
):
    history = FileHistoryProvider(storage_path=tmp_path, serialization_format="msgpack")
    original = Message("user", "retained", message_id="original")
    await history.save_messages("session", [original])
    before = next(tmp_path.glob("*.msgpack")).read_bytes()
    monkeypatch.setattr(FileHistoryProvider, "_MAX_MSGPACK_RECORD_BYTES", 2048)
    with pytest.raises(ValueError, match="record limit"):
        await history.save_messages(
            "session",
            [
                Message("assistant", "would fit", message_id="small"),
                Message("assistant", "x" * 4096, message_id="large"),
            ],
        )
    assert next(tmp_path.glob("*.msgpack")).read_bytes() == before
    restored = await history.get_messages("session")
    assert [serialize_message(m) for m in restored] == [serialize_message(original)]


@pytest.mark.anyio
@pytest.mark.parametrize(
    "code,param,option,rate",
    [
        ("unsupported_parameter", "temperature", "temperature", False),
        ("rate_limit_exceeded", None, None, True),
        ("server_error", "temperature", None, False),
        ("unsupported_parameter", "input[0]", None, False),
    ],
)
async def test_real_sse_errors_keep_retry_classification(code, param, option, rate):
    async with OpenAIChatClient(model="test", api_key="unused") as client:
        event = ResponseErrorEvent(
            type="error", sequence_number=1, code=code, param=param, message="Request refused."
        )
        with pytest.raises(ChatClientException) as captured:
            client._parse_chunk_from_openai(event, options={}, function_call_ids={})
        error = captured.value
        assert getattr(error.__cause__, "code") == code
        assert getattr(error.__cause__, "param") == param
        assert unsupported_option(error) == option
        assert is_rate_limited(error) is rate


@pytest.mark.anyio
async def test_completed_hosted_media_survives_aggregation_and_cachebench_reload():
    async with OpenAIChatClient(model="test", api_key="unused") as client:
        event = SimpleNamespace(
            type="response.output_item.done",
            output_index=0,
            item=SimpleNamespace(
                type="code_interpreter_call",
                id="ci_one",
                call_id="ci_one",
                status="completed",
                code="print(1)",
                outputs=[
                    SimpleNamespace(type="logs", logs="1"),
                    SimpleNamespace(type="image", url="https://example.com/plot.png"),
                ],
            ),
        )
        update = client._parse_chunk_from_openai(event, options={}, function_call_ids={})
        response = ChatResponse.from_updates([update])
        message = response.messages[0]
        restored = Message.from_dict(json.loads(json.dumps(message.to_dict())))
        result = next(c for c in restored.contents if c.type == "code_interpreter_tool_result")
        assert result.outputs is not None
        assert any(c.type == "text" and c.text == "1" for c in result.outputs)
        assert any(
            c.type == "uri" and c.uri == "https://example.com/plot.png" for c in result.outputs
        )
        assert serialize_message(restored) == serialize_message(message)


@pytest.mark.anyio
async def test_responses_message_ids_survive_stream_and_nonstream_replay():
    from openai.types.responses import Response, ResponseOutputMessage, ResponseOutputText

    items = [
        ResponseOutputMessage(
            type="message",
            id=f"msg_{number}",
            role="assistant",
            status="completed",
            content=[
                ResponseOutputText(type="output_text", text=text, annotations=[], logprobs=[])
            ],
        )
        for number, text in enumerate(["first", "second"])
    ]
    raw = Response.model_construct(
        id="resp_one",
        created_at=1,
        model="test",
        status="completed",
        metadata={},
        output=items,
        usage=None,
        conversation=None,
        incomplete_details=None,
        error=None,
    )
    async with OpenAIChatClient(model="test", api_key="unused") as client:
        response = client._parse_response_from_openai(raw, options={"store": False})
        updates = [
            client._parse_chunk_from_openai(
                SimpleNamespace(
                    type="response.output_text.delta",
                    item_id=item.id,
                    output_index=i,
                    content_index=0,
                    delta=cast(ResponseOutputText, item.content[0]).text,
                    logprobs=[],
                ),
                options={"store": False},
                function_call_ids={},
            )
            for i, item in enumerate(items)
        ]
        streamed = ChatResponse.from_updates(updates)
        for parsed in [response, streamed]:
            restored = [
                Message.from_dict(json.loads(json.dumps(m.to_dict()))) for m in parsed.messages
            ]
            assert [(m.message_id, m.text) for m in restored] == [
                ("msg_0", "first"),
                ("msg_1", "second"),
            ]
            assert [serialize_message(m) for m in restored] == [
                serialize_message(m) for m in parsed.messages
            ]
            wire = client._prepare_messages_for_openai(
                restored, request_uses_service_side_storage=False
            )
            assert [m["content"][0]["text"] for m in wire] == ["first", "second"]


@pytest.mark.anyio
async def test_mcp_optional_tasks_and_exact_approval_rules_keep_plain_calls():
    from unittest.mock import AsyncMock

    from agent_framework import MCPStreamableHTTPTool
    from agent_framework.observability import OtelAttr
    from mcp import types

    tool = MCPStreamableHTTPTool(
        name="docs",
        url="https://example.com:8443/mcp",
        request_timeout=60,
        approval_mode={
            "default": "always_require",
            "never_require_approval": ["optional"],
            "always_require_approval": ["overlap"],
        },
    )
    session = AsyncMock()
    session.list_tools.return_value = types.ListToolsResult(
        tools=[
            types.Tool(
                name=name,
                input_schema={"type": "object", "properties": {}},
                execution=types.ToolExecution(task_support=support),
            )
            for name, support in [
                ("required", "required"),
                ("optional", "optional"),
                ("overlap", "optional"),
                ("option", "forbidden"),
            ]
        ]
    )
    session.call_tool.return_value = types.CallToolResult(
        content=[types.TextContent(type="text", text="ok")]
    )
    tool.session = session
    try:
        await tool.load_tools()
        functions = {f.name: f for f in tool.functions}
        assert set(functions) == {"optional", "overlap", "option"}
        assert functions["optional"].approval_mode == "never_require"
        assert (
            functions["overlap"].approval_mode
            == functions["option"].approval_mode
            == "always_require"
        )
        await functions["optional"].invoke(arguments={})
        assert session.call_tool.await_args.args[0] == "optional"
        attributes = tool._mcp_base_span_attributes()
        assert attributes[OtelAttr.ADDRESS] == "example.com"
        assert attributes[OtelAttr.PORT] == 8443
        assert attributes[OtelAttr.NETWORK_PROTOCOL_NAME] == "http"
    finally:
        await tool.close()


@pytest.mark.anyio
async def test_suite_label_trackers_keep_quarantine_clients_config_scoped():
    import asyncio

    from agent_framework import AgentSession, FunctionInvocationContext
    from agent_framework._middleware import FunctionMiddlewarePipeline
    from agent_framework.security import SecureAgentConfig
    from maf_sandbox.maf import sandbox_label_tracking_middleware

    class Client:
        def __init__(self):
            self.seen = []

        async def get_response(self, *, messages, **kwargs):
            self.seen.append(" ".join(m.text for m in messages))
            await asyncio.sleep(0)
            return SimpleNamespace(text="summary")

    clients = [Client(), Client()]
    configs = [SecureAgentConfig(quarantine_chat_client=client) for client in [*clients, None]]
    for config in configs:
        config.label_tracker = sandbox_label_tracking_middleware()

    async def call(config, marker):
        function = next(t for t in config.get_tools() if t.name == "quarantined_llm")
        context = FunctionInvocationContext(
            function=function,
            session=AgentSession(),
            arguments={
                "prompt": "Summarize",
                "labelled_data": {
                    "record": {
                        "content": marker,
                        "security_label": {"integrity": "untrusted", "confidentiality": "private"},
                    }
                },
            },
        )

        async def invoke(inner):
            return await function.invoke(
                arguments=inner.arguments, context=inner, skip_parsing=True
            )

        await FunctionMiddlewarePipeline(*config.get_middleware()).execute(context, invoke)

    await asyncio.gather(call(configs[0], "ALPHA_ONLY"), call(configs[1], "BETA_ONLY"))
    await call(configs[2], "NO_CLIENT")
    assert "ALPHA_ONLY" in clients[0].seen[0] and "BETA_ONLY" not in clients[0].seen[0]
    assert "BETA_ONLY" in clients[1].seen[0] and "ALPHA_ONLY" not in clients[1].seen[0]
    assert all("NO_CLIENT" not in request for client in clients for request in client.seen)
