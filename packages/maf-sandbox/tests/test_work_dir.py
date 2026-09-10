"""Acquisition prepares the requested base without requiring a guest command."""

from __future__ import annotations

import asyncio

import pytest

from maf_sandbox import Capability, EntryKind, SandboxEntry, SandboxKey, SandboxSpec
from maf_sandbox.conformance import assert_storage_base_conformance
from maf_sandbox.paths import ensure_guest_work_dir
from maf_sandbox.testing import InProcessSandbox, InProcessSandboxBackend

_KEY = SandboxKey("work-dir", "thread", "agent")


@pytest.mark.parametrize("allocated", ["/runtime/private-prefix", "/another/store"])
@pytest.mark.parametrize("override", [None, "/image/configured-base"])
def test_relative_storage_contract_without_a_filesystem(allocated, override):
    async def scenario():
        sandbox = InProcessSandbox(storage_base=allocated)
        backend = InProcessSandboxBackend(sandbox)
        capabilities = frozenset(
            {
                Capability.FILES_IN,
                Capability.FILES_OUT,
                Capability.FILES_LIST,
                Capability.FILES_DELETE,
                Capability.RECLAIM,
            }
        )
        spec = SandboxSpec(
            kind="store", work_dir=override, requires=capabilities - {Capability.RECLAIM}
        )
        await backend.acquire(_KEY, spec)
        base = override if override is not None else allocated
        assert base in sandbox.directories
        await assert_storage_base_conformance(sandbox, capabilities)
        await sandbox.write_file("kept", b"warm", working_directory=".")
        assert await backend.acquire(_KEY, spec) is sandbox
        assert await sandbox.read_file("kept", working_directory=".", max_bytes=4) == b"warm"
        await sandbox.reset(timeout=1)
        assert base in sandbox.directories and sandbox.contents == {}
        assert sandbox.commands == []

    asyncio.run(scenario())


def test_a_later_acquire_cannot_retarget_a_held_sandbox():
    async def scenario():
        backend = InProcessSandboxBackend()
        sandbox = await backend.acquire(_KEY, SandboxSpec(kind="store", work_dir=None))
        await sandbox.write_file("kept", b"data", working_directory=".")
        with pytest.raises(ValueError, match="storage base"):
            await backend.acquire(_KEY, SandboxSpec(kind="store", work_dir="/elsewhere"))
        assert await sandbox.read_file("kept", working_directory=".", max_bytes=4) == b"data"

    asyncio.run(scenario())


@pytest.mark.parametrize("path", ["C:/agent/work", r"D:\agent\work", r"\\server\share\agent\work"])
def test_native_work_dir_is_prepared_repaired_and_retained_by_reset(path):
    async def scenario():
        backend = InProcessSandboxBackend()
        spec = SandboxSpec(kind="native", work_dir=path, requires=frozenset({Capability.EXEC}))
        sandbox = await backend.acquire(_KEY, spec)
        assert path in sandbox.directories
        assert sandbox.commands == []
        sandbox.directories.remove(path)
        assert await backend.acquire(_KEY, spec) is sandbox
        assert path in sandbox.directories
        await sandbox.reset(timeout=1)
        assert path in sandbox.directories

    asyncio.run(scenario())


@pytest.mark.parametrize("path", ["//workspace", "//workspace/nested", "///workspace"])
def test_slash_rooted_base_is_visible_to_file_methods_after_acquire_and_repair(path):
    async def scenario():
        backend = InProcessSandboxBackend()
        spec = SandboxSpec(kind="posix", work_dir=path)
        sandbox = await backend.acquire(_KEY, spec)
        for _ in range(2):
            entry = await sandbox.stat_file(path, working_directory=path)
            assert entry is not None and entry.kind is EntryKind.DIRECTORY
            await sandbox.write_file("marker", "keep", working_directory=path)
            assert await backend.acquire(_KEY, spec) is sandbox
            assert await sandbox.read_file("marker", working_directory=path, max_bytes=4) == b"keep"
            sandbox.contents.clear()
            sandbox.directories.clear()
            assert await sandbox.stat_file(path, working_directory=path) is None
            assert await backend.acquire(_KEY, spec) is sandbox
        await sandbox.reset(timeout=1)
        entry = await sandbox.stat_file(path, working_directory=path)
        assert entry is not None and entry.kind is EntryKind.DIRECTORY

    asyncio.run(scenario())


@pytest.mark.parametrize("kind", [EntryKind.SYMLINK, EntryKind.FILE])
def test_native_work_dir_refuses_an_obstructed_parent(kind):
    sandbox = InProcessSandbox(
        seed_files={r"D:\agent": kind if kind is EntryKind.SYMLINK else b"keep"}
    )
    before = sandbox._snapshot()
    spec = SandboxSpec(kind="native", work_dir=r"D:\agent\work")
    with pytest.raises(ValueError if kind is EntryKind.SYMLINK else NotADirectoryError):
        asyncio.run(InProcessSandboxBackend(sandbox).acquire(_KEY, spec))
    assert sandbox._snapshot() == before


def test_the_backend_resolves_its_own_path_grammar():
    resolved: list[str] = []
    inspected: list[str] = []
    created: list[tuple[str, ...]] = []

    def resolve(path):
        resolved.append(path)
        return ("volume", "volume|agent", "volume|agent|work")

    async def stat(path):
        inspected.append(path)
        return (
            SandboxEntry(path=path, kind=EntryKind.DIRECTORY, size_bytes=None)
            if path == "volume"
            else None
        )

    async def create(directories):
        created.append(directories)

    asyncio.run(
        ensure_guest_work_dir(
            SandboxSpec(kind="native", work_dir="volume|agent|work"), stat, create, resolve=resolve
        )
    )
    assert resolved == ["volume|agent|work"]
    assert inspected == ["volume", "volume|agent"]
    assert created == [("volume|agent", "volume|agent|work")]


@pytest.mark.parametrize(
    "capability",
    [
        Capability.EXEC,
        Capability.FILES_IN,
        Capability.FILES_OUT,
        Capability.FILES_LIST,
        Capability.FILES_DELETE,
    ],
)
def test_acquire_and_warm_repair_prepare_only_the_base(capability):
    async def scenario():
        backend = InProcessSandboxBackend()
        spec = SandboxSpec(kind="test", requires=frozenset({capability}))
        sandbox = await backend.acquire(_KEY, spec)
        assert spec.work_dir in sandbox.directories
        assert sandbox.contents == {}
        assert sandbox.commands == []
        assert spec.work_dir + "/call" not in sandbox.directories
        sandbox.directories.remove(spec.work_dir)
        assert await backend.acquire(_KEY, spec) is sandbox
        assert spec.work_dir in sandbox.directories
        await sandbox.reset(timeout=1)
        assert spec.work_dir in sandbox.directories

    asyncio.run(scenario())


@pytest.mark.parametrize("requires", [frozenset(), frozenset({Capability.RUN_CODE})])
def test_runtime_only_acquire_does_not_interpret_the_base(requires):
    backend = InProcessSandboxBackend()
    sandbox = asyncio.run(
        backend.acquire(
            _KEY, SandboxSpec(kind="runtime", work_dir="runtime-owned", requires=requires)
        )
    )
    assert sandbox.directories == set()


@pytest.mark.parametrize("path", ["/maf-sandbox", "/maf-sandbox/work"])
@pytest.mark.parametrize("kind", ["link", "file"])
def test_acquire_refuses_obstructed_paths_without_modifying_the_store(path, kind):
    sandbox = InProcessSandbox()
    if kind == "link":
        sandbox.symlinks.add(path)
    else:
        sandbox.contents[path] = b"keep"
    before = sandbox._snapshot()
    baseline = tuple(part.copy() for part in sandbox._baseline)
    backend = InProcessSandboxBackend(sandbox=sandbox)
    with pytest.raises(ValueError if kind == "link" else NotADirectoryError):
        asyncio.run(backend.acquire(_KEY, SandboxSpec(kind="test")))
    assert sandbox._snapshot() == before
    assert sandbox._baseline == baseline


@pytest.mark.parametrize("path", ["relative", "", "/bad\0path", "/bad\\path"])
def test_filesystem_acquire_refuses_invalid_bases(path):
    with pytest.raises(ValueError):
        asyncio.run(
            InProcessSandboxBackend().acquire(_KEY, SandboxSpec(kind="test", work_dir=path))
        )


def test_an_existing_base_keeps_contents_and_creates_no_child():
    sandbox = InProcessSandbox(seed_files={"/maf-sandbox/work/config": b"keep"})
    before = dict(sandbox.contents)
    asyncio.run(InProcessSandboxBackend(sandbox=sandbox).acquire(_KEY, SandboxSpec(kind="test")))
    assert sandbox.contents == before
    assert sandbox.directories == {"/maf-sandbox", "/maf-sandbox/work"}
    assert not sandbox.changed_paths()


@pytest.mark.parametrize("existing", ["explicit", "implicit", "parent"])
def test_preparing_a_warm_base_retains_only_its_directories_on_reset(existing):
    async def scenario():
        sandbox = InProcessSandbox(seed_files={"/seed.txt": b"original"})
        backend = InProcessSandboxBackend(sandbox)
        runtime = SandboxSpec(kind="test", work_dir="/session/work", requires=frozenset())
        assert await backend.acquire(_KEY, runtime) is sandbox
        if existing == "explicit":
            sandbox.directories.update({"/session", "/session/work"})
        elif existing == "implicit":
            sandbox.contents["/session/work/marker"] = b"guest"
        else:
            sandbox.directories.add("/session")
        sandbox.directories.add("/other")
        sandbox.contents["/other/marker"] = b"guest"
        before_contents = dict(sandbox.contents)

        spec = SandboxSpec(
            kind="test", work_dir=runtime.work_dir, requires=frozenset({Capability.EXEC})
        )
        assert await backend.acquire(_KEY, spec) is sandbox
        assert sandbox.contents == before_contents
        prepared_are_clean = not {"/session", spec.work_dir}.intersection(sandbox.changed_paths())
        await sandbox.reset(timeout=1)
        assert spec.work_dir is not None
        entry = await sandbox.stat_file(spec.work_dir, working_directory=spec.work_dir)
        assert entry is not None and entry.kind is EntryKind.DIRECTORY
        assert sandbox.directories == {"/session", spec.work_dir}
        assert sandbox.contents == {"/seed.txt": b"original"}
        assert prepared_are_clean

    asyncio.run(scenario())


@pytest.mark.parametrize("path", ["/", "//", "///"])
def test_posix_root_base_exists_through_acquire_and_reset(path):
    async def scenario():
        backend = InProcessSandboxBackend()
        spec = SandboxSpec(kind="root", work_dir=path)
        sandbox = await backend.acquire(_KEY, spec)
        for _ in range(2):
            entry = await sandbox.stat_file(path, working_directory=path)
            assert entry is not None and entry.kind is EntryKind.DIRECTORY
            with pytest.raises(IsADirectoryError):
                await sandbox.read_file(path, working_directory=path, max_bytes=1)
            assert sandbox.directories == set()
            assert sandbox.contents == {}
            assert await backend.acquire(_KEY, spec) is sandbox
            await sandbox.reset(timeout=1)

    asyncio.run(scenario())
