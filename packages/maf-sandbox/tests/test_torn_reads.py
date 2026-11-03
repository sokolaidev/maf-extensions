"""Input consistency is path-specific and scoped to one tool call's reads."""

import asyncio
import gc
import logging
from weakref import ref

import pytest

from maf_sandbox import (
    CallerContext,
    FileStoreChanged,
    FileStoreProvenance,
    Isolation,
    ListedFile,
    SandboxObserver,
    SandboxRouter,
    SandboxSpec,
    SourceIntegrity,
    StoreFileRead,
)
from maf_sandbox.maf import (
    NOT_COMPLETED_TEXT,
    SandboxResult,
    SandboxToolSession,
    file_store_provenance_middleware,
    sandboxed_tool,
)
from maf_sandbox.testing import InProcessSandboxBackend


def trusted_record():
    record = FileStoreProvenance(floor=SourceIntegrity.TRUSTED)
    file_store_provenance_middleware(record)
    return record


def attach(record, store, *, listing=None, observed=None, between=None):
    async def default_listing(_store):
        return [ListedFile(name, SourceIntegrity.TRUSTED) for name in store.files]

    context = CallerContext(lambda: "scope", lambda: "thread", listing or default_listing)
    router = SandboxRouter(
        [InProcessSandboxBackend()], min_isolation=Isolation.NONE, observer=observed
    )

    def build(session):
        async def read_inputs(files: list[str]):
            listed = await session.list_files(store)
            assert not isinstance(listed, str)
            for index, name in enumerate(files):
                item = await session.read_file(
                    store,
                    next(entry for entry in listed if entry.name == name),
                    named=f"files[{index}]",
                )
                assert not isinstance(item, str) and item is not None
                if between is not None:
                    await between(index)
            return SandboxResult(completed=True, output=("report",))

        return read_inputs

    return sandboxed_tool(
        build,
        router=router,
        context=context,
        agent_id="agent",
        spec=SandboxSpec(kind="test"),
        name="read_inputs",
        source_integrity=SourceIntegrity.UNTRUSTED,
        result_contract=True,
        file_store_provenance=record,
    )[0]


class Store:
    def __init__(self, files=None, during=None):
        self.files = files or {"a": "private workload", "b": "other workload"}
        self.during = during
        self.reads = []

    async def read(self, path):
        self.reads.append(path)
        content = self.files.get(path)
        if self.during is not None:
            self.during(path)
        return content


@pytest.mark.parametrize("when", ["listing", "read", "between"])
@pytest.mark.parametrize("path", ["a", "b", "unrelated"])
def test_mutations_are_scoped_to_selected_paths(when, path):
    record = trusted_record()
    events = []

    class Observer(SandboxObserver):
        def store_file_read(self, event):
            events.append(event)

    store = Store(during=(lambda _: record.record(path)) if when == "read" else None)

    async def listing(_store):
        result = [ListedFile(name, SourceIntegrity.TRUSTED) for name in store.files]
        if when == "listing":
            record.record(path)
        return result

    async def between(index):
        if when == "between" and index == 0:
            record.record(path)

    tool = attach(record, store, listing=listing, observed=Observer(), between=between)
    items = asyncio.run(tool.invoke(arguments={"files": ["a", "b"]}))
    if path == "unrelated":
        assert items[-1].text == "report"
        assert items[-1].additional_properties["security_label"]["integrity"] == "trusted"
    else:
        assert items[0].text == NOT_COMPLETED_TEXT
        assert any("changed while this call" in item.text for item in items)
        assert all(
            item.text not in {"report", "private workload", "other workload"} for item in items
        )
        assert any(
            isinstance(event, StoreFileRead)
            and event.outcome == "refused"
            and event.characters == 0
            for event in events
        )
    assert not record._readers


@pytest.mark.parametrize("label", [None, *SourceIntegrity])
@pytest.mark.parametrize("when", ["listing", "read"])
def test_custom_record_can_distinguish_stale_from_unestablished(label, when):
    class Record(FileStoreProvenance):
        stale = False

        def state_of(self, path):
            if self.stale:
                raise FileStoreChanged("must never expose these provider details")
            return label, 0

    record = Record()
    store = Store(during=(lambda _: setattr(record, "stale", True)) if when == "read" else None)

    async def listing(_store):
        record.stale = when == "listing"
        return [ListedFile("a", label)]

    tool = attach(record, store, listing=listing)
    items = asyncio.run(tool.invoke(arguments={"files": ["a"]}))
    assert items[0].text == NOT_COMPLETED_TEXT
    assert "provider details" not in str(items)


@pytest.mark.parametrize("changed", [False, True])
@pytest.mark.parametrize("content", ["text", b"\x00\xff"])
def test_host_counter_change_rechecks_the_actual_bytes(changed, content):
    class Record(FileStoreProvenance):
        counter = 0

        def state_of(self, path):
            return SourceIntegrity.TRUSTED, self.counter

    record = Record()
    store = Store({"a": content})

    def mutate(_path):
        record.counter += 1
        if changed:
            store.files["a"] = b"different" if isinstance(content, bytes) else "different"

    store.during = mutate
    tool = attach(record, store)
    items = asyncio.run(tool.invoke(arguments={"files": ["a"]}))
    assert store.reads == ["a", "a"]
    assert (items[0].text == NOT_COMPLETED_TEXT) is changed
    if not changed:
        assert items[-1].additional_properties["security_label"]["integrity"] == "trusted"


def test_concurrent_calls_have_independent_windows_and_can_retry():
    async def exercise():
        record = trusted_record()
        first_read = asyncio.Event()
        changed = asyncio.Event()

        async def between(index):
            if asyncio.current_task().get_name() == "first" and index == 0:
                first_read.set()
                await changed.wait()

        tool = attach(record, Store(), between=between)
        first = asyncio.create_task(tool.invoke(arguments={"files": ["a", "b"]}), name="first")
        await first_read.wait()
        record.record("b")
        record.forget("b")
        second = await tool.invoke(arguments={"files": ["a", "b"]})
        changed.set()
        refused = await first
        assert refused[0].text == NOT_COMPLETED_TEXT
        assert second[-1].text == "report"
        assert second[-1].additional_properties["security_label"]["integrity"] == "trusted"
        assert not record._readers

    asyncio.run(exercise())


def test_read_window_does_not_keep_permanent_path_history():
    record = trusted_record()
    window = record.read_window()
    weak = ref(window)
    for index in range(100):
        record.record(f"temporary/{index}")
        record.forget(f"temporary/{index}")
    assert window.changed("temporary/0")
    window.close()
    assert not window.changed("temporary/0")
    del window
    gc.collect()
    assert weak() is None
    assert not record._readers
    assert len(record) == 0


@pytest.mark.parametrize("hidden", [False, True])
def test_direct_read_refusal_obeys_name_redaction(hidden):
    record = trusted_record()
    store = Store(during=record.record)
    session = SandboxToolSession(
        SandboxRouter([InProcessSandboxBackend()], min_isolation=Isolation.NONE),
        CallerContext(lambda: "scope", lambda: "thread", lambda _: None),
        "agent",
        SandboxSpec(kind="test"),
        name="test",
        logger=logging.getLogger(__name__),
        file_store_provenance=record,
    )
    result = asyncio.run(
        session.read_file(
            store, ListedFile("a", SourceIntegrity.TRUSTED), at="files[0]", hidden=hidden
        )
    )
    assert isinstance(result, str)
    assert ("'a'" in result) is not hidden
    assert ("files[0]" in result) is hidden
    assert "private workload" not in result


def test_never_recorded_stays_unestablished():
    record = FileStoreProvenance()
    tool = attach(record, Store())
    items = asyncio.run(tool.invoke(arguments={"files": ["a"]}))
    assert items[-1].text == "report"
    assert items[-1].additional_properties["security_label"]["integrity"] == "untrusted"


def test_cancellation_closes_a_window_retained_by_a_child():
    async def exercise():
        record = trusted_record()
        entered = asyncio.Event()

        class WaitingStore(Store):
            async def read(self, path):
                entered.set()
                await asyncio.Event().wait()

        tool = attach(record, WaitingStore())
        task = asyncio.create_task(tool.invoke(arguments={"files": ["a"]}))
        await entered.wait()
        window = next(iter(record._readers))
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        record.record("a")
        assert not window.changed("a")

    asyncio.run(exercise())


def test_a_write_after_input_collection_does_not_invalidate_staged_bytes():
    record = trusted_record()

    async def after_read(_index):
        record.record("a")

    tool = attach(record, Store(), between=after_read)
    items = asyncio.run(tool.invoke(arguments={"files": ["a"]}))
    assert items[-1].text == "report"
    assert items[-1].additional_properties["security_label"]["integrity"] == "trusted"


@pytest.mark.parametrize("guidance", [(), ("Fixed guidance for {call_id}",)])
def test_legacy_tool_refusal_preserves_guidance_without_workload_labels(guidance):
    record = trusted_record()
    store = Store(during=record.record)

    async def listing(_store):
        return [ListedFile("a", SourceIntegrity.TRUSTED)]

    def build(session):
        async def read_inputs():
            listed = await session.list_files(store)
            await session.read_file(store, listed[0], named="files[0]")
            pytest.fail("A torn read must stop the body")

        return read_inputs

    tool = sandboxed_tool(
        build,
        router=SandboxRouter([InProcessSandboxBackend()], min_isolation=Isolation.NONE),
        context=CallerContext(lambda: "scope", lambda: "thread", listing),
        agent_id="agent",
        spec=SandboxSpec(kind="test"),
        name="read_inputs",
        source_integrity=SourceIntegrity.UNTRUSTED,
        standing_guidance=guidance,
        file_store_provenance=record,
    )[0]
    items = asyncio.run(tool.invoke(arguments={}))
    assert "changed while this call" in items[0].text
    assert len(items) == 1 + len(guidance)
    assert all(
        item.additional_properties["security_label"]["integrity"] == "trusted" for item in items
    )
    if guidance:
        assert items[-1].text.startswith("Fixed guidance for ")
        assert "{call_id}" not in items[-1].text
