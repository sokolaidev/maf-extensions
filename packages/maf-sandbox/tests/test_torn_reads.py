"""Input consistency is path-specific and scoped to one tool call's reads."""

import asyncio
import gc
import logging
import threading
from weakref import ref

import pytest

import maf_sandbox.maf as maf_module
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
from maf_sandbox._observer import fed_with
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


@pytest.mark.parametrize("custom", [False, True])
@pytest.mark.parametrize("second", ["same_session", "same_store", "other_store", "unrecorded"])
@pytest.mark.parametrize("outcome", ["content", "error", "absent"])
def test_prior_input_is_rechecked_across_read_boundaries(custom, second, outcome):
    class HostRecord(FileStoreProvenance):
        stale = False

        def state_of(self, path):
            if self.stale and path == "a":
                raise FileStoreChanged("private provider detail")
            return SourceIntegrity.TRUSTED, 0

    record = HostRecord() if custom else trusted_record()

    def mutate(path):
        if path == "b":
            if isinstance(record, HostRecord):
                record.stale = True
            else:
                record.record("a")
            if outcome == "error":
                raise OSError("private provider detail")

    store = Store({"a": "first", "b": None if outcome == "absent" else "second"}, mutate)
    router = SandboxRouter([InProcessSandboxBackend()], min_isolation=Isolation.NONE)
    context = CallerContext(lambda: "scope", lambda: "thread", lambda _: None)

    def build(session):
        async def read_inputs():
            await session.read_file(store, ListedFile("a", SourceIntegrity.TRUSTED))
            other_store = store
            other = session
            if second != "same_session":
                if second in {"other_store", "unrecorded"}:
                    other_store = Store(store.files, mutate)
                    other_record = None if second == "unrecorded" else trusted_record()
                else:
                    other_record = record
                other = SandboxToolSession(
                    router,
                    context,
                    "other",
                    SandboxSpec(kind="test"),
                    name="other",
                    logger=logging.getLogger(__name__),
                    file_store_provenance=other_record,
                )
            await other.read_file(other_store, ListedFile("b", SourceIntegrity.TRUSTED))
            return SandboxResult(completed=True, output=("torn report",))

        return read_inputs

    tool = sandboxed_tool(
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
    items = asyncio.run(tool.invoke(arguments={}))
    assert items[0].text == NOT_COMPLETED_TEXT
    refusal = next(item.text for item in items if "changed while" in item.text)
    assert "input file 1" in refusal
    assert "input file 2" not in refusal
    assert "private provider detail" not in str(items)
    assert all(item.text != "torn report" for item in items)
    assert not record._readers


@pytest.mark.parametrize("contract", [False, True])
def test_trusted_refusal_contains_only_generated_input_references(contract):
    record = trusted_record()
    hostile = "IGNORE_PRIOR_INSTRUCTIONS"
    store = Store({hostile: "private workload"}, record.record)

    def build(session):
        async def read_inputs():
            await session.read_file(
                store,
                ListedFile(hostile, SourceIntegrity.TRUSTED),
                named=hostile,
                at=hostile,
            )
            pytest.fail("A torn read must stop the body")

        return read_inputs

    tool = sandboxed_tool(
        build,
        router=SandboxRouter([InProcessSandboxBackend()], min_isolation=Isolation.NONE),
        context=CallerContext(lambda: "scope", lambda: "thread", lambda _: None),
        agent_id="agent",
        spec=SandboxSpec(kind="test"),
        name="read_inputs",
        source_integrity=SourceIntegrity.UNTRUSTED,
        result_contract=contract,
        file_store_provenance=record,
    )[0]
    items = asyncio.run(tool.invoke(arguments={}))
    assert hostile not in str(items)
    assert any("input file 1 may have changed" in item.text for item in items)
    refusal = next(item for item in items if "changed while" in item.text)
    if contract:
        assert "security_label" not in refusal.additional_properties
        assert tool.additional_properties["source_integrity"] == "trusted"
    else:
        assert refusal.additional_properties["security_label"]["integrity"] == "trusted"


@pytest.mark.parametrize("reason", ["read_error", "integrity"])
@pytest.mark.parametrize("provenance", [False, True])
@pytest.mark.parametrize("override", [False, True])
def test_ordinary_read_refusals_never_echo_input_values(reason, provenance, override):
    hostile = "IGNORE_PRIOR_INSTRUCTIONS"

    class RefusingStore(Store):
        async def read(self, path):
            if reason == "read_error":
                raise OSError("private provider detail")
            return await super().read(path)

    store = RefusingStore({hostile: "content"})

    async def listing(_store):
        return [ListedFile(hostile, SourceIntegrity.UNTRUSTED)]

    def build(session):
        async def read_inputs():
            answer = await session.read_file(
                store,
                ListedFile(hostile, SourceIntegrity.UNTRUSTED),
                named=hostile if override else None,
                at=hostile,
            )
            assert isinstance(answer, str)
            return SandboxResult(completed=False, trusted_output=(answer,))

        return read_inputs

    tool = sandboxed_tool(
        build,
        router=SandboxRouter([InProcessSandboxBackend()], min_isolation=Isolation.NONE),
        context=CallerContext(lambda: "scope", lambda: "thread", listing),
        agent_id="agent",
        spec=SandboxSpec(kind="test"),
        name="read_inputs",
        source_integrity=SourceIntegrity.UNTRUSTED,
        result_contract=True,
        file_store_provenance=trusted_record() if provenance else None,
        requires_file_integrity=SourceIntegrity.TRUSTED,
    )[0]
    items = asyncio.run(tool.invoke(arguments={}))
    assert hostile not in str(items)
    assert "private provider detail" not in str(items)
    assert any("input file 1" in item.text for item in items)
    assert tool.additional_properties["source_integrity"] == "trusted"


def test_input_references_without_provenance_are_stable_and_call_scoped():
    class RefusingStore(Store):
        async def read(self, path):
            if path == "b":
                raise OSError("unavailable")
            return await super().read(path)

    store = RefusingStore()

    async def listing(_store):
        return [ListedFile(name) for name in store.files]

    def build(session):
        async def read_inputs(files: list[str]):
            for path in files:
                answer = await session.read_file(store, ListedFile(path))
                if isinstance(answer, str):
                    return SandboxResult(completed=False, trusted_output=(answer,))
            pytest.fail("One input must be refused")

        return read_inputs

    tool = sandboxed_tool(
        build,
        router=SandboxRouter([InProcessSandboxBackend()], min_isolation=Isolation.NONE),
        context=CallerContext(lambda: "scope", lambda: "thread", listing),
        agent_id="agent",
        spec=SandboxSpec(kind="test"),
        name="read_inputs",
        source_integrity=SourceIntegrity.UNTRUSTED,
        result_contract=True,
    )[0]
    first = asyncio.run(tool.invoke(arguments={"files": ["a", "a", "b"]}))
    second = asyncio.run(tool.invoke(arguments={"files": ["b"]}))
    assert "input file 2" in first[-1].text
    assert "input file 1" in second[-1].text


@pytest.mark.parametrize("contract", [False, True])
@pytest.mark.parametrize("when", ["listing", "read", "between"])
@pytest.mark.parametrize("swallow", [False, True])
def test_synchronous_tools_refuse_torn_reads_and_close_windows(contract, when, swallow):
    from agent_framework import Content

    record = trusted_record()
    store = Store(during=record.record if when == "read" else None)

    async def listing(_store):
        if when == "listing":
            record.record("a")
        return [ListedFile(name, SourceIntegrity.TRUSTED) for name in store.files]

    def build(session):
        def read_inputs():
            listed = asyncio.run(session.list_files(store))
            for index, item in enumerate(listed):
                try:
                    asyncio.run(session.read_file(store, item))
                except Exception:
                    if not swallow:
                        raise
                if when == "between" and index == 0:
                    record.record("a")
            if contract:
                return SandboxResult(completed=True, output=("torn report",))
            return [Content.from_text("torn report"), Content.from_text("Fixed guidance")]

        return read_inputs

    tool = sandboxed_tool(
        build,
        router=SandboxRouter([InProcessSandboxBackend()], min_isolation=Isolation.NONE),
        context=CallerContext(lambda: "scope", lambda: "thread", listing),
        agent_id="agent",
        spec=SandboxSpec(kind="test"),
        name="read_inputs",
        source_integrity=SourceIntegrity.UNTRUSTED,
        result_contract=contract,
        standing_guidance=("Fixed guidance",),
        file_store_provenance=record,
    )[0]
    items = asyncio.run(tool.invoke(arguments={}))
    assert any("changed while this call" in item.text for item in items)
    assert all(item.text != "torn report" for item in items)
    assert items[-1].text == "Fixed guidance"
    assert not record._readers


@pytest.mark.parametrize("changed", [False, True])
@pytest.mark.parametrize("other_store", [False, True])
@pytest.mark.parametrize("outcome", ["content", "error", "absent"])
def test_counter_only_record_rechecks_prior_inputs(changed, other_store, outcome):
    class Record(FileStoreProvenance):
        counter = 0

        def state_of(self, path):
            return SourceIntegrity.TRUSTED, self.counter

    record = Record()
    first = Store({"a": "old-a", "b": "old-b"})

    class LaterStore(Store):
        async def read(self, path):
            self.reads.append(path)
            if path == "b" and self.reads.count("b") == 1:
                record.counter += 1
                if changed:
                    first.files["a"] = "new-a"
                self.files["b"] = "new-b"
                if outcome == "error":
                    raise OSError("unavailable")
                if outcome == "absent":
                    self.files["b"] = None
            return self.files.get(path)

    later = LaterStore(first.files)
    if not other_store:
        first = later

    async def listing(_store):
        return []

    router = SandboxRouter([InProcessSandboxBackend()], min_isolation=Isolation.NONE)
    context = CallerContext(lambda: "scope", lambda: "thread", listing)

    def build(session):
        async def read_inputs():
            await session.read_file(first, ListedFile("a", SourceIntegrity.TRUSTED))
            other = session
            if other_store:
                other = SandboxToolSession(
                    router,
                    context,
                    "other",
                    SandboxSpec(kind="test"),
                    name="other",
                    logger=logging.getLogger(__name__),
                    file_store_provenance=Record(),
                )
            await other.read_file(later, ListedFile("b", SourceIntegrity.TRUSTED))
            return SandboxResult(completed=True, output=("report",))

        return read_inputs

    tool = sandboxed_tool(
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
    items = asyncio.run(tool.invoke(arguments={}))
    assert (items[0].text == NOT_COMPLETED_TEXT) is changed
    if changed:
        assert any("input file 1" in item.text for item in items)
    else:
        assert items[-1].text == "report"
        assert items[-1].additional_properties["security_label"]["integrity"] == "trusted"


@pytest.mark.parametrize("victim", ["a", "b", "unrelated"])
def test_verification_reads_are_counter_bracketed_for_every_input(victim):
    class Record(FileStoreProvenance):
        counter = 0

        def state_of(self, path):
            return SourceIntegrity.TRUSTED, self.counter

    record = Record()

    class RacingStore(Store):
        async def read(self, path):
            self.reads.append(path)
            captured = self.files.get(path)
            if path == "b" and self.reads.count("b") in {1, 2}:
                record.counter += 1
                if self.reads.count("b") == 2:
                    self.files[victim] = "changed"
            return captured

    store = RacingStore()
    items = asyncio.run(attach(record, store).invoke(arguments={"files": ["a", "b"]}))
    assert (items[0].text == NOT_COMPLETED_TEXT) is (victim != "unrelated")
    assert len(store.reads) <= 8


def test_counter_verification_has_a_bounded_retry_budget():
    class Record(FileStoreProvenance):
        counter = 0

        def state_of(self, path):
            return SourceIntegrity.TRUSTED, self.counter

    record = Record()
    store = Store(during=lambda _: setattr(record, "counter", record.counter + 1))
    items = asyncio.run(attach(record, store).invoke(arguments={"files": ["a"]}))
    assert items[0].text == NOT_COMPLETED_TEXT
    assert len(store.reads) <= 4


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
        if len(store.reads) > 1:
            return
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


class ContendedLock:
    def __init__(self, contended):
        self._lock = threading.Lock()
        self.contended = contended

    def __enter__(self):
        if not self._lock.acquire(blocking=False):
            self.contended.set()
            assert self._lock.acquire(timeout=5)
        return self

    def __exit__(self, *_args):
        self._lock.release()


@pytest.mark.parametrize("changed", [False, True])
def test_threaded_readers_retain_one_window(monkeypatch, changed):
    entered = threading.Event()
    proceed = threading.Event()
    files = maf_module._FileReads()
    monkeypatch.setattr(files, "lock", ContendedLock(proceed))
    monkeypatch.setattr(maf_module, "_FileReads", lambda: files)
    record = trusted_record()
    make_window = record.read_window
    windows = []

    def paused_window():
        if not entered.is_set():
            entered.set()
            assert proceed.wait(5)
        window = make_window()
        windows.append(window)
        return window

    monkeypatch.setattr(record, "read_window", paused_window)
    store = Store({"a": "first", "b": "second", "c": "last"})

    def build(session):
        def read(path):
            try:
                return asyncio.run(session.read_file(store, ListedFile(path, None)))
            finally:
                if path == "b":
                    proceed.set()

        async def read_inputs():
            first = asyncio.create_task(asyncio.to_thread(read, "a"))
            assert await asyncio.to_thread(entered.wait, 5)
            second = asyncio.create_task(asyncio.to_thread(read, "b"))
            await asyncio.gather(first, second)
            if changed:
                record.record("b")
                record.forget("b")
            await session.read_file(store, ListedFile("c", None))
            return SandboxResult(completed=True, output=("report",))

        return read_inputs

    tool = sandboxed_tool(
        build,
        router=SandboxRouter([InProcessSandboxBackend()], min_isolation=Isolation.NONE),
        context=CallerContext(lambda: "scope", lambda: "thread", lambda _: None),
        agent_id="agent",
        spec=SandboxSpec(kind="test"),
        name="read_inputs",
        result_contract=True,
        file_store_provenance=record,
        source_integrity=SourceIntegrity.UNTRUSTED,
    )[0]
    items = asyncio.run(tool.invoke())
    if changed:
        assert items[0].text == NOT_COMPLETED_TEXT
    else:
        assert items[-1].text == "report"
    assert len(windows) == 1
    assert not record._readers


def test_threaded_input_numbers_are_unique(monkeypatch):
    entered = threading.Event()
    proceed = threading.Event()

    class PausedInputs(dict):
        def __len__(self):
            size = super().__len__()
            if not entered.is_set():
                entered.set()
                assert proceed.wait(5)
            return size

    files = maf_module._FileReads()
    monkeypatch.setattr(files, "lock", ContendedLock(proceed))
    files.file_inputs = PausedInputs()
    monkeypatch.setattr(maf_module, "_FileReads", lambda: files)

    def fail_read(_path):
        raise OSError

    store = Store(during=fail_read)

    def build(session):
        def read(path):
            try:
                return asyncio.run(session.read_file(store, ListedFile(path, None)))
            finally:
                if path == "b":
                    proceed.set()

        async def read_inputs():
            first = asyncio.create_task(asyncio.to_thread(read, "a"))
            assert await asyncio.to_thread(entered.wait, 5)
            second = asyncio.create_task(asyncio.to_thread(read, "b"))
            results = await asyncio.gather(first, second)
            assert results == [
                "Error: input file 1 could not be read from the file store",
                "Error: input file 2 could not be read from the file store",
            ]
            return SandboxResult(completed=True, output=("report",))

        return read_inputs

    tool = sandboxed_tool(
        build,
        router=SandboxRouter([InProcessSandboxBackend()], min_isolation=Isolation.NONE),
        context=CallerContext(lambda: "scope", lambda: "thread", lambda _: None),
        agent_id="agent",
        spec=SandboxSpec(kind="test"),
        name="read_inputs",
        result_contract=True,
        source_integrity=SourceIntegrity.UNTRUSTED,
    )[0]
    asyncio.run(tool.invoke())


def test_threaded_readers_can_add_inputs_during_consistency_checks():
    checking = threading.Event()
    added = threading.Event()
    armed = False

    class Record(FileStoreProvenance):
        def state_of(self, path):
            if armed and path == "a" and threading.current_thread().name == "checking":
                checking.set()
                assert added.wait(5)
            return SourceIntegrity.TRUSTED, 0

    record = Record()
    store = Store({"a": "first", "b": "second", "c": "last"})

    def build(session):
        def read(path):
            thread = threading.current_thread()
            previous = thread.name
            thread.name = "checking" if path == "b" else "adding"
            try:
                return asyncio.run(session.read_file(store, ListedFile(path, None)))
            finally:
                thread.name = previous
                if path == "c":
                    added.set()

        async def read_inputs():
            nonlocal armed
            await session.read_file(store, ListedFile("a", None))
            armed = True
            first = asyncio.create_task(asyncio.to_thread(read, "b"))
            assert await asyncio.to_thread(checking.wait, 5)
            second = asyncio.create_task(asyncio.to_thread(read, "c"))
            await asyncio.gather(first, second)
            return SandboxResult(completed=True, output=("report",))

        return read_inputs

    tool = sandboxed_tool(
        build,
        router=SandboxRouter([InProcessSandboxBackend()], min_isolation=Isolation.NONE),
        context=CallerContext(lambda: "scope", lambda: "thread", lambda _: None),
        agent_id="agent",
        spec=SandboxSpec(kind="test"),
        name="read_inputs",
        result_contract=True,
        file_store_provenance=record,
        source_integrity=SourceIntegrity.UNTRUSTED,
    )[0]
    items = asyncio.run(tool.invoke())
    assert items[-1].text == "report"
    assert not record._readers


def test_threaded_reads_preserve_the_weakest_input_label(monkeypatch):
    entered = threading.Event()
    proceed = threading.Event()
    files = maf_module._FileReads()
    monkeypatch.setattr(files, "lock", ContendedLock(proceed))
    monkeypatch.setattr(maf_module, "_FileReads", lambda: files)

    def paused_fold(fed, integrity):
        if integrity is SourceIntegrity.TRUSTED:
            entered.set()
            assert proceed.wait(5)
        return fed_with(fed, integrity)

    monkeypatch.setattr(maf_module, "fed_with", paused_fold)
    store = Store()

    def build(session):
        def read(path, integrity):
            try:
                return asyncio.run(session.read_file(store, ListedFile(path, integrity)))
            finally:
                if path == "b":
                    proceed.set()

        async def read_inputs():
            first = asyncio.create_task(asyncio.to_thread(read, "a", SourceIntegrity.TRUSTED))
            assert await asyncio.to_thread(entered.wait, 5)
            second = asyncio.create_task(asyncio.to_thread(read, "b", SourceIntegrity.UNTRUSTED))
            await asyncio.gather(first, second)
            return SandboxResult(completed=True, output=("report",))

        return read_inputs

    tool = sandboxed_tool(
        build,
        router=SandboxRouter([InProcessSandboxBackend()], min_isolation=Isolation.NONE),
        context=CallerContext(lambda: "scope", lambda: "thread", lambda _: None),
        agent_id="agent",
        spec=SandboxSpec(kind="test"),
        name="read_inputs",
        result_contract=True,
        source_integrity=SourceIntegrity.UNTRUSTED,
    )[0]
    items = asyncio.run(tool.invoke())
    assert items[-1].text == "report"
    assert items[-1].additional_properties["security_label"]["integrity"] == "untrusted"


@pytest.mark.parametrize("phase", ["verification", "acceptance"])
@pytest.mark.parametrize("other_store", [False, True])
@pytest.mark.parametrize("changed", [False, True])
def test_checks_include_inputs_added_concurrently(phase, other_store, changed):
    entered = threading.Event()
    proceed = threading.Event()

    class Record(FileStoreProvenance):
        counter = 0
        samples = 0

        def state_of(self, path):
            if threading.current_thread().name == "checking" and path == "a":
                self.samples += 1
                if phase == "acceptance" and self.samples == 2:
                    entered.set()
                    assert proceed.wait(5)
            return SourceIntegrity.TRUSTED, self.counter

    record = Record()

    class WaitingStore(Store):
        async def read(self, path):
            self.reads.append(path)
            if path == "a" and phase == "verification":
                if self.reads.count("a") == 1:
                    record.counter += 1
                elif self.reads.count("a") == 2:
                    entered.set()
                    assert proceed.wait(5)
            return self.files.get(path)

    first_store = WaitingStore({"a": "first", "c": "second"})
    later_store = Store({"c": "second"}) if other_store else first_store
    later_record = trusted_record() if other_store else record
    router = SandboxRouter([InProcessSandboxBackend()], min_isolation=Isolation.NONE)
    context = CallerContext(lambda: "scope", lambda: "thread", lambda _: None)

    def build(session):
        other = SandboxToolSession(
            router,
            context,
            "other",
            SandboxSpec(kind="test"),
            name="other",
            logger=logging.getLogger(__name__),
            file_store_provenance=later_record,
        )

        def read():
            thread = threading.current_thread()
            previous = thread.name
            thread.name = "checking"
            try:
                return asyncio.run(
                    session.read_file(first_store, ListedFile("a", SourceIntegrity.TRUSTED))
                )
            finally:
                thread.name = previous

        async def read_inputs():
            first = asyncio.create_task(asyncio.to_thread(read))
            assert await asyncio.to_thread(entered.wait, 5)
            try:
                await other.read_file(later_store, ListedFile("c", SourceIntegrity.TRUSTED))
                if changed:
                    later_record.record("c")
            finally:
                proceed.set()
            await first
            return SandboxResult(completed=True, output=("report",))

        return read_inputs

    tool = sandboxed_tool(
        build,
        router=router,
        context=context,
        agent_id="agent",
        spec=SandboxSpec(kind="test"),
        name="read_inputs",
        result_contract=True,
        file_store_provenance=record,
        source_integrity=SourceIntegrity.UNTRUSTED,
    )[0]
    items = asyncio.run(tool.invoke())
    if changed:
        assert items[0].text == NOT_COMPLETED_TEXT
    else:
        assert items[-1].text == "report"
        assert items[-1].additional_properties["security_label"]["integrity"] == "trusted"
    assert not record._readers
    assert not later_record._readers


def test_continuous_input_registration_has_a_bounded_retry_budget():
    requested = [threading.Event() for _ in range(3)]
    released = [threading.Event() for _ in range(3)]

    class Record(FileStoreProvenance):
        samples = 0

        def state_of(self, path):
            if threading.current_thread().name == "checking" and path == "a":
                self.samples += 1
                if self.samples % 2 == 0:
                    index = (self.samples - 2) // 2
                    assert index < len(requested)
                    requested[index].set()
                    assert released[index].wait(5)
            return SourceIntegrity.TRUSTED, 0

    record = Record()
    first_store = Store({"a": "first", **{f"b{index}": "later" for index in range(3)}})

    def build(session):
        def read():
            thread = threading.current_thread()
            previous = thread.name
            thread.name = "checking"
            try:
                return asyncio.run(
                    session.read_file(first_store, ListedFile("a", SourceIntegrity.TRUSTED))
                )
            finally:
                thread.name = previous

        async def read_inputs():
            first = asyncio.create_task(asyncio.to_thread(read))
            for index, (request, release) in enumerate(zip(requested, released, strict=True)):
                assert await asyncio.to_thread(request.wait, 5)
                try:
                    await session.read_file(
                        first_store, ListedFile(f"b{index}", SourceIntegrity.TRUSTED)
                    )
                finally:
                    release.set()
            await first
            return SandboxResult(completed=True, output=("report",))

        return read_inputs

    tool = sandboxed_tool(
        build,
        router=SandboxRouter([InProcessSandboxBackend()], min_isolation=Isolation.NONE),
        context=CallerContext(lambda: "scope", lambda: "thread", lambda _: None),
        agent_id="agent",
        spec=SandboxSpec(kind="test"),
        name="read_inputs",
        result_contract=True,
        file_store_provenance=record,
        source_integrity=SourceIntegrity.UNTRUSTED,
    )[0]
    items = asyncio.run(tool.invoke())
    assert items[0].text == NOT_COMPLETED_TEXT
    assert record.samples == 7
    assert not record._readers


@pytest.mark.parametrize(("native", "collection"), [(False, 1), (True, 1), (True, 2)])
@pytest.mark.parametrize("other_store", [False, True])
@pytest.mark.parametrize("changed", [False, True])
def test_later_state_checks_cannot_invalidate_earlier_inputs(
    monkeypatch, native, collection, other_store, changed
):
    class CounterRecord(FileStoreProvenance):
        counter = 0

        def state_of(self, path):
            return SourceIntegrity.TRUSTED, self.counter

    first_record = trusted_record() if native else CounterRecord()
    later_record = (
        (trusted_record() if native else CounterRecord()) if other_store else first_record
    )
    first_store = Store({"a": "first", "b": "second"})
    later_store = Store({"b": "second"}) if other_store else first_store
    sampled = later_record.state_of
    mutated = False
    samples = 0

    def sample(path):
        nonlocal mutated, samples
        if path == "b" and "b" in later_store.reads:
            samples += 1
        if path == "b" and samples == collection and not mutated:
            mutated = True
            victim = "a" if changed else "unrelated"
            first_store.files[victim] = "changed"
            if isinstance(first_record, CounterRecord):
                first_record.counter += 1
            else:
                first_record.record(victim)
        return sampled(path)

    monkeypatch.setattr(later_record, "state_of", sample)
    router = SandboxRouter([InProcessSandboxBackend()], min_isolation=Isolation.NONE)
    context = CallerContext(lambda: "scope", lambda: "thread", lambda _: None)

    def build(session):
        other = SandboxToolSession(
            router,
            context,
            "other",
            SandboxSpec(kind="test"),
            name="other",
            logger=logging.getLogger(__name__),
            file_store_provenance=later_record,
        )

        async def read_inputs():
            await session.read_file(first_store, ListedFile("a", SourceIntegrity.TRUSTED))
            await other.read_file(later_store, ListedFile("b", SourceIntegrity.TRUSTED))
            return SandboxResult(completed=True, output=("report",))

        return read_inputs

    tool = sandboxed_tool(
        build,
        router=router,
        context=context,
        agent_id="agent",
        spec=SandboxSpec(kind="test"),
        name="read_inputs",
        result_contract=True,
        file_store_provenance=first_record,
        source_integrity=SourceIntegrity.UNTRUSTED,
    )[0]
    items = asyncio.run(tool.invoke())
    assert mutated
    if changed:
        assert items[0].text == NOT_COMPLETED_TEXT
    else:
        assert items[-1].text == "report"
        assert items[-1].additional_properties["security_label"]["integrity"] == "trusted"


def test_native_record_can_clear_continuous_unrelated_writes(monkeypatch):
    record = trusted_record()
    sampled = record.state_of
    samples = 0

    def sample(path):
        nonlocal samples
        samples += 1
        record.record(f"unrelated/{samples}")
        return sampled(path)

    monkeypatch.setattr(record, "state_of", sample)
    items = asyncio.run(attach(record, Store()).invoke(arguments={"files": ["a", "b"]}))
    assert items[-1].text == "report"
    assert items[-1].additional_properties["security_label"]["integrity"] == "trusted"
    assert not record._readers


@pytest.mark.parametrize("provenance", [False, True])
@pytest.mark.parametrize("in_flight", [False, True])
def test_threaded_reads_do_not_reopen_a_closed_call(provenance, in_flight):
    entered = threading.Event()
    proceed = threading.Event()
    record = trusted_record() if provenance else None
    children = []
    calls = []

    class WaitingStore(Store):
        async def read(self, path):
            if in_flight:
                entered.set()
                assert proceed.wait(5)
            return await super().read(path)

    store = WaitingStore()

    def build(session):
        def read():
            if not in_flight:
                entered.set()
                assert proceed.wait(5)
            return asyncio.run(session.read_file(store, ListedFile("a", None)))

        async def read_inputs():
            calls.append(maf_module._FILE_READS.get())
            children.append(asyncio.create_task(asyncio.to_thread(read)))
            assert await asyncio.to_thread(entered.wait, 5)
            return SandboxResult(completed=True, output=("report",))

        return read_inputs

    tool = sandboxed_tool(
        build,
        router=SandboxRouter([InProcessSandboxBackend()], min_isolation=Isolation.NONE),
        context=CallerContext(lambda: "scope", lambda: "thread", lambda _: None),
        agent_id="agent",
        spec=SandboxSpec(kind="test"),
        name="read_inputs",
        result_contract=True,
        file_store_provenance=record,
        source_integrity=SourceIntegrity.UNTRUSTED,
    )[0]

    async def exercise():
        await tool.invoke()
        proceed.set()
        if in_flight and provenance:
            with pytest.raises(RuntimeError, match="closed"):
                await children[0]
        else:
            result = await children[0]
            assert result.text == "private workload"

    asyncio.run(exercise())
    if record is not None:
        assert not record._readers
    assert calls[0].closed
    assert not calls[0].file_windows
    assert not calls[0].file_inputs


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
    assert "input file 1" in result
    assert "'a'" not in result
    assert "files[0]" not in result
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
