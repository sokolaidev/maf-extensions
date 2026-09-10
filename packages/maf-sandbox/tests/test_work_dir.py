"""Acquisition prepares the requested base without requiring a guest command."""

from __future__ import annotations

import asyncio

import pytest

from maf_sandbox import Capability, EntryKind, SandboxEntry, SandboxKey, SandboxSpec
from maf_sandbox.paths import ensure_guest_work_dir
from maf_sandbox.testing import InProcessSandbox, InProcessSandboxBackend

_KEY = SandboxKey("work-dir", "thread", "agent")


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


@pytest.mark.parametrize("path", ["C:/", "D:\\", r"\\server\share", "\\\\server\\share\\"])
def test_native_root_base_is_intrinsic_to_the_store(path):
    async def scenario():
        backend = InProcessSandboxBackend()
        spec = SandboxSpec(kind="root", work_dir=path)
        sandbox = await backend.acquire(_KEY, spec)
        for _ in range(2):
            entry = await sandbox._stat_unconfined(path)
            assert entry is not None and entry.kind is EntryKind.DIRECTORY
            assert sandbox.directories == set()
            assert sandbox.contents == {}
            assert await backend.acquire(_KEY, spec) is sandbox
            await sandbox.reset(timeout=1)

    asyncio.run(scenario())


@pytest.mark.parametrize("path", ["C:", "C:work", "//workspace", r"\\server\share\missing"])
def test_non_root_paths_are_not_intrinsic_to_the_store(path):
    assert asyncio.run(InProcessSandbox()._stat_unconfined(path)) is None


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
    backend = InProcessSandboxBackend(sandbox=sandbox)
    with pytest.raises(ValueError if kind == "link" else NotADirectoryError):
        asyncio.run(backend.acquire(_KEY, SandboxSpec(kind="test")))
    assert sandbox._snapshot() == before


@pytest.mark.parametrize("path", ["relative", "", "/bad\0path", "/bad\\path"])
def test_filesystem_acquire_refuses_invalid_bases(path):
    with pytest.raises(ValueError):
        asyncio.run(
            InProcessSandboxBackend().acquire(_KEY, SandboxSpec(kind="test", work_dir=path))
        )


def test_an_existing_base_keeps_contents_and_creates_no_child():
    sandbox = InProcessSandbox(seed_files={"/maf-sandbox/work/config": b"keep"})
    before = sandbox._snapshot()
    asyncio.run(InProcessSandboxBackend(sandbox=sandbox).acquire(_KEY, SandboxSpec(kind="test")))
    assert sandbox._snapshot() == before
