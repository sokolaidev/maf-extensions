"""File tools, listings, sinks and provenance share a host-bound native store."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
from threading import Event

import pytest
from agent_framework import (
    AgentSession,
    FunctionInvocationContext,
    InMemoryAgentFileStore,
    SessionContext,
)

from maf_sandbox import Artifact, FileStoreProvenance, SourceIntegrity
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
        lambda scope, thread: InMemoryAgentFileStore(),
        floor=SourceIntegrity.TRUSTED,
        read_only=False,
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
        listing = await a.caller_context().list_files(a.store)
        assert {item.name for item in listing} == {"notes.txt"}
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
    binding = ScopedFileStores(
        lambda scope, thread: InMemoryAgentFileStore(), read_only=False
    ).bind(scope="tenant", thread_id="thread")

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


def test_output_binding_exposes_only_reads_and_keeps_scratch_mutations_separate():
    outputs = ScopedFileStores(lambda scope, thread: InMemoryAgentFileStore())
    scratch = ScopedFileStores(lambda scope, thread: InMemoryAgentFileStore(), read_only=False)
    a = outputs.bind(scope="tenant", thread_id="a")
    b = outputs.bind(scope="tenant", thread_id="b")
    writable = scratch.bind(scope="tenant", thread_id="a")

    async def scenario():
        readers = await tools_for(a)
        writers = await tools_for(writable)
        assert readers
        for operation in ("write", "delete", "replace", "replace_lines"):
            assert f"file_access_{operation}" not in readers
            assert f"file_access_{operation}" in writers
        await invoke(writable, writers, "write", file_name="call/report.txt", content="model")
        sink = make_file_store_sink(a.store, provenance=a.provenance)
        await sink.deliver(
            Artifact(
                name="report.txt", content=b"result", kind="probe", media_type=None, call_id="call"
            )
        )
        assert (await invoke(a, readers, "read", file_name="call/report.txt"))[0].text == "result"
        await invoke(writable, writers, "delete", file_name="call/report.txt")
        assert await a.store.read("call/report.txt") == "result"
        assert await b.store.read("call/report.txt") is None
        assert a.provenance.integrity_of("call/report.txt") is SourceIntegrity.UNTRUSTED
        listing = await a.caller_context().list_files(a.store)
        assert [(item.name, item.integrity) for item in listing] == [
            ("call/report.txt", SourceIntegrity.UNTRUSTED)
        ]

    asyncio.run(scenario())


def test_restoration_finishes_before_concurrent_binding_is_published():
    store = InMemoryAgentFileStore()
    asyncio.run(store.write("notes.txt", "persisted model text"))
    entered, release, contender = Event(), Event(), Event()
    records = []

    def restore(scope, thread, native):
        assert (scope, thread, native) == ("tenant", "thread", store)
        record = FileStoreProvenance(floor=SourceIntegrity.TRUSTED)
        records.append(record)
        entered.set()
        assert release.wait(5)
        record.record("notes.txt")
        return record

    scopes = ScopedFileStores(lambda scope, thread: store, provenance_factory=restore)

    def concurrent_bind():
        contender.set()
        return scopes.bind(scope="tenant", thread_id="thread")

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(scopes.bind, scope="tenant", thread_id="thread")
        try:
            assert entered.wait(5)
            second = pool.submit(concurrent_bind)
            assert contender.wait(5)
            assert not second.done()
        finally:
            release.set()
        a, b = first.result(), second.result()
    assert a is b
    assert records == [a.provenance]
    assert a.provenance.integrity_of("notes.txt") is SourceIntegrity.UNTRUSTED
    assert a.provenance.integrity_of("host.txt") is SourceIntegrity.TRUSTED
    listing = asyncio.run(a.caller_context().list_files(a.store))
    assert listing[0].integrity is SourceIntegrity.UNTRUSTED


def test_failed_restoration_does_not_cache_a_binding():
    attempts = []

    def restore(scope, thread, store):
        attempts.append(store)
        if len(attempts) == 1:
            raise RuntimeError("restore failed")
        record = FileStoreProvenance()
        record.record("notes.txt")
        return record

    scopes = ScopedFileStores(
        lambda scope, thread: InMemoryAgentFileStore(), provenance_factory=restore
    )
    with pytest.raises(RuntimeError, match="restore failed"):
        scopes.bind(scope="tenant", thread_id="thread")
    bound = scopes.bind(scope="tenant", thread_id="thread")
    assert len(attempts) == 2
    assert bound.provenance.integrity_of("notes.txt") is SourceIntegrity.UNTRUSTED


def test_restoration_rejects_invalid_or_shared_records_and_ambiguous_floor():
    record = FileStoreProvenance()
    with pytest.raises(ValueError, match="floor"):
        ScopedFileStores(
            lambda scope, thread: InMemoryAgentFileStore(),
            floor=SourceIntegrity.TRUSTED,
            provenance_factory=lambda scope, thread, store: record,
        )
    invalid = ScopedFileStores(
        lambda scope, thread: InMemoryAgentFileStore(),
        provenance_factory=lambda scope, thread, store: None,
    )
    with pytest.raises(ValueError, match="distinct provenance"):
        invalid.bind(scope="tenant", thread_id="thread")
    shared = ScopedFileStores(
        lambda scope, thread: InMemoryAgentFileStore(),
        provenance_factory=lambda scope, thread, store: record,
    )
    shared.bind(scope="tenant", thread_id="a")
    with pytest.raises(ValueError, match="distinct provenance"):
        shared.bind(scope="tenant", thread_id="b")
