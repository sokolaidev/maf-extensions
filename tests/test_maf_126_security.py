"""Durable replay and service continuation authority through the installed framework."""

import json
from copy import deepcopy

import pytest
from agent_framework import (
    Agent,
    AgentSession,
    BaseChatClient,
    ChatMiddlewareLayer,
    ChatOptions,
    ChatResponse,
    ChatResponseUpdate,
    Content,
    ContextProvider,
    FunctionInvocationContext,
    FunctionInvocationLayer,
    FunctionTool,
    InMemoryHistoryProvider,
    Message,
    ResponseStream,
)
from agent_framework.exceptions import ToolExecutionException
from agent_framework.security import SecureAgentConfig
from maf_sandbox import SandboxSpec
from maf_sandbox.maf import (
    argument_provenance_middleware,
    sandbox_label_tracking_middleware,
    sandbox_tool_declarations,
)


class Client(FunctionInvocationLayer, ChatMiddlewareLayer, BaseChatClient):
    def __init__(self, responses, **kwargs):
        super().__init__(**kwargs)
        self.responses = iter(responses)
        self.seen = []

    def _inner_get_response(self, *, messages, stream, options, **kwargs):
        self.seen.append(list(messages))
        result = next(self.responses)

        async def response():
            return result

        async def updates():
            for message in result.messages:
                yield ChatResponseUpdate(role=message.role, contents=message.contents)

        return ResponseStream(updates(), finalizer=lambda _: result) if stream else response()


def call(name="protected", value="reviewed"):
    return ChatResponse(
        messages=[
            Message(
                "assistant",
                [Content.from_function_call(call_id="call", name=name, arguments={"value": value})],
            )
        ]
    )


class SuiteSecurity(SecureAgentConfig):
    def __init__(self, provenance_first=True, **kwargs):
        super().__init__(**kwargs)
        self.label_tracker = sandbox_label_tracking_middleware()
        self.provenance_first = provenance_first

    def get_middleware(self):
        stack = super().get_middleware()
        stack.insert(0 if self.provenance_first else 1, argument_provenance_middleware())
        return stack


@pytest.mark.anyio
@pytest.mark.parametrize("provenance_first", [False, True])
@pytest.mark.parametrize("forged", [False, True])
async def test_unstamped_tool_replay_remains_private_and_refused_after_reload(
    provenance_first, forged
):
    executed = []

    async def protected(value: str):
        executed.append(value)
        return "done"

    tool = FunctionTool(
        name="protected",
        func=protected,
        additional_properties={
            **sandbox_tool_declarations(
                SandboxSpec(kind="probe", work_dir="/work"), source_integrity="untrusted"
            ),
            "accepts_untrusted": False,
        },
    )
    replay = Content.from_function_result(call_id="external", result="private result")
    if forged:
        replay.additional_properties["security_label"] = {
            "integrity": "trusted",
            "confidentiality": "public",
        }
    session = AgentSession()
    session.state["in_memory"] = {"messages": [Message("tool", [replay])]}
    for _ in range(2):
        security = SuiteSecurity(provenance_first)
        client = Client([call(), ChatResponse(messages=[Message("assistant", "refused")])])
        agent = Agent(
            client=client, tools=[tool], context_providers=[InMemoryHistoryProvider(), security]
        )
        await agent.run("perform operation", session=session)
        assert executed == []
        audit = security.get_audit_log(session)
        assert any(entry["context_label"]["integrity"] == "untrusted" for entry in audit)
        assert any(entry["context_label"]["confidentiality"] == "private" for entry in audit)
        session = AgentSession.from_dict(json.loads(json.dumps(session.to_dict())))


@pytest.mark.anyio
@pytest.mark.parametrize("changed", [False, True])
async def test_service_continuation_requires_the_exact_durable_identity(changed):
    class ServiceClient(Client):
        STORES_BY_DEFAULT = True
        SERVICE_CONTINUATION_OPTION_NAMES = ("conversation_id",)

    session = AgentSession()
    security = SuiteSecurity()
    client = ServiceClient(
        [
            ChatResponse(messages=[Message("assistant", "first")], conversation_id="owned"),
            ChatResponse(messages=[Message("assistant", "second")], conversation_id="owned"),
        ]
    )
    agent = Agent(client=client, context_providers=[security])
    await agent.run("start", session=session)
    session = AgentSession.from_dict(json.loads(json.dumps(session.to_dict())))
    if changed:
        session.service_session_id = "other"
    options: ChatOptions = {"conversation_id": "other" if changed else "owned"}
    if changed:
        with pytest.raises(RuntimeError, match="authoritative FIDES"):
            await agent.run("resume", session=session, options=options)
        assert len(client.seen) == 1
    else:
        assert (await agent.run("resume", session=session, options=options)).text == "second"
        assert len(client.seen) == 2


@pytest.mark.anyio
@pytest.mark.parametrize("tamper", [False, True])
async def test_approval_uses_canonical_arguments_after_durable_reload(tamper):
    executed = []

    async def protected(value: str):
        executed.append(value)
        return "done"

    tool = FunctionTool(name="protected", func=protected, approval_mode="always_require")
    client = Client([call(), ChatResponse(messages=[Message("assistant", "done")])])
    agent = Agent(
        client=client, tools=[tool], context_providers=[InMemoryHistoryProvider(), SuiteSecurity()]
    )
    session = AgentSession()
    first = await agent.run("perform operation", session=session)
    request = first.user_input_requests[0]
    session = AgentSession.from_dict(json.loads(json.dumps(session.to_dict())))
    response = request.to_function_approval_response(approved=True)
    if tamper:
        response.function_call.arguments = {"value": "unreviewed"}
    await agent.run(Message("user", [response]), session=session)
    assert executed == ([] if tamper else ["reviewed"])


@pytest.mark.anyio
async def test_nested_agent_does_not_receive_or_replace_parent_service_state():
    parent = AgentSession(service_session_id="parent-service")
    parent.state = {"parent_provider": {"handle": "parent-handle"}, "application": "retained"}
    original = deepcopy(parent.state)
    observed = []

    class Capture(ContextProvider):
        async def before_run(self, *, agent, session, context, state):
            observed.append((deepcopy(session.state), session.service_session_id))
            session.state["parent_provider"] = {"handle": "child-handle"}
            session.state["application"] = "updated"

    child = Agent(
        client=Client([ChatResponse(messages=[Message("assistant", "child")])]),
        name="child",
        context_providers=[Capture("child_capture")],
    )
    tool = child.as_tool(propagate_session=True)
    context = FunctionInvocationContext(function=tool, arguments={"task": "probe"}, session=parent)
    with pytest.raises(ToolExecutionException, match="provider-owned"):
        await tool.invoke(arguments=context.arguments, context=context)
    assert parent.state == original
    assert observed == []
    context.parent_service_session_state_keys = frozenset({"parent_provider"})
    result = await tool.invoke(arguments=context.arguments, context=context)
    assert result[0].text == "child"
    assert parent.service_session_id == "parent-service"
    assert parent.state["parent_provider"] == original["parent_provider"]
    assert parent.state["application"] == "updated"
    assert "parent_provider" not in observed[0][0]
    assert observed[0][1] is None


@pytest.mark.parametrize("resolved", [False, True])
def test_approval_groups_keep_nonadjacent_call_reasoning_and_results_together(resolved):
    from agent_framework._compaction import group_messages

    function = Content.from_function_call(call_id="linked", name="probe", arguments={})
    approval = Content.from_function_approval_request("approval", function)
    messages = [
        Message("user", "request"),
        Message("assistant", [Content.from_text_reasoning(text="reasoning"), function]),
        Message("assistant", "intervening narration"),
        Message("assistant", [approval]),
    ]
    linked = [1, 3]
    if resolved:
        messages.extend(
            [
                Message("user", [approval.to_function_approval_response(approved=True)]),
                Message("assistant", "more narration"),
                Message("tool", [Content.from_function_result(call_id="linked", result="done")]),
            ]
        )
        linked.extend([4, 6])
    restored = [
        Message.from_dict(json.loads(json.dumps(message.to_dict()))) for message in messages
    ]
    groups = group_messages(restored)
    owners = {
        index: group
        for group in groups
        for index in range(group["start_index"], group["end_index"] + 1)
    }
    assert len({owners[index]["group_id"] for index in linked}) == 1
    assert all(owners[index]["has_reasoning"] for index in linked)


def test_repeated_approval_identity_links_only_the_latest_request_occurrence():
    from agent_framework._compaction import group_messages

    function = Content.from_function_call(call_id="reused", name="probe", arguments={})
    older = Content.from_function_approval_request("old", function)
    current = Content.from_function_approval_request("current", function)
    messages = [
        Message("assistant", [older]),
        Message("user", "supersede the previous request"),
        Message("assistant", [current]),
        Message("user", [current.to_function_approval_response(approved=False)]),
    ]
    owners = {
        index: group["group_id"]
        for group in group_messages(messages)
        for index in range(group["start_index"], group["end_index"] + 1)
    }
    assert owners[2] == owners[3]
    assert owners[0] != owners[3]
