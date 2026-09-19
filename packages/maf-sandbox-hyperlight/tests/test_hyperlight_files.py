"""File authority, call ownership and cleanup without evaluating inspection code in a guest."""

from __future__ import annotations

import asyncio
import contextvars
import os
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest
from maf_sandbox import (
    Capability,
    Cleanup,
    EntryKind,
    SandboxKey,
    SandboxRouter,
    SandboxSpec,
    SandboxTransferCapExceeded,
)
from maf_sandbox.conformance import (
    assert_files_out_conformance,
    assert_reach_conformance,
    assert_storage_base_conformance,
)

from maf_sandbox_hyperlight import HyperlightSandboxBackend, HyperlightSandboxConfig, _backend
from maf_sandbox_hyperlight._files import OutputDirectory

KEY = SandboxKey("files-test", "thread", "agent")
SPEC = SandboxSpec(
    kind="python",
    work_dir="/output",
    requires=frozenset({Capability.RUN_CODE, Capability.FILES_OUT}),
)


@pytest.fixture
def output():
    directory = OutputDirectory()
    yield directory
    directory.close()


def test_binary_size_refusal_empty_and_missing(output):
    data = bytes(range(256)) + b"\xff\x00"
    (output.path / "result.bin").write_bytes(data)
    entry = output.stat_file("result.bin", ".")
    assert entry.kind is EntryKind.FILE and entry.size_bytes == len(data)
    assert output.read_file("result.bin", "/output", len(data)) == data
    with pytest.raises(SandboxTransferCapExceeded):
        output.read_file("result.bin", ".", len(data) - 1)
    (output.path / "empty").touch()
    assert output.read_file("empty", ".", 0) == b""
    assert output.stat_file("missing", ".") is None
    with pytest.raises(FileNotFoundError):
        output.read_file("missing", ".", 10)


@pytest.mark.parametrize(
    "path,cwd",
    [
        ("../secret", "."),
        ("file", "../output"),
        ("/output/file", "."),
        ("C:/secret", "."),
        ("file:stream", "."),
        ("a/../file", "."),
        ("file", "/output-other"),
        ("a\\file", "."),
        ("NUL.txt", "."),
        ("file.", "."),
        ("file\0", "."),
        ("file ", "."),
    ],
)
def test_unsafe_names_are_refused(output, path, cwd):
    with pytest.raises(ValueError):
        output.stat_file(path, cwd)
    with pytest.raises(ValueError):
        output.read_file(path, cwd, 10)


def test_nested_host_file_is_not_mistaken_for_guest_reach(output):
    (output.path / "nested").mkdir()
    (output.path / "nested" / "secret").write_bytes(b"host-only")
    with pytest.raises(ValueError, match="flat"):
        output.read_file("nested/secret", ".", 100)
    with pytest.raises(ValueError):
        output.validate()


def _symlink(path, target, *, directory=False):
    try:
        path.symlink_to(target, target_is_directory=directory)
    except OSError as error:
        pytest.skip(f"host cannot plant symlinks: {error}")


def test_links_are_classified_but_never_read(output, tmp_path):
    secret = tmp_path / "secret"
    secret.write_bytes(b"secret")
    _symlink(output.path / "link", secret)
    assert output.stat_file("link", ".").kind is EntryKind.SYMLINK
    with pytest.raises(OSError):
        output.read_file("link", ".", 100)
    with pytest.raises(ValueError):
        output.validate()
    _symlink(output.path / "parent", tmp_path, directory=True)
    with pytest.raises(ValueError, match="link"):
        output.stat_file("parent/secret", ".")
    with pytest.raises(ValueError, match="link"):
        output.stat_file(".", "/output/parent")
    output.close()
    assert secret.read_bytes() == b"secret"


def test_hardlinks_are_refused(output, tmp_path):
    secret = tmp_path / "secret"
    secret.write_bytes(b"secret")
    os.link(secret, output.path / "hard")
    assert output.stat_file("hard", ".").kind is EntryKind.OTHER
    with pytest.raises(OSError):
        output.read_file("hard", ".", 100)
    with pytest.raises(ValueError):
        output.validate()


@pytest.mark.skipif(os.name != "nt", reason="Windows junction fixture")
def test_junctions_cannot_redirect_collection_or_cleanup(output, tmp_path):
    secret = tmp_path / "secret"
    secret.write_bytes(b"secret")
    junction = output.path / "junction"
    subprocess.run(["cmd", "/c", "mklink", "/J", str(junction), str(tmp_path)], check=True)
    assert output.stat_file("junction", ".").kind is EntryKind.SYMLINK
    with pytest.raises(OSError):
        output.read_file("junction", ".", 100)
    with pytest.raises(ValueError, match="link"):
        output.stat_file("junction/secret", ".")
    with pytest.raises(ValueError, match="link"):
        output.stat_file(".", "/output/junction")
    with pytest.raises(ValueError):
        output.validate()
    output.close()
    assert secret.read_bytes() == b"secret"


@pytest.mark.skipif(os.name == "nt", reason="POSIX FIFO fixture")
def test_fifo_is_never_opened(output):
    if sys.platform == "win32":
        pytest.skip("POSIX FIFO fixture")
    os.mkfifo(output.path / "pipe")
    assert output.stat_file("pipe", ".").kind is EntryKind.OTHER
    with pytest.raises(OSError):
        output.read_file("pipe", ".", 100)


class FileWorker:
    def __init__(self, config):
        self.alive = True
        self.directory = None

    def request(self, message, *, deadline):
        if message["op"] == "init":
            self.directory = Path(message["output_dir"])
        elif message["op"] == "run":
            assert self.directory is not None
            (self.directory / "result.bin").write_bytes(b"\x00\xff")
            return {"stdout": "done", "stderr": "", "exit_code": 0}
        return {"ok": True}

    def close(self):
        self.alive = False


@pytest.fixture
def backend(monkeypatch):
    monkeypatch.setattr(_backend, "Worker", FileWorker)
    monkeypatch.setattr(_backend, "check_host", lambda: None)
    backend = HyperlightSandboxBackend(HyperlightSandboxConfig(file_outputs=True))
    yield backend
    asyncio.run(backend.aclose())
    assert not backend._sandboxes


def test_direct_access_requires_call_scope_and_reset_cleans(backend):
    async def check():
        with pytest.raises(RuntimeError, match="call_admission"):
            await backend.acquire(KEY, SPEC)
        async with backend.call_admission(KEY, SPEC, owner="one", timeout=1):
            sandbox = await backend.acquire(KEY, SPEC)
            directory = sandbox.outputs.path
            assert (await sandbox.stat_file(".", working_directory=".")).kind is EntryKind.DIRECTORY
            await sandbox.run_code("write", timeout=1)
            assert (
                await sandbox.read_file("result.bin", working_directory=".", max_bytes=2)
                == b"\x00\xff"
            )
            identity = sandbox.instance_id
            await sandbox.reset(timeout=1)
            assert sandbox.instance_id != identity
            assert list(directory.iterdir()) == []
        with pytest.raises(RuntimeError):
            await sandbox.read_file("result.bin", working_directory=".", max_bytes=2)
        await backend.aclose()
        assert not directory.exists()

    asyncio.run(check())


def test_two_routers_hold_outputs_until_delivery_and_cleanup(backend):
    async def check():
        first = SandboxRouter([backend], min_cleanup=Cleanup.RESET)
        second = SandboxRouter([backend], min_cleanup=Cleanup.RESET)
        admission = await first.enter_call(KEY, SPEC, owner="first")
        sandbox = cast(
            _backend._HyperlightSandbox, await first.acquire(KEY, SPEC, _admission=admission)
        )
        await sandbox.run_code("write", timeout=1)
        entered = asyncio.Event()

        async def competitor():
            other = await second.enter_call(KEY, SPEC, owner="second")
            entered.set()
            fresh = await second.acquire(KEY, SPEC, _admission=other)
            assert await fresh.stat_file("result.bin", working_directory=".") is None
            await second.finish_call(KEY, SPEC, admission=other, sandbox=fresh, owner="second")

        task = asyncio.create_task(competitor(), context=contextvars.Context())
        await asyncio.sleep(0)
        assert not entered.is_set()
        assert (
            await sandbox.read_file("result.bin", working_directory=".", max_bytes=2) == b"\x00\xff"
        )
        # Delivery is still inside the first admission, even after all bytes were read.
        disposal = asyncio.create_task(backend.dispose(KEY), context=contextvars.Context())
        assert await disposal is not None
        assert sandbox.alive
        await first.finish_call(KEY, SPEC, admission=admission, sandbox=sandbox, owner="first")
        await asyncio.wait_for(task, 2)
        assert entered.is_set()

    asyncio.run(check())


def test_cancelled_waiter_does_not_release_owner_and_other_instances_can_run(backend):
    async def check():
        async with backend.call_admission(KEY, SPEC, owner="first", timeout=1):
            sandbox = await backend.acquire(KEY, SPEC)

            async def waiter():
                async with backend.call_admission(KEY, SPEC, owner="second", timeout=10):
                    pytest.fail("another call entered the same sandbox")

            waiting = asyncio.create_task(waiter(), context=contextvars.Context())
            await asyncio.sleep(0)
            waiting.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiting
            other_key = replace(KEY, thread_id="different")

            async def other():
                async with backend.call_admission(other_key, SPEC, owner="other", timeout=1):
                    other_sandbox = await backend.acquire(other_key, SPEC)
                    assert other_sandbox is not sandbox
                    await other_sandbox.run_code("write", timeout=1)

            await asyncio.create_task(other(), context=contextvars.Context())
            async with backend.call_admission(other_key, SPEC, owner="nested", timeout=1):
                await backend.acquire(other_key, SPEC)
            await sandbox.run_code("write", timeout=1)

    asyncio.run(check())


def test_call_ownership_crosses_event_loops_and_backend_objects(backend):
    from maf_sandbox import SandboxQueuedTimeout

    second = HyperlightSandboxBackend(HyperlightSandboxConfig(file_outputs=True))
    waiting = threading.Event()

    async def attempt():
        waiting.set()
        async with second.call_admission(KEY, SPEC, owner="second", timeout=0.1):
            return await second.acquire(KEY, SPEC)

    async def check():
        with ThreadPoolExecutor(max_workers=1) as executor:
            async with backend.call_admission(KEY, SPEC, owner="first", timeout=1):
                sandbox = await backend.acquire(KEY, SPEC)
                future = executor.submit(asyncio.run, attempt())
                assert await asyncio.to_thread(waiting.wait, 2)
                with pytest.raises(SandboxQueuedTimeout):
                    await asyncio.wrap_future(future)
                await sandbox.run_code("write", timeout=1)
            assert await asyncio.wrap_future(executor.submit(asyncio.run, attempt())) is sandbox

    asyncio.run(check())


def test_failed_worker_cleanup_retains_output_directory_for_retry(backend, monkeypatch):
    async def check():
        async with backend.call_admission(KEY, SPEC, owner="first", timeout=1):
            sandbox = await backend.acquire(KEY, SPEC)
            await sandbox.run_code("write", timeout=1)
            directory = sandbox.outputs.path
            original = sandbox.worker.close

            def fail():
                raise TimeoutError("worker still running")

            monkeypatch.setattr(sandbox.worker, "close", fail)
            assert await backend.dispose(KEY) is not None
            assert (directory / "result.bin").read_bytes() == b"\x00\xff"
            monkeypatch.setattr(sandbox.worker, "close", original)
            assert await backend.dispose(KEY) is None
            assert not directory.exists()

    asyncio.run(check())


def test_failed_file_reset_retires_instance_before_another_router_can_reuse(backend, monkeypatch):
    async def check():
        async with backend.call_admission(KEY, SPEC, owner="first", timeout=1):
            sandbox = await backend.acquire(KEY, SPEC)
            await sandbox.run_code("write", timeout=1)
            directory = sandbox.outputs.path

            def fail():
                raise OSError("cannot clear output files")

            monkeypatch.setattr(sandbox.outputs, "clear", fail)
            with pytest.raises(OSError, match="cannot clear"):
                await sandbox.reset(timeout=1)
            assert not sandbox.alive and not directory.exists()
        async with backend.call_admission(KEY, SPEC, owner="second", timeout=1):
            fresh = await backend.acquire(KEY, SPEC)
            assert fresh.instance_id != sandbox.instance_id
            assert await fresh.stat_file("result.bin", working_directory=".") is None

    asyncio.run(check())


def test_output_conformance_uses_exec_free_fixture(backend, tmp_path):
    class Subject:
        capabilities = backend.declarations.capabilities
        working_directory = "/output"
        sandbox: _backend._HyperlightSandbox

        def host(self, guest):
            if guest == "/output" or guest.startswith("/output/"):
                assert self.sandbox.outputs is not None
                return self.sandbox.outputs.path / guest.removeprefix("/output").lstrip("/")
            return tmp_path / guest.lstrip("/")

        async def plant_file(self, path, content):
            target = self.host(path)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)

        async def plant_symlink(self, path, target):
            _symlink(self.host(path), self.host(target), directory=self.host(target).is_dir())

        async def exists(self, path: str) -> bool:
            return os.path.lexists(self.host(path))

        async def plant_directory_the_guest_owns(self, path: str) -> bool:
            raise AssertionError("FILES_IN and deletion reach probes must be skipped")

        async def plant_file_the_guest_owns(self, path: str) -> bool:
            raise AssertionError("FILES_IN and deletion reach probes must be skipped")

        async def the_guest_can_write(self, path: str) -> bool:
            raise AssertionError("FILES_IN reach probes must be skipped")

        async def the_guest_can_delete(self, path: str) -> bool:
            raise AssertionError("deletion reach probes must be skipped")

    async def check():
        async with backend.call_admission(KEY, SPEC, owner="conformance", timeout=1):
            subject = Subject()
            subject.sandbox = await backend.acquire(KEY, SPEC)
            await assert_storage_base_conformance(subject.sandbox, subject.capabilities)
            results = await assert_files_out_conformance(subject, flat_files=True)
            assert sum(result.passed for result in results) == 8
            reach = await assert_reach_conformance(subject)
            assert all(result.skipped for result in reach)

    asyncio.run(check())
