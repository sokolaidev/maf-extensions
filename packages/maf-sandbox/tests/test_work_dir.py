"""Acquisition prepares the requested base without requiring a guest command."""

from __future__ import annotations

import asyncio

import pytest

from maf_sandbox import Capability, SandboxKey, SandboxSpec
from maf_sandbox.testing import InProcessSandbox, InProcessSandboxBackend

_KEY = SandboxKey("work-dir", "thread", "agent")


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
