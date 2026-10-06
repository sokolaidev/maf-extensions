"""File tools, listings, sinks and provenance share a host-bound native store."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError

import pytest
from agent_framework import (
    AgentSession,
    FunctionInvocationContext,
    InMemoryAgentFileStore,
    SessionContext,
)

from maf_sandbox import Artifact, SourceIntegrity
from maf_sandbox.maf import ScopedFileStores, make_file_store_sink


async def tools_for(binding):
    context = SessionContext(session_id=binding.thread_id, input_messages=[])
    await binding.provider.before_run(
        agent=None,
        session=AgentSession(session_id=binding.thread_id),
        context=context,
        state={},
    )
    return {tool.name: tool for tool in context.tools}


async def invoke(binding, tools, name, **arguments):
    tool = tools[f"file_access_{name}"]
    context = FunctionInvocationContext(function=tool, arguments=arguments)

    async def call():
        context.result = await tool.invoke(arguments=context.arguments)

    await binding.middleware.process(context, call)
    return context.result


def test_two_sessions_share_relative_names_without_sharing_bytes_or_provenance():
    scopes = ScopedFileStores(
        lambda scope, thread: InMemoryAgentFileStore(), floor=SourceIntegrity.TRUSTED
    )
    a = scopes.bind(scope="tenant", thread_id="a")
    b = scopes.bind(scope="tenant", thread_id="b")

    async def scenario():
        ta, tb = await asyncio.gather(tools_for(a), tools_for(b))
        await asyncio.gather(
            invoke(a, ta, "write", file_name="notes.txt", content="AAA"),
            invoke(b, tb, "write", file_name="notes.txt", content="BBB"),
        )
        assert await a.store.read("notes.txt") == "AAA"
        assert await b.store.read("notes.txt") == "BBB"
        assert (await invoke(a, ta, "read", file_name="notes.txt"))[0].text == "AAA"
        await invoke(a, ta, "replace", file_name="notes.txt", old_string="AAA", new_string="AXA")
        await invoke(
            a,
            ta,
            "replace_lines",
            file_name="notes.txt",
            edits=[{"line_number": 1, "new_line": "updated"}],
        )
        assert await a.store.read("notes.txt") == "updated"
        assert await b.store.read("notes.txt") == "BBB"
        sink = make_file_store_sink(a.store, provenance=a.provenance)
        await sink.deliver(
            Artifact(
                name="report.txt", content=b"result", kind="probe", media_type=None, call_id="call"
            )
        )
        assert await b.store.read("call/report.txt") is None
        assert a.provenance.integrity_of("call/report.txt") is SourceIntegrity.UNTRUSTED
        assert b.provenance.integrity_of("call/report.txt") is SourceIntegrity.TRUSTED
        listing = await a.caller_context().list_files(a.store)
        assert {item.name for item in listing} == {"notes.txt", "call/report.txt"}
        assert all(item.integrity is SourceIntegrity.UNTRUSTED for item in listing)
        with pytest.raises(ValueError, match="bound scoped store"):
            await a.caller_context().list_files(b.store)
        await invoke(a, ta, "delete", file_name="notes.txt")
        assert await a.store.read("notes.txt") is None
        assert await b.store.read("notes.txt") == "BBB"
        assert a.provenance.integrity_of("notes.txt") is SourceIntegrity.UNTRUSTED

    asyncio.run(scenario())


@pytest.mark.parametrize("path", ["../escape", "/absolute", "a/../../escape", "C:/escape"])
def test_provider_refuses_escaping_paths(path):
    binding = ScopedFileStores(lambda scope, thread: InMemoryAgentFileStore()).bind(
        scope="tenant", thread_id="thread"
    )

    async def scenario():
        tools = await tools_for(binding)
        await invoke(binding, tools, "write", file_name=path, content="bad")
        assert await binding.store.list_children() == []

    asyncio.run(scenario())


def test_binding_is_stable_and_does_not_wrap_native_capabilities():
    store = InMemoryAgentFileStore()
    scopes = ScopedFileStores(lambda scope, thread: store)
    with ThreadPoolExecutor(max_workers=4) as pool:
        bindings = list(
            pool.map(lambda _: scopes.bind(scope="tenant", thread_id="thread"), range(8))
        )
    assert all(bound is bindings[0] for bound in bindings)
    bound = bindings[0]
    assert bound.store is store
    assert bound.provider.store is store
    assert bound.provider.session_scoped is False
    assert bound.caller_context().current_scope() == "tenant"
    assert bound.caller_context().current_thread_id() == "thread"
    with pytest.raises(FrozenInstanceError):
        setattr(bound, "scope", "different")
    with pytest.raises(ValueError, match="distinct confined store"):
        scopes.bind(scope="another-tenant", thread_id="thread")


def test_missing_scope_and_capacity_refuse_without_evicting_provenance():
    scopes = ScopedFileStores(lambda scope, thread: InMemoryAgentFileStore(), max_scopes=1)
    with pytest.raises(ValueError, match="scope"):
        scopes.bind(scope="", thread_id="thread")
    with pytest.raises(ValueError, match="thread_id"):
        scopes.bind(scope="tenant", thread_id="")
    first = scopes.bind(scope="tenant", thread_id="thread")
    first.provenance.record("notes.txt")
    with pytest.raises(ValueError, match="capacity"):
        scopes.bind(scope="tenant", thread_id="other")
    assert scopes.bind(scope="tenant", thread_id="thread") is first
    assert first.provenance.integrity_of("notes.txt") is SourceIntegrity.UNTRUSTED
