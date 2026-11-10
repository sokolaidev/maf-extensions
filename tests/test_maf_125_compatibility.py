"""Installed MAF contracts used by sandbox tools and persisted benchmark histories."""

import json
from unittest.mock import AsyncMock

import pytest
from agent_framework import (
    AgentSession,
    Content,
    FunctionInvocationContext,
    FunctionMiddleware,
    FunctionTool,
    MCPStreamableHTTPTool,
    Message,
    MiddlewareFailure,
    MiddlewareTermination,
)
from agent_framework._middleware import FunctionMiddlewarePipeline
from agent_framework.exceptions import ToolExecutionException
from agent_framework.security import (
    ConfidentialityLabel,
    ContentLabel,
    IntegrityLabel,
    PolicyEnforcementFunctionMiddleware,
)
from maf_cachebench import serialize_message
from maf_sandbox import SandboxSpec
from maf_sandbox.maf import (
    argument_provenance_middleware,
    positions_holding_hidden_content,
    sandbox_label_tracking_middleware,
    sandbox_tool_declarations,
)
from mcp import types
from pydantic import BaseModel, field_validator


async def invoke(context):
    return await context.function.invoke(arguments=context.arguments, context=context)


@pytest.mark.anyio
@pytest.mark.parametrize("provenance_first", [False, True])
@pytest.mark.parametrize("confidentiality", ["public", "private", "user_identity"])
async def test_terminal_materialization_preserves_exact_suite_provenance(
    provenance_first, confidentiality
):
    tracker = sandbox_label_tracking_middleware()
    session = AgentSession()
    hidden = tracker.get_variable_store(session).store(
        "same.txt",
        ContentLabel(
            IntegrityLabel.UNTRUSTED,
            ConfidentialityLabel(confidentiality),
            metadata={
                "agent_framework.security.principals": [{"tenant_id": "tenant", "user_id": "user"}]
            },
        ),
    )
    seen = []

    async def body(files: list[str]):
        seen.append((files, positions_holding_hidden_content(files, argument="files")))
        return "done"

    tool = FunctionTool(
        name="probe",
        func=body,
        additional_properties={
            **sandbox_tool_declarations(
                SandboxSpec(kind="probe", work_dir="/work"), source_integrity="untrusted"
            ),
            "accepts_untrusted": True,
            "max_allowed_confidentiality": confidentiality,
            "agent_framework.security.principals": [{"tenant_id": "tenant", "user_id": "user"}],
        },
    )
    provenance = argument_provenance_middleware()
    middleware = [provenance, tracker] if provenance_first else [tracker, provenance]
    context = FunctionInvocationContext(
        function=tool,
        session=session,
        arguments={"files": ["same.txt", f"[{hidden}]"]},
    )
    await FunctionMiddlewarePipeline(*middleware, PolicyEnforcementFunctionMiddleware()).execute(
        context, invoke
    )
    assert seen == [(["same.txt", "same.txt"], frozenset({1}))]
    assert context.metadata["argument_label"].confidentiality == ConfidentialityLabel(
        confidentiality
    )
    assert positions_holding_hidden_content(["same.txt"], argument="files") == frozenset()


@pytest.mark.anyio
@pytest.mark.parametrize(
    "policy",
    [
        {"accepts_untrusted": False},
        {"accepts_untrusted": "false"},
        {"Source_Integrity": "trusted"},
        {"source_integrity": "TRUSTED"},
        {"max_allowed_confidentiality": "invalid"},
    ],
)
async def test_refused_policy_cannot_expose_hidden_content_to_validators(policy):
    tracker = sandbox_label_tracking_middleware()
    hidden = tracker.get_variable_store().store(
        "private bytes", ContentLabel(IntegrityLabel.UNTRUSTED, ConfidentialityLabel.PRIVATE)
    )
    observed = []

    class Arguments(BaseModel):
        value: str

        @field_validator("value")
        @classmethod
        def observe(cls, value):
            observed.append(value)
            return value

    async def body(value: str):
        pytest.fail("Refused arguments must not reach the body")

    tool = FunctionTool(
        name="refused",
        func=body,
        input_model=Arguments,
        additional_properties={
            **sandbox_tool_declarations(
                SandboxSpec(kind="probe", work_dir="/work"), source_integrity="untrusted"
            ),
            "max_allowed_confidentiality": "private",
            **policy,
        },
    )
    context = FunctionInvocationContext(function=tool, arguments={"value": f"[{hidden}]"})
    with pytest.raises((MiddlewareTermination, MiddlewareFailure)):
        await FunctionMiddlewarePipeline(
            argument_provenance_middleware(), tracker, PolicyEnforcementFunctionMiddleware()
        ).execute(context, invoke)
    assert observed == []


@pytest.mark.anyio
@pytest.mark.parametrize("mutation", ["arguments", "policy", "session", "runtime"])
async def test_approved_authority_cannot_change_before_sandbox_execution(mutation):
    tracker = sandbox_label_tracking_middleware()
    policy = PolicyEnforcementFunctionMiddleware(approval_on_violation=True)
    session = AgentSession(session_id="issuing")
    hidden = tracker.get_variable_store(session).store(
        "payload", ContentLabel(IntegrityLabel.UNTRUSTED)
    )

    async def body(value: str):
        pytest.fail("Mutated authority must not reach the body")

    tool = FunctionTool(name="sink", func=body, additional_properties={"accepts_untrusted": False})

    def context():
        result = FunctionInvocationContext(
            function=tool,
            arguments={"value": f"[{hidden}]"},
            session=session,
            kwargs={"tenant": "one"},
        )
        result.metadata["call_id"] = "approved-call"
        return result

    initial = context()
    with pytest.raises(MiddlewareTermination):
        await FunctionMiddlewarePipeline(tracker, policy).execute(initial, invoke)
    approval = initial.result.to_function_approval_response(True)

    class Mutate(FunctionMiddleware):
        async def process(self, context, call_next):
            if mutation == "arguments":
                assert isinstance(context.arguments, dict)
                context.arguments["value"] = "replacement"
            elif mutation == "policy":
                assert context.function.additional_properties is not None
                context.function.additional_properties["accepts_untrusted"] = True
            elif mutation == "session":
                context.session = AgentSession(session_id="other")
            else:
                context.kwargs["tenant"] = "two"
            await call_next()

    replay = context()
    replay.metadata["approval_response"] = approval
    with pytest.raises(MiddlewareFailure):
        await FunctionMiddlewarePipeline(
            argument_provenance_middleware(), tracker, policy, Mutate()
        ).execute(replay, invoke)


@pytest.mark.anyio
async def test_mcp_catalogue_reload_is_atomic_and_rechecks_approval():
    def approval(item):
        return "never_require" if item.annotations.read_only_hint else "always_require"

    tool = MCPStreamableHTTPTool(
        name="workload",
        url="https://example.com/mcp",
        request_timeout=60,
        max_discovery_pages=2,
        max_discovery_items=2,
        approval_mode=approval,
    )
    session = AsyncMock()
    tool.session = session

    def listing(read_only, **kwargs):
        return types.ListToolsResult(
            tools=[
                types.Tool(
                    name="inspect",
                    input_schema={"type": "object", "properties": {}},
                    annotations=types.ToolAnnotations(read_only_hint=read_only),
                )
            ],
            **kwargs,
        )

    try:
        session.list_tools.return_value = listing(True)
        await tool.load_tools()
        retained = tool.functions[0]
        assert retained.approval_mode == "never_require"
        session.list_tools.return_value = listing(False)
        await tool.load_tools()
        assert tool.functions[0] is retained
        assert retained.approval_mode == "always_require"
        session.list_tools.return_value = listing(True, next_cursor="repeated")
        with pytest.raises(ToolExecutionException, match="cursor|page|discovery"):
            await tool.load_tools()
        assert tool.functions == [retained]
        assert retained.approval_mode == "always_require"
    finally:
        await tool.close()


@pytest.mark.anyio
async def test_tuple_tool_results_survive_cachebench_history_serialization():
    async def body():
        return (Content.from_text("first"), {"nested": (1, 2)}, Content.from_text("last"))

    tool = FunctionTool(name="tuple_result", func=body)
    result = await tool.invoke(arguments={})
    assert [item.text for item in result] == ["first", '{"nested": [1, 2]}', "last"]
    message = Message(
        role="tool", contents=[Content.from_function_result(call_id="tuple", result=result)]
    )
    restored = Message.from_dict(json.loads(json.dumps(message.to_dict())))
    assert serialize_message(restored) == serialize_message(message)


@pytest.mark.anyio
@pytest.mark.parametrize("serialization_format", ["json", "msgpack"])
async def test_suite_hidden_surrogates_survive_session_reload(tmp_path, serialization_format):
    from agent_framework import FileSessionStore

    tracker = sandbox_label_tracking_middleware()
    session = AgentSession(session_id="surrogate")
    variable = tracker.get_variable_store(session).store(
        "\ud800", ContentLabel(IntegrityLabel.UNTRUSTED, ConfidentialityLabel.PRIVATE)
    )
    store = FileSessionStore(tmp_path, serialization_format=serialization_format)
    await store.set(session.session_id, session)
    restored = await store.get(session.session_id)
    assert restored is not None
    value, label = (
        sandbox_label_tracking_middleware().get_variable_store(restored).retrieve(variable)
    )
    assert value == "\ud800"
    assert label.integrity == IntegrityLabel.UNTRUSTED
    assert label.confidentiality == ConfidentialityLabel.PRIVATE


@pytest.mark.anyio
@pytest.mark.parametrize("wrapped", [False, True])
@pytest.mark.parametrize("stream", [False, True])
async def test_cachebench_client_preserves_agent_evaluation_metadata(wrapped, stream):
    from agent_framework import (
        Agent,
        BaseChatClient,
        ChatResponse,
        ChatResponseUpdate,
        ResponseStream,
    )
    from maf_cachebench._providers import ConfiguredClient

    class Client(BaseChatClient):
        def _inner_get_response(self, *, messages, stream, options, **kwargs):
            async def updates():
                yield ChatResponseUpdate(role="assistant", contents=[Content.from_text("done")])

            async def response():
                return ChatResponse(messages=[Message("assistant", "done")])

            if stream:
                return ResponseStream(updates(), finalizer=ChatResponse.from_updates)
            return response()

    async def inspect(value: str):
        return value

    client = Client()
    agent = Agent(
        client=ConfiguredClient(client, {"temperature": 0}) if wrapped else client,
        tools=[FunctionTool(name="inspect", func=inspect)],
    )
    if stream:
        response_stream = agent.run("test", stream=True)
        updates = [update async for update in response_stream]
        response = await response_stream.get_final_response()
        assert updates[-1].text == ""
        assert updates[-1].additional_properties is not None
        assert (
            updates[-1].additional_properties["agent_framework.evaluation"]
            == response.additional_properties["agent_framework.evaluation"]
        )
    else:
        response = await agent.run("test")
    assert response.text == "done"
    definition = response.additional_properties["agent_framework.evaluation"]["inspect"]
    assert definition["input_schema"]["properties"]["value"]["type"] == "string"


@pytest.mark.anyio
async def test_integral_json_number_reaches_integer_tool_without_accepting_bool():
    received = []

    async def body(value):
        received.append(value)
        return str(value)

    tool = FunctionTool(
        name="integer",
        func=body,
        input_model={
            "type": "object",
            "properties": {"value": {"type": "integer"}},
            "required": ["value"],
        },
    )
    await tool.invoke(arguments={"value": 7.0})
    assert received == [7] and type(received[0]) is int
    with pytest.raises(TypeError, match="expected integer, got bool"):
        await tool.invoke(arguments={"value": True})
    assert received == [7]


@pytest.mark.anyio
async def test_termination_result_remains_a_response_without_model_execution():
    from agent_framework import Agent, AgentMiddleware, AgentResponse
    from maf_cachebench._providers import ConfiguredClient

    class Client:
        async def get_response(self, **kwargs):
            pytest.fail("Terminated runs cannot reach the provider")

    class Refuse(AgentMiddleware):
        async def process(self, context, call_next):
            raise MiddlewareTermination(
                result=AgentResponse(messages=[Message("assistant", "refused")])
            )

    response = await Agent(client=ConfiguredClient(Client(), {}), middleware=[Refuse()]).run("test")
    assert response.text == "refused"
    assert serialize_message(response.messages[0]) == serialize_message(
        Message("assistant", "refused")
    )
