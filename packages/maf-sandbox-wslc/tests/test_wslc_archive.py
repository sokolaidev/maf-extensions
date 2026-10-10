"""Archive metadata, bounded real pipes, admission and confinement without WSL."""

from __future__ import annotations

import asyncio
import base64
import io
import sys
import tarfile
import zlib
from dataclasses import replace

import pytest
from maf_sandbox import (
    Capability,
    EntryKind,
    Isolation,
    SandboxCapabilityNotSupported,
    SandboxKey,
    SandboxRouter,
    SandboxSpec,
    SandboxTransferCapExceeded,
)

from maf_sandbox_wslc import WslcSandboxBackend, WslcSandboxConfig
from maf_sandbox_wslc._archive import ArchiveResult, entry_prefix, read_archive_process
from maf_sandbox_wslc._backend import _WslcResult, _WslcSandbox


def _tar(
    data=b"binary\x00\xff\r\n",
    *,
    kind=tarfile.REGTYPE,
    name="out",
    format=tarfile.PAX_FORMAT,
    pax=None,
):
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w", format=format) as archive:
        entry = tarfile.TarInfo(name)
        entry.type = kind
        entry.size = len(data) if kind == tarfile.REGTYPE else 0
        entry.linkname = "inside" if kind in (tarfile.SYMTYPE, tarfile.LNKTYPE) else ""
        entry.pax_headers = pax or {}
        archive.addfile(entry, io.BytesIO(data))
    return stream.getvalue()


async def _process(payload, *, after="", before=""):
    script = (
        "import sys,time,base64,zlib\n"
        + before
        + "\n"
        + f"sys.stdout.buffer.write(zlib.decompress(base64.b64decode({base64.b64encode(zlib.compress(payload))!r})));sys.stdout.buffer.flush()\n"
        + after
    )
    return await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        script,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )


def _read(payload, *, cap=1024, after="", before="", timeout=5):
    async def scenario():
        process = await _process(payload, after=after, before=before)
        try:
            return await read_archive_process(process, max_bytes=cap, timeout=timeout)
        finally:
            assert process.returncode is not None

    return asyncio.run(scenario())


@pytest.mark.parametrize("data", [b"", bytes(range(256)) * 4])
def test_binary_and_empty_archives_preserve_bytes_and_exit(data):
    result = _read(_tar(data), cap=max(1, len(data)))
    assert result.data == data and result.returncode == 0
    assert result.entry.size == len(data)


@pytest.mark.parametrize("format", [tarfile.PAX_FORMAT, tarfile.GNU_FORMAT])
def test_long_metadata_is_parsed_before_the_body(format):
    payload = _tar(name="x" * 200, format=format)
    info, needed = entry_prefix(payload[:512])
    assert info is None and needed <= 65536
    result = _read(payload, cap=None)
    assert result.entry.name == "x" * 200
    assert result.data == b""


def test_effective_pax_size_is_the_cap():
    payload = _tar(b"", pax={"size": "2147483648"})
    with pytest.raises(SandboxTransferCapExceeded):
        _read(payload)


@pytest.mark.parametrize(
    "kind", [tarfile.SYMTYPE, tarfile.DIRTYPE, tarfile.FIFOTYPE, tarfile.LNKTYPE, tarfile.CHRTYPE]
)
def test_nonregular_entries_are_described_but_not_read(kind):
    payload = _tar(kind=kind)
    assert _read(payload, cap=None).entry.type == kind
    with pytest.raises(OSError, match="regular"):
        _read(payload, after="time.sleep(60)")


def test_header_only_stat_does_not_wait_for_or_consume_body():
    entry = tarfile.TarInfo("large")
    entry.size = 2**31
    result = _read(entry.tobuf(), cap=None, after="time.sleep(60)")
    assert result.entry.size == 2**31 and result.data == b""
    with pytest.raises(SandboxTransferCapExceeded):
        _read(entry.tobuf(), cap=12, after="time.sleep(60)")


@pytest.mark.parametrize(
    "payload",
    [
        b"bad",
        b"!" * 512,
        _tar()[:515],
        _tar()[:1024],
        _tar()[:1536],
        _tar() + b"unexpected",
        _tar() + b"\0" * 65536,
    ],
    ids=[
        "short",
        "bad-header",
        "short-body",
        "no-end",
        "short-end",
        "extra-data",
        "excessive-padding",
    ],
)
def test_malformed_truncated_and_excessive_archives_fail(payload):
    with pytest.raises(RuntimeError):
        _read(payload)


def test_second_entry_cannot_be_returned_as_part_of_an_artifact():
    first = _tar(b"a")[:1024]
    with pytest.raises(RuntimeError, match="extra entries"):
        _read(first + _tar(b"b"))


def test_partial_trailing_zero_block_is_refused():
    with pytest.raises(RuntimeError, match="partial trailing"):
        _read(_tar() + b"\0")


@pytest.mark.parametrize(
    "kind", [tarfile.XHDTYPE, tarfile.XGLTYPE, tarfile.GNUTYPE_LONGNAME, tarfile.GNUTYPE_LONGLINK]
)
def test_metadata_size_is_refused_before_allocating_or_reading_it(kind):
    entry = tarfile.TarInfo("metadata")
    entry.type = kind
    entry.size = 2**31
    with pytest.raises(RuntimeError, match="64 KiB"):
        _read(entry.tobuf(), after="time.sleep(60)")


@pytest.mark.parametrize(
    "record", [b"0 x=y\n", b"999 x=y\n", b"nonsense", b"11 size=-1\n", b"22 GNU.sparse.size=3\n"]
)
def test_malformed_and_sparse_pax_is_refused(record):
    entry = tarfile.TarInfo("pax")
    entry.type = tarfile.XHDTYPE
    entry.size = len(record)
    payload = entry.tobuf() + record.ljust(512, b"\0") + _tar()
    with pytest.raises(RuntimeError):
        _read(payload)


def test_stderr_flood_does_not_stall_and_is_bounded():
    result = _read(
        _tar(), before="sys.stderr.buffer.write(b'e' * 200000);sys.stderr.buffer.flush()"
    )
    assert result.returncode == 0 and len(result.stderr) == 65536


def test_eof_preserves_failure_even_after_a_complete_archive():
    result = _read(_tar(), after="sys.exit(7)")
    assert result.returncode == 7
    absent = _read(b"", before="sys.stderr.write('missing');sys.exit(9)")
    assert absent.returncode == 9 and absent.entry is None


@pytest.mark.parametrize("cancel", [False, True])
def test_timeout_and_cancellation_kill_and_reap_real_child(cancel):
    async def scenario():
        process = await _process(_tar()[:512], after="time.sleep(60)")
        task = asyncio.create_task(
            read_archive_process(process, max_bytes=1024, timeout=0.3 if not cancel else 30)
        )
        if cancel:
            await asyncio.sleep(0.1)
            task.cancel()
        with pytest.raises(asyncio.CancelledError if cancel else TimeoutError):
            await task
        assert process.returncode is not None

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "text,code,supported",
    [
        (b"wslc 3.0.2.0\r\nCopyright\r\n", 0, True),
        (b"wslc 3.0.3.0\n", 0, True),
        (b"wslc 3.0.1.0\n", 0, False),
        (b"wslc 2.9.13.0\n", 0, False),
        (b"wslc 3.0.2.0\n", 1, False),
        (b"wslc 3.0.2.0-dev\n", 0, False),
        (b"wslc 3.0.2.0\n" + b"x" * 4096, 0, False),
        (b"garbled", 0, False),
    ],
)
def test_factory_checks_version_before_advertising_files_out(monkeypatch, text, code, supported):
    calls = []

    async def run(self, *args, **kwargs):
        calls.append((args, kwargs))
        return _WslcResult(code, text, b"")

    monkeypatch.setattr(WslcSandboxBackend, "_wslc", run)
    backend = asyncio.run(WslcSandboxBackend.create(WslcSandboxConfig()))
    assert (Capability.FILES_OUT in backend.declarations.capabilities) == supported
    assert (
        not {Capability.FILES_LIST, Capability.FILES_DELETE, Capability.RECLAIM}
        & backend.declarations.capabilities
    )
    assert calls == [(("--version",), {"timeout": 5, "read_limit": 4096})]
    router = SandboxRouter([backend], min_isolation=Isolation.CONTAINER)
    spec = SandboxSpec(kind="output", requires=frozenset({Capability.FILES_OUT}))
    if not supported:
        with pytest.raises(SandboxCapabilityNotSupported):
            router.ensure_can_serve(spec)
    else:
        router.ensure_can_serve(spec)


def test_plain_constructor_and_downgrade_refuse_before_creating_workload(monkeypatch):
    async def scenario():
        backend = WslcSandboxBackend(WslcSandboxConfig())
        spec = SandboxSpec(kind="output", requires=frozenset({Capability.FILES_OUT}))
        key = SandboxKey("test", "thread", "agent")
        with pytest.raises(SandboxCapabilityNotSupported):
            await backend.acquire(key, spec)
        backend._declarations = replace(
            backend.declarations,
            capabilities=backend.declarations.capabilities | {Capability.FILES_OUT},
        )

        async def unsupported(*args, **kwargs):
            assert args == ("--version",)
            return _WslcResult(0, b"wslc 3.0.1.0\n", b"")

        monkeypatch.setattr(backend, "_wslc", unsupported)
        with pytest.raises(SandboxCapabilityNotSupported):
            await backend.acquire(key, spec)

    asyncio.run(scenario())


def _sandbox(entries):
    calls = []

    async def archive(instance, guest, cap):
        assert instance == "immutable-id"
        calls.append((guest, cap))
        if guest not in entries:
            return ArchiveResult(None, b"", 1, b"Error code: ERROR_PATH_NOT_FOUND\r\n")
        return _read_result(entries[guest], cap)

    async def no_guest(*args, **kwargs):
        raise AssertionError("output reads must not run guest utilities")

    return _WslcSandbox(no_guest, "name", 5, instance_id="immutable-id", archive=archive), calls


def _read_result(payload, cap):
    entry, offset = entry_prefix(payload)
    if cap is not None:
        if not entry.isreg():
            raise OSError("not regular")
        if entry.size > cap:
            raise SandboxTransferCapExceeded("grew")
    return ArchiveResult(
        entry, payload[offset : offset + entry.size] if cap is not None else b"", 0, b""
    )


@pytest.mark.parametrize("method", ["stat_file", "read_file"])
@pytest.mark.parametrize("linked", ["/work", "/work/parent"])
def test_linked_ancestors_are_refused_before_the_leaf(method, linked):
    entries = {"/work": _tar(kind=tarfile.DIRTYPE), "/work/parent": _tar(kind=tarfile.DIRTYPE)}
    entries[linked] = _tar(kind=tarfile.SYMTYPE)
    sandbox, calls = _sandbox(entries)
    kwargs = {"max_bytes": 100} if method == "read_file" else {}
    with pytest.raises(ValueError, match="link"):
        asyncio.run(getattr(sandbox, method)("parent/file", working_directory="/work", **kwargs))
    assert all(path != "/work/parent/file" for path, _ in calls)


def test_final_link_stat_and_read_are_distinct_and_growth_is_rechecked():
    entries = {"/work": _tar(kind=tarfile.DIRTYPE), "/work/out": _tar(kind=tarfile.SYMTYPE)}
    sandbox, calls = _sandbox(entries)
    assert (
        asyncio.run(sandbox.stat_file("out", working_directory="/work")).kind == EntryKind.SYMLINK
    )
    with pytest.raises(OSError):
        asyncio.run(sandbox.read_file("out", working_directory="/work", max_bytes=10))
    entries["/work/out"] = _tar(b"a")
    assert asyncio.run(sandbox.stat_file("out", working_directory="/work")).size_bytes == 1
    entries["/work/out"] = _tar(b"a" * 20)
    with pytest.raises(SandboxTransferCapExceeded):
        asyncio.run(sandbox.read_file("out", working_directory="/work", max_bytes=10))


def test_missing_and_engine_failure_keep_distinct_semantics():
    sandbox, _ = _sandbox({"/work": _tar(kind=tarfile.DIRTYPE)})
    assert asyncio.run(sandbox.stat_file("missing", working_directory="/work")) is None
    with pytest.raises(FileNotFoundError):
        asyncio.run(sandbox.read_file("missing", working_directory="/work", max_bytes=100))

    async def failed(*args):
        return ArchiveResult(None, b"", 1, b"Error code: E_FAIL\r\n")

    sandbox._archive = failed
    with pytest.raises(RuntimeError):
        asyncio.run(sandbox.stat_file("out", working_directory="/work"))


def test_missing_cli_is_not_reported_as_a_missing_guest_path():
    sandbox, _ = _sandbox({})

    async def missing_cli(*args):
        raise FileNotFoundError("wslc executable disappeared")

    sandbox._archive = missing_cli
    with pytest.raises(FileNotFoundError, match="executable"):
        asyncio.run(sandbox.stat_file("out", working_directory="/work"))


def test_direct_reads_cannot_raise_the_advertised_file_ceiling():
    sandbox, calls = _sandbox({"/work": _tar(kind=tarfile.DIRTYPE), "/work/out": _tar(b"a")})
    assert asyncio.run(sandbox.read_file("out", working_directory="/work", max_bytes=2**40)) == b"a"
    assert calls[-1] == ("/work/out", 8 * 1024 * 1024)


def test_supported_input_checks_use_archive_metadata_without_host_copies():
    sandbox, calls = _sandbox({"/work": _tar(kind=tarfile.DIRTYPE)})
    entry = asyncio.run(sandbox._stat_guest("/work", "relative"))
    assert entry.kind is EntryKind.DIRECTORY and entry.path == "relative"
    assert calls == [("/work", None)]


@pytest.mark.parametrize("failure", [TimeoutError(), FileNotFoundError()])
def test_unavailable_version_withholds_output(monkeypatch, failure):
    async def unavailable(*args, **kwargs):
        raise failure

    monkeypatch.setattr(WslcSandboxBackend, "_wslc", unavailable)
    backend = asyncio.run(WslcSandboxBackend.create(WslcSandboxConfig()))
    assert Capability.FILES_OUT not in backend.declarations.capabilities
