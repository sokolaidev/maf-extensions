"""Framework approval authority must precede a sandbox tool body."""

import asyncio
from typing import Any

import pytest
from agent_framework import (
    AgentSession,
    BaseChatClient,
    ChatResponse,
    Content,
    FunctionInvocationLayer,
    Message,
)
from maf_sandbox import CallerContext, Isolation, SandboxRouter, SandboxSpec
from maf_sandbox.maf import SandboxResult, sandboxed_tool
from maf_sandbox.testing import InMemoryStore, InProcessSandboxBackend


class ScriptedClient(FunctionInvocationLayer, BaseChatClient):
    def __init__(self, responses):
        super().__init__()
        self.responses = iter(responses)

    async def _inner_get_response(self, *, messages, stream, options, **kwargs) -> Any:
        assert not stream
        return next(self.responses)


def _guarded_tool(executed):
    def build(session):
        async def guarded(value: str) -> SandboxResult:
            """Record an approved operation."""
            executed.append(value)
            return SandboxResult(completed=True)

        return guarded

    return sandboxed_tool(
        build,
        router=SandboxRouter([InProcessSandboxBackend()], min_isolation=Isolation.NONE),
        context=CallerContext(
            current_scope=lambda: "scope",
            current_thread_id=lambda: "thread",
            list_files=InMemoryStore.list,
        ),
        agent_id="agent",
        spec=SandboxSpec(kind="approval-probe", work_dir="/work"),
        name="guarded",
        source_integrity="untrusted",
        result_contract=True,
        approval_mode="always_require",
    )[0]


@pytest.mark.parametrize("resume_scope", ["same", "different", "missing"])
@pytest.mark.parametrize("approved", [True, False])
def test_sandbox_body_requires_approval_bound_to_the_issuing_session(resume_scope, approved):
    async def exercise():
        executed = []
        guarded = _guarded_tool(executed)
        client = ScriptedClient(
            [
                ChatResponse(
                    messages=Message(
                        role="assistant",
                        contents=[
                            Content.from_function_call(
                                call_id="call-1", name="guarded", arguments={"value": "reviewed"}
                            )
                        ],
                    )
                ),
                ChatResponse(messages=Message(role="assistant", contents=["done"])),
                ChatResponse(messages=Message(role="assistant", contents=["still done"])),
            ]
        )
        session = AgentSession(session_id="issuing-session")
        first = await client.get_response(
            [Message(role="user", contents=["perform the operation"])],
            options={"tools": [guarded]},
            client_kwargs={"session": session},
        )
        request = next(
            item
            for message in first.messages
            for item in message.contents
            if item.type == "function_approval_request"
        )
        assert executed == []
        resumed_session = {
            "same": session,
            "different": AgentSession(session_id="another-session"),
            "missing": None,
        }[resume_scope]
        reply = Message(
            role="user", contents=[request.to_function_approval_response(approved=approved)]
        )
        history = [*first.messages, reply]
        kwargs = {"session": resumed_session} if resumed_session is not None else {}
        resumed = await client.get_response(
            history, options={"tools": [guarded]}, client_kwargs=kwargs
        )
        expected = ["reviewed"] if approved and resume_scope == "same" else []
        assert executed == expected
        if resume_scope == "same":
            await client.get_response(
                [*history, *resumed.messages, Message(role="user", contents=["continue"])],
                options={"tools": [guarded]},
                client_kwargs={"session": session},
            )
            assert executed == expected

    asyncio.run(exercise())
