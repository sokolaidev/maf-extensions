"""Measurement-only raw-exec/SFTP adapter; metadata checks do not fence file replacement."""

from __future__ import annotations

import asyncio
import math
import posixpath
import stat
import uuid
from collections.abc import Sequence
from importlib import import_module
from typing import Any

from maf_sandbox import EntryKind, ExecResult, SandboxEntry, SandboxTransferCapExceeded
from maf_sandbox.paths import (
    confine_resolve_guest_delete_path,
    confine_resolve_guest_list_path,
    confine_resolve_guest_path,
    confine_resolve_guest_read_path,
    confine_resolve_guest_write_path,
    resolve_guest_working_directory,
)
from openshell_owned_exec import OutputLimitError
from openshell_owned_probe import RawStream
from openshell_sftp_probe import native_sftp


def entry(path: str, metadata: Any) -> SandboxEntry:
    """Preserve the native lstat kind, including links and nonregular files."""
    mode = metadata.st_mode
    kind = (
        EntryKind.FILE
        if stat.S_ISREG(mode)
        else EntryKind.DIRECTORY
        if stat.S_ISDIR(mode)
        else EntryKind.SYMLINK
        if stat.S_ISLNK(mode)
        else EntryKind.OTHER
    )
    return SandboxEntry(path, kind, metadata.st_size if kind is EntryKind.FILE else None)


class Candidate:
    """Expose a provisional file plane for probes only; no backend capabilities are declared."""

    def __init__(self, client: Any, owner: dict, dispose: Any, *, base: str = "/sandbox"):
        self.client, self.owner, self.dispose = client, owner, dispose
        self.base = base
        self.output_limit = 1024 * 1024
        self.before_open: Any = None

    @property
    def instance_id(self) -> str:
        return self.owner["sandbox_id"]

    def cwd(self, working_directory: str) -> str:
        return resolve_guest_working_directory(working_directory, self.base)

    def virtual(self, guest_path: str) -> str:
        # This pinned SFTP server roots every session at the image workdir.
        confined = confine_resolve_guest_path(guest_path, "/sandbox")
        return posixpath.relpath(confined, "/sandbox")

    def sftp(self):
        return native_sftp(self.client, self.owner["name"], self.instance_id)

    async def raw_stat(self, guest_path: str) -> SandboxEntry | None:
        if guest_path == "/":
            return SandboxEntry(guest_path, EntryKind.DIRECTORY, None)
        with self.sftp() as sftp:
            try:
                metadata = sftp.lstat(self.virtual(guest_path))
            except FileNotFoundError:
                return None
            return entry(guest_path, metadata)

    async def write_file(self, path: str, content: str | bytes, *, working_directory: str) -> None:
        cwd = self.cwd(working_directory)
        resolved = await confine_resolve_guest_write_path(self.raw_stat, path, cwd)
        data = content.encode() if isinstance(content, str) else content
        if len(data) > self.output_limit:
            raise SandboxTransferCapExceeded("fixture input cap exceeded")
        with self.sftp() as sftp:
            parent = posixpath.dirname(self.virtual(resolved))
            current = ""
            for part in parent.split("/"):
                if part in ("", "."):
                    continue
                current = posixpath.join(current, part)
                try:
                    sftp.mkdir(current)
                except OSError:
                    if not stat.S_ISDIR(sftp.lstat(current).st_mode):
                        raise
            if self.before_open:
                await self.before_open()
            with sftp.open(self.virtual(resolved), "wb") as stream:
                stream.write(data)

    async def stat_file(self, path: str, *, working_directory: str) -> SandboxEntry | None:
        cwd = self.cwd(working_directory)
        resolved = await confine_resolve_guest_read_path(self.raw_stat, path, cwd)
        found = await self.raw_stat(resolved)
        if found is None:
            return None
        return SandboxEntry(posixpath.relpath(resolved, cwd), found.kind, found.size_bytes)

    async def read_file(self, path: str, *, working_directory: str, max_bytes: int) -> bytes:
        if max_bytes < 0:
            raise ValueError("max_bytes must be nonnegative")
        cwd = self.cwd(working_directory)
        resolved = await confine_resolve_guest_read_path(self.raw_stat, path, cwd)
        found = await self.raw_stat(resolved)
        if found is None:
            raise FileNotFoundError(path)
        if found.kind is not EntryKind.FILE:
            raise OSError("only regular files may be read")
        if found.size_bytes is None or found.size_bytes > max_bytes:
            raise SandboxTransferCapExceeded("native file exceeds caller cap")
        if self.before_open:
            await self.before_open()
        with self.sftp() as sftp:
            with sftp.open(self.virtual(resolved), "rb") as stream:
                data = stream.read(min(max_bytes, self.output_limit) + 1)
        if len(data) > min(max_bytes, self.output_limit):
            raise SandboxTransferCapExceeded("native stream exceeds capture cap")
        return data

    async def list_dir(self, path: str, *, working_directory: str) -> tuple[SandboxEntry, ...]:
        cwd = self.cwd(working_directory)
        resolved = await confine_resolve_guest_list_path(self.raw_stat, path, cwd)
        with self.sftp() as sftp:
            return tuple(
                entry(item.filename, item) for item in sftp.listdir_attr(self.virtual(resolved))
            )

    async def remove(self, path: str, *, working_directory: str, recursive: bool = False) -> None:
        cwd = self.cwd(working_directory)
        resolved = await confine_resolve_guest_delete_path(self.raw_stat, path, cwd)
        with self.sftp() as sftp:

            def remove(virtual: str) -> None:
                try:
                    metadata = sftp.lstat(virtual)
                except FileNotFoundError:
                    return
                if stat.S_ISDIR(metadata.st_mode):
                    if not recursive:
                        raise IsADirectoryError(path)
                    for name in sftp.listdir(virtual):
                        if name in (".", "..") or "/" in name or "\\" in name:
                            raise ValueError("unsafe SFTP directory entry")
                        remove(posixpath.join(virtual, name))
                    sftp.rmdir(virtual)
                else:
                    sftp.remove(virtual)

            remove(self.virtual(resolved))

    async def exec(
        self, command: str | Sequence[str], *, working_directory: str, timeout: float
    ) -> ExecResult:
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be positive and finite")
        grpc = import_module("grpc")
        bindings = import_module("openshell._proto.openshell_pb2_grpc")
        request = self.client.pb.ExecSandboxRequest(
            sandbox=self.owner["name"],
            workspace_scope=self.client.scope,
            command=["sh", "-c", command] if isinstance(command, str) else list(command),
            workdir=self.cwd(working_directory),
            no_login_shell=True,
            request_id=str(uuid.uuid4()),
        )
        out, err = bytearray(), bytearray()
        exited = None
        budget = asyncio.timeout(timeout)
        try:
            async with budget:
                async with grpc.aio.insecure_channel(
                    "127.0.0.1:19797", options=[("grpc.max_receive_message_length", 65536)]
                ) as channel:
                    call = bindings.OpenShellStub(channel).ExecSandbox(
                        request, metadata=[("authorization", "Bearer " + self.client.token)]
                    )
                    try:
                        async for event in RawStream(call):
                            if exited is not None:
                                raise RuntimeError("event after terminal exit")
                            if event.kind == "exit":
                                exited = event.exit_code
                            else:
                                if len(out) + len(err) + len(event.data) > self.output_limit:
                                    raise OutputLimitError("aggregate output cap exceeded")
                                (out if event.kind == "stdout" else err).extend(event.data)
                    finally:
                        call.cancel()
                if exited is None:
                    raise RuntimeError("missing terminal exit")
        except BaseException as primary:
            cleanup = asyncio.create_task(self.dispose())
            while not cleanup.done():
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    pass
            await cleanup
            if isinstance(primary, TimeoutError) and not budget.expired():
                raise RuntimeError(
                    "transport timeout did not exhaust the caller budget"
                ) from primary
            raise
        return ExecResult(stdout_bytes=bytes(out), stderr_bytes=bytes(err), exit_code=exited)

    async def run_code(self, code: str, *, timeout: float) -> ExecResult:
        raise NotImplementedError("native runtime execution is withheld")

    async def reclaim(self, directory: str, *, working_directory: str, timeout: float) -> None:
        raise NotImplementedError("reclaim is withheld")

    async def reset(self, *, timeout: float) -> None:
        raise NotImplementedError("snapshot is withheld")
