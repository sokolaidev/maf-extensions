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
    SandboxEntry,
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

from maf_sandbox_hyperlight import (
    HyperlightSandboxBackend,
    HyperlightSandboxConfig,
    _backend,
    _files,
)
from maf_sandbox_hyperlight._files import OutputDirectory

KEY = SandboxKey("files-test", "thread", "agent")
SPEC = SandboxSpec(
    kind="python",
    work_dir="/output",
    requires=frozenset({Capability.RUN_CODE, Capability.FILES_OUT, Capability.FILES_LIST}),
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


@pytest.mark.parametrize("cwd", [".", "/output"])
def test_listing_is_flat_complete_and_reports_binary_sizes(output, cwd):
    assert output.list_dir(".", cwd) == ()
    (output.path / "zéro.bin").write_bytes(bytes(range(256)))
    (output.path / "empty").touch()
    (output.path / "directory").mkdir()
    assert output.list_dir(".", cwd) == (
        SandboxEntry("directory", EntryKind.DIRECTORY, None),
        SandboxEntry("empty", EntryKind.FILE, 0),
        SandboxEntry("zéro.bin", EntryKind.FILE, 256),
    )
    with pytest.raises(ValueError, match="flat"):
        output.list_dir("directory", cwd)
    with pytest.raises(NotADirectoryError):
        output.list_dir("empty", cwd)
    with pytest.raises(FileNotFoundError):
        output.list_dir("missing", cwd)


def test_listing_reports_hardlinks_without_readable_sizes(output, tmp_path):
    target = tmp_path / "secret"
    target.write_bytes(b"outside")
    os.link(target, output.path / "hard")
    assert output.list_dir(".", ".") == (SandboxEntry("hard", EntryKind.OTHER, None),)


def test_listing_entry_cap_counts_directories_and_refuses_without_partial_success(output):
    for i in range(_files.MAX_LIST_ENTRIES):
        (output.path / f"d{i:02}").mkdir()
    assert len(output.list_dir(".", ".")) == _files.MAX_LIST_ENTRIES
    (output.path / "overflow").touch()
    with pytest.raises(SandboxTransferCapExceeded, match="metadata"):
        output.list_dir(".", ".")


def test_listing_name_budget_uses_encoded_bytes(output, monkeypatch):
    (output.path / "é").touch()
    monkeypatch.setattr(_files, "MAX_LIST_NAME_BYTES", 2)
    assert output.list_dir(".", ".")[0].path == "é"
    monkeypatch.setattr(_files, "MAX_LIST_NAME_BYTES", 1)
    with pytest.raises(SandboxTransferCapExceeded):
        output.list_dir(".", ".")


@pytest.mark.parametrize("fault", ["iterator", "metadata", "identity", "duplicate", "unsafe"])
def test_listing_inspection_failure_closes_enumeration_and_root(output, monkeypatch, fault):
    (output.path / "result").write_bytes(b"inside")
    original = output._names
    closed = []
    descriptors = []

    def names(root):
        descriptors.append(root)
        try:
            records = list(original(root))
            yield from records
            if fault == "iterator":
                raise OSError("inspection failure")
            if fault == "duplicate":
                yield from records
            if fault == "unsafe":
                yield "../outside", 0
        finally:
            closed.append(True)

    if fault in {"metadata", "identity"}:

        def inspect(root, name):
            if fault == "metadata":
                raise OSError("inspection failure")
            return os.fstat(root)

        monkeypatch.setattr(output, "_stat_child", inspect)
    monkeypatch.setattr(output, "_names", names)
    with pytest.raises((OSError, ValueError)):
        output.list_dir(".", ".")
    assert closed == [True]
    with pytest.raises(OSError):
        os.fstat(descriptors[0])


def test_listing_rechecks_root_after_path_validation(output, monkeypatch):
    (output.path / "inside").touch()
    moved = output.path.with_name(output.path.name + "-original")
    original = output._path

    def replace_root(path, cwd):
        result = original(path, cwd)
        output.path.rename(moved)
        output.path.mkdir()
        (output.path / "outside").touch()
        return result

    monkeypatch.setattr(output, "_path", replace_root)
    try:
        with pytest.raises(ValueError, match="replaced"):
            output.list_dir(".", ".")
    finally:
        (output.path / "outside").unlink()
        output.path.rmdir()
        moved.rename(output.path)


def test_listing_root_cannot_redirect_after_open(output, monkeypatch):
    (output.path / "inside").touch()
    moved = output.path.with_name(output.path.name + "-original")
    original = output._names

    def names(root):
        if sys.platform == "win32":
            with pytest.raises(OSError):
                output.path.rename(moved)
        else:
            output.path.rename(moved)
            output.path.mkdir()
            (output.path / "outside").touch()
        yield from original(root)

    monkeypatch.setattr(output, "_names", names)
    try:
        assert output.list_dir(".", ".") == (SandboxEntry("inside", EntryKind.FILE, 0),)
    finally:
        if moved.exists():
            (output.path / "outside").unlink()
            output.path.rmdir()
            moved.rename(output.path)


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
    with pytest.raises(ValueError):
        output.list_dir(path, cwd)


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
    assert output.list_dir(".", ".") == (SandboxEntry("link", EntryKind.SYMLINK, None),)
    with pytest.raises(ValueError, match="link"):
        output.list_dir("link", ".")
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


@pytest.mark.parametrize("kind", ["symlink", "hardlink"])
def test_file_replaced_by_link_after_validation_is_never_read(output, tmp_path, monkeypatch, kind):
    secret = tmp_path / "secret"
    secret.write_bytes(b"outside")
    link = output.path / "replacement"
    if kind == "symlink":
        _symlink(link, secret)
    else:
        os.link(secret, link)
    target = output.path / "result"
    target.write_bytes(b"inside")
    original = output.stat_file

    def replace_after_validation(path, working_directory):
        resolved = original(path, working_directory)
        target.unlink()
        link.rename(target)
        return resolved

    monkeypatch.setattr(output, "stat_file", replace_after_validation)
    with pytest.raises(OSError):
        output.read_file("result", ".", 100)


def test_root_replaced_after_path_validation_cannot_redirect_read(output, tmp_path, monkeypatch):
    (output.path / "result").write_bytes(b"inside")
    (tmp_path / "result").write_bytes(b"outside")
    moved = output.path.with_name(output.path.name + "-original")
    original = output._path
    calls = 0

    def racing_path(path, working_directory):
        nonlocal calls
        resolved = original(path, working_directory)
        calls += 1
        if calls == 2:
            output.path.rename(moved)
            if os.name == "nt":
                subprocess.run(
                    ["cmd", "/c", "mklink", "/J", str(output.path), str(tmp_path)], check=True
                )
            else:
                output.path.symlink_to(tmp_path, target_is_directory=True)
        return resolved

    monkeypatch.setattr(output, "_path", racing_path)
    try:
        with pytest.raises((OSError, ValueError)):
            output.read_file("result", ".", 100)
    finally:
        if moved.exists():
            if output.path.is_junction():
                output.path.rmdir()
            elif output.path.is_symlink():
                output.path.unlink()
            moved.rename(output.path)
    assert (tmp_path / "result").read_bytes() == b"outside"


@pytest.mark.skipif(os.name != "nt", reason="Windows sharing guarantees")
def test_windows_reader_pins_root_and_file_until_closed(output):
    target = output.path / "result"
    target.write_bytes(b"inside")
    moved = output.path.with_name(output.path.name + "-moved")
    with output._reader(target) as stream:
        with pytest.raises(OSError):
            output.path.rename(moved)
        with pytest.raises(OSError):
            target.write_bytes(b"changed")
        with pytest.raises(OSError):
            target.unlink()
        assert stream.read() == b"inside"
    target.write_bytes(b"changed")
    output.path.rename(moved)
    moved.rename(output.path)


@pytest.mark.skipif(os.name != "nt", reason="Windows handle ownership")
@pytest.mark.parametrize("failure", ["descriptor", "stream"])
def test_windows_reader_setup_failure_closes_handles(output, monkeypatch, failure):
    if sys.platform != "win32":
        pytest.skip("Windows handle ownership")
    import msvcrt

    from maf_sandbox_hyperlight._windows_files import open_no_follow

    target = output.path / "result"
    target.write_bytes(b"inside")

    def refuse(*args, **kwargs):
        raise OSError("descriptor allocation failed")

    if failure == "descriptor":
        monkeypatch.setattr(msvcrt, "open_osfhandle", refuse)
    else:
        monkeypatch.setattr(os, "fdopen", refuse)
    with pytest.raises(OSError, match="allocation"):
        if failure == "descriptor":
            open_no_follow(target)
        else:
            output.read_file("result", ".", 100)
    target.unlink()
    moved = output.path.with_name(output.path.name + "-moved")
    output.path.rename(moved)
    moved.rename(output.path)


@pytest.mark.skipif(os.name != "nt", reason="Windows junction fixture")
def test_junctions_cannot_redirect_collection_or_cleanup(output, tmp_path):
    secret = tmp_path / "secret"
    secret.write_bytes(b"secret")
    junction = output.path / "junction"
    subprocess.run(["cmd", "/c", "mklink", "/J", str(junction), str(tmp_path)], check=True)
    assert output.stat_file("junction", ".").kind is EntryKind.SYMLINK
    assert output.list_dir(".", ".") == (SandboxEntry("junction", EntryKind.SYMLINK, None),)
    for path, cwd in (("junction", "."), ("junction/secret", "."), (".", "/output/junction")):
        with pytest.raises(ValueError, match="link"):
            output.list_dir(path, cwd)
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
    assert output.list_dir(".", ".") == (SandboxEntry("pipe", EntryKind.OTHER, None),)
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
    assert backend.declarations.capabilities == {
        Capability.RUN_CODE,
        Capability.SNAPSHOT,
        Capability.FILES_OUT,
        Capability.FILES_LIST,
    }

    async def check():
        with pytest.raises(RuntimeError, match="call_admission"):
            await backend.acquire(KEY, SPEC)
        async with backend.call_admission(KEY, SPEC, owner="one", timeout=1):
            sandbox = await backend.acquire(KEY, SPEC)
            directory = sandbox.outputs.path
            assert (await sandbox.stat_file(".", working_directory=".")).kind is EntryKind.DIRECTORY
            await sandbox.run_code("write", timeout=1)
            assert await sandbox.list_dir(".", working_directory=".") == (
                SandboxEntry("result.bin", EntryKind.FILE, 2),
            )
            assert (
                await sandbox.read_file("result.bin", working_directory=".", max_bytes=2)
                == b"\x00\xff"
            )
            identity = sandbox.instance_id
            await sandbox.reset(timeout=1)
            assert sandbox.instance_id != identity
            assert list(directory.iterdir()) == []
            assert await sandbox.list_dir(".", working_directory=".") == ()
        with pytest.raises(RuntimeError):
            await sandbox.read_file("result.bin", working_directory=".", max_bytes=2)
        with pytest.raises(RuntimeError):
            await sandbox.list_dir(".", working_directory=".")
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


@pytest.mark.parametrize("reverse", [False, True])
def test_all_admitted_keys_remain_authorized_until_their_own_release(backend, reverse):
    async def check():
        router = SandboxRouter([backend], min_cleanup=Cleanup.RESET)
        keys = [KEY, replace(KEY, thread_id="other")]
        held = {}
        try:
            for key in keys:
                admission = await router.enter_call(key, SPEC, owner="call")
                held[key] = admission
            sandboxes = {key: await router.acquire(key, SPEC, _admission=held[key]) for key in keys}
            for sandbox in sandboxes.values():
                await sandbox.run_code("write", timeout=1)
            for key in reversed(keys) if reverse else keys:
                sandbox = sandboxes[key]
                assert (
                    await sandbox.read_file("result.bin", working_directory=".", max_bytes=2)
                    == b"\x00\xff"
                )
                assert (
                    await router.finish_call(
                        key, SPEC, admission=held[key], sandbox=sandbox, owner="call"
                    )
                    is None
                )
                del held[key]
        finally:
            for key in reversed(held):
                await router.release_call(key, SPEC.kind, owner="call")

    asyncio.run(check())


@pytest.mark.parametrize("cleanup", [Cleanup.RESET, Cleanup.DISPOSE])
@pytest.mark.parametrize("other_loop", [False, True])
def test_cleanup_transfers_authority_to_a_fresh_context(backend, cleanup, other_loop):
    async def check():
        router = SandboxRouter([backend], min_cleanup=cleanup)
        admission = await router.enter_call(KEY, SPEC, owner="call")
        sandbox = cast(
            _backend._HyperlightSandbox, await router.acquire(KEY, SPEC, _admission=admission)
        )
        assert sandbox.outputs is not None
        directory = sandbox.outputs.path
        await sandbox.run_code("write", timeout=1)

        async def finish():
            # A fresh task cannot touch files until the router activates its retained authority.
            with pytest.raises(RuntimeError, match="call_admission"):
                await sandbox.read_file("result.bin", working_directory=".", max_bytes=2)
            return await router.finish_call(
                KEY, SPEC, admission=admission, sandbox=sandbox, owner="call"
            )

        if other_loop:
            with ThreadPoolExecutor(max_workers=1) as executor:
                failure = await asyncio.wrap_future(executor.submit(asyncio.run, finish()))
        else:
            failure = await asyncio.create_task(finish(), context=contextvars.Context())
        assert failure is None
        if cleanup is Cleanup.RESET:
            assert directory.exists() and not list(directory.iterdir())
        else:
            assert not directory.exists()
        with pytest.raises(RuntimeError, match="call_admission"):
            await sandbox.stat_file(".", working_directory=".")
        async with backend.call_admission(KEY, SPEC, owner="next", timeout=1):
            await backend.acquire(KEY, SPEC)

    asyncio.run(check())


def test_cancelled_cleanup_releases_transferred_authority_and_retires_worker(backend, monkeypatch):
    async def check():
        router = SandboxRouter([backend], min_cleanup=Cleanup.RESET)
        admission = await router.enter_call(KEY, SPEC, owner="call")
        sandbox = cast(
            _backend._HyperlightSandbox, await router.acquire(KEY, SPEC, _admission=admission)
        )
        await sandbox.run_code("write", timeout=1)
        started, stopped = threading.Event(), threading.Event()
        request, close = sandbox.worker.request, sandbox.worker.close

        def resetting(message, *, deadline):
            if message["op"] == "reset":
                started.set()
                assert stopped.wait(5)
            return request(message, deadline=deadline)

        def stopping():
            close()
            stopped.set()

        monkeypatch.setattr(sandbox.worker, "request", resetting)
        monkeypatch.setattr(sandbox.worker, "close", stopping)
        task = asyncio.create_task(
            router.finish_call(KEY, SPEC, admission=admission, sandbox=sandbox, owner="call"),
            context=contextvars.Context(),
        )
        assert await asyncio.to_thread(started.wait, 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not sandbox.alive
        assert sandbox.outputs is not None and not sandbox.outputs.path.exists()
        async with backend.call_admission(KEY, SPEC, owner="next", timeout=1):
            fresh = await backend.acquire(KEY, SPEC)
            assert fresh.instance_id != sandbox.instance_id

    asyncio.run(check())


def test_expired_context_cannot_reuse_authority_when_owner_name_is_reused(backend):
    async def check():
        queued, entered, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def next_call():
            queued.set()
            async with backend.call_admission(KEY, SPEC, owner="same", timeout=2):
                entered.set()
                await release.wait()

        async with backend.call_admission(KEY, SPEC, owner="same", timeout=1):
            sandbox = await backend.acquire(KEY, SPEC)
            old_context = contextvars.copy_context()
            next_task = asyncio.create_task(next_call(), context=contextvars.Context())
            await queued.wait()
        await asyncio.wait_for(entered.wait(), 2)
        try:
            with pytest.raises(RuntimeError, match="call_admission"):
                await asyncio.create_task(
                    sandbox.stat_file(".", working_directory="."), context=old_context
                )
        finally:
            release.set()
            await next_task

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
            sandbox = await second.acquire(KEY, SPEC)
            assert await sandbox.list_dir(".", working_directory=".") == (
                SandboxEntry("result.bin", EntryKind.FILE, 2),
            )
            return sandbox

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


@pytest.mark.parametrize("operation", ["run", "reset", "dispose"])
def test_listing_holds_execution_and_storage_cleanup_across_loops(backend, monkeypatch, operation):
    async def check():
        async with backend.call_admission(KEY, SPEC, owner="listing", timeout=5):
            sandbox = await backend.acquire(KEY, SPEC)
            await sandbox.run_code("write", timeout=1)
            scanning, release = threading.Event(), threading.Event()
            names = sandbox.outputs._names

            def blocked_names(root):
                scanning.set()
                assert release.wait(5)
                yield from names(root)

            monkeypatch.setattr(sandbox.outputs, "_names", blocked_names)
            request, close = sandbox.worker.request, sandbox.outputs.close

            def checked_request(message, *, deadline):
                assert release.is_set(), "execution/reset interleaved with listing"
                return request(message, deadline=deadline)

            def checked_close():
                assert release.is_set(), "storage deletion interleaved with listing"
                return close()

            monkeypatch.setattr(sandbox.worker, "request", checked_request)
            monkeypatch.setattr(sandbox.outputs, "close", checked_close)
            listing = asyncio.create_task(
                asyncio.to_thread(lambda: asyncio.run(sandbox.list_dir(".", working_directory=".")))
            )
            pending = None
            try:
                assert await asyncio.to_thread(scanning.wait, 2)
                action = (
                    sandbox.run_code("write", timeout=3)
                    if operation == "run"
                    else sandbox.reset(timeout=3)
                    if operation == "reset"
                    else backend.dispose(KEY)
                )
                pending = asyncio.create_task(action)
                await asyncio.sleep(0)
                assert not pending.done()
            finally:
                release.set()
                entries = await listing
                if pending is not None:
                    await pending
            assert entries == (SandboxEntry("result.bin", EntryKind.FILE, 2),)

    asyncio.run(check())


def test_cancelled_listing_waiter_never_enumerates(backend, monkeypatch):
    async def check():
        async with backend.call_admission(KEY, SPEC, owner="listing", timeout=1):
            sandbox = await backend.acquire(KEY, SPEC)

            def unexpected(*args):
                pytest.fail("a cancelled listing entered the file plane")

            monkeypatch.setattr(sandbox.outputs, "list_dir", unexpected)
            sandbox._gate.acquire()
            try:
                task = asyncio.create_task(sandbox.list_dir(".", working_directory="."))
                await asyncio.sleep(0)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            finally:
                sandbox._gate.release()

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


@pytest.mark.parametrize("work_dir", [None, "/output"])
def test_output_conformance_uses_exec_free_fixture(backend, tmp_path, work_dir):
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
            subject.sandbox = await backend.acquire(KEY, replace(SPEC, work_dir=work_dir))
            await assert_storage_base_conformance(subject.sandbox, subject.capabilities)
            results = await assert_files_out_conformance(subject, flat_files=True)
            assert len(results) == 12 and all(result.passed for result in results)
            reach = await assert_reach_conformance(subject)
            assert all(result.skipped for result in reach)

    asyncio.run(check())
