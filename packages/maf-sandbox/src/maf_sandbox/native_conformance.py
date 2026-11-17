"""File conformance for native runtimes that refuse links and provide no shell.

The subject must measure guest reads and link refusal through its native transport. Reading
the backend's staged upload cache is not evidence that a guest received those bytes.
"""

from __future__ import annotations

from typing import Protocol

from ._outputs import SandboxTransferCapExceeded
from ._protocol import Capability, EntryKind, Sandbox
from .conformance import (
    assert_call_scope_conformance as assert_call_scope_conformance,
)
from .conformance import (
    assert_exec_conformance as assert_exec_conformance,
)
from .conformance import (
    assert_storage_base_conformance,
)


class NativeFileSubject(Protocol):
    """A runtime whose guest cannot create links, with independent native read controls."""

    sandbox: Sandbox
    capabilities: frozenset[Capability]
    working_directory: str

    async def read_in_guest(self, path: str) -> bytes:
        """Read bytes by executing in the guest, independent of the host pull implementation."""
        ...

    async def assert_links_refused(self) -> None:
        """Attempt guest symbolic and hard links; fail unless both are refused without a link."""
        ...


async def assert_files_in_conformance(subject: NativeFileSubject) -> None:
    """Verify exact guest-visible writes without assuming an EXEC command language."""
    if Capability.FILES_IN not in subject.capabilities:
        raise ValueError("FILES_IN is not declared")
    await subject.assert_links_refused()
    sandbox, cwd = subject.sandbox, subject.working_directory
    for content in (bytes(range(256)), "héllo 世界", b"replacement"):
        await sandbox.write_file("native-input/nested/value", content, working_directory=cwd)
        expected = content.encode() if isinstance(content, str) else content
        assert await subject.read_in_guest("native-input/nested/value") == expected
    for path in ("../escape", "/escape", "bad\\path", "."):
        try:
            await sandbox.write_file(path, b"refuse", working_directory=cwd)
        except ValueError:
            pass
        else:
            raise AssertionError(f"unsafe write was admitted: {path}")


async def assert_files_out_conformance(subject: NativeFileSubject) -> None:
    """Verify bounded bytes, metadata, enumeration and confinement on a link-free file plane."""
    if not {Capability.FILES_IN, Capability.FILES_OUT} <= subject.capabilities:
        raise ValueError("native file controls require FILES_IN and FILES_OUT")
    await subject.assert_links_refused()
    await assert_storage_base_conformance(subject.sandbox, subject.capabilities)
    sandbox, cwd = subject.sandbox, subject.working_directory
    data = bytes(range(256))
    await sandbox.write_file("native-output/value", data, working_directory=cwd)
    assert await subject.read_in_guest("native-output/value") == data
    entry = await sandbox.stat_file("native-output/value", working_directory=cwd)
    assert entry is not None and entry.kind is EntryKind.FILE and entry.size_bytes == len(data)
    assert (
        await sandbox.read_file("native-output/value", working_directory=cwd, max_bytes=256) == data
    )
    try:
        await sandbox.read_file("native-output/value", working_directory=cwd, max_bytes=255)
    except SandboxTransferCapExceeded:
        pass
    else:
        raise AssertionError("an oversized file was returned or truncated")
    if Capability.FILES_LIST in subject.capabilities:
        entries = await sandbox.list_dir("native-output", working_directory=cwd)
        assert [(entry.path, entry.kind, entry.size_bytes) for entry in entries] == [
            ("native-output/value", EntryKind.FILE, 256)
        ]
    assert await sandbox.stat_file("absent-native-file", working_directory=cwd) is None
    for path in ("../escape", "/escape", "bad\\path"):
        try:
            await sandbox.read_file(path, working_directory=cwd, max_bytes=1)
        except ValueError:
            pass
        else:
            raise AssertionError(f"unsafe read was admitted: {path}")


async def assert_files_delete_conformance(subject: NativeFileSubject) -> None:
    """Verify recursive deletion boundaries and refusal to delete a working directory."""
    if Capability.FILES_DELETE not in subject.capabilities:
        raise ValueError("FILES_DELETE is not declared")
    await subject.assert_links_refused()
    sandbox, cwd = subject.sandbox, subject.working_directory
    await sandbox.write_file("native-delete/nested/value", b"remove", working_directory=cwd)
    await sandbox.write_file("native-bystander", b"keep", working_directory=cwd)
    try:
        await sandbox.remove("native-delete", working_directory=cwd)
    except OSError:
        pass
    else:
        raise AssertionError("directory removal requires recursive=True")
    for _ in range(2):
        await sandbox.remove("native-delete", working_directory=cwd, recursive=True)
    assert await sandbox.stat_file("native-delete", working_directory=cwd) is None
    assert await subject.read_in_guest("native-bystander") == b"keep"
    for path in (".", "../escape", "/escape"):
        try:
            await sandbox.remove(path, working_directory=cwd, recursive=True)
        except ValueError:
            pass
        else:
            raise AssertionError(f"unsafe removal was admitted: {path}")


async def assert_reclaim_conformance(subject: NativeFileSubject) -> None:
    """Verify a native call tree is reclaimed without touching a sibling."""
    if Capability.RECLAIM not in subject.capabilities:
        raise ValueError("RECLAIM is not declared")
    await subject.assert_links_refused()
    sandbox, cwd = subject.sandbox, subject.working_directory
    await sandbox.write_file("native-reclaim/nested/value", b"remove", working_directory=cwd)
    await sandbox.write_file("native-survivor", b"keep", working_directory=cwd)
    for _ in range(2):
        await sandbox.reclaim("native-reclaim", working_directory=cwd, timeout=60)
    assert await sandbox.stat_file("native-reclaim", working_directory=cwd) is None
    assert await subject.read_in_guest("native-survivor") == b"keep"
