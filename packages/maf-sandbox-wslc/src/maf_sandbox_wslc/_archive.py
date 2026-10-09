"""Bounded, binary WSLC archive reads without guest utilities or host staging."""

from __future__ import annotations

import asyncio
import contextlib
import io
import tarfile
from dataclasses import dataclass

from maf_sandbox import SandboxTransferCapExceeded
from maf_sandbox.paths import tar_header_from_block

BLOCK = 512
METADATA_LIMIT = 64 * 1024
_STDERR_LIMIT = 64 * 1024


def entry_prefix(data: bytes) -> tuple[tarfile.TarInfo | None, int]:
    """Return the effective header, or the bounded prefix still needed to parse it."""
    offset = 0
    for _ in range(32):
        needed = offset + BLOCK
        if needed > METADATA_LIMIT:
            raise RuntimeError("wslc tar metadata exceeds 64 KiB")
        if len(data) < needed:
            return None, needed
        try:
            info = tar_header_from_block(data[offset:needed])
        except tarfile.HeaderError as exc:
            raise RuntimeError("wslc returned a malformed tar header") from exc
        if info.size < 0 or info.type == tarfile.GNUTYPE_SPARSE:
            raise RuntimeError("wslc returned a negative size or unsupported sparse entry")
        if info.type not in (
            tarfile.XHDTYPE,
            tarfile.XGLTYPE,
            tarfile.GNUTYPE_LONGNAME,
            tarfile.GNUTYPE_LONGLINK,
        ):
            if offset == 0:
                return info, needed
            try:
                with tarfile.open(fileobj=io.BytesIO(data[:needed]), mode="r:") as archive:
                    entry = archive.next()
            except (tarfile.TarError, ValueError, OverflowError) as exc:
                raise RuntimeError("wslc returned malformed extended metadata") from exc
            if entry is None or entry.size < 0 or entry.sparse is not None:
                raise RuntimeError("wslc returned an unsupported tar entry")
            return entry, entry.offset_data
        end = needed + info.size
        offset = needed + ((info.size + BLOCK - 1) // BLOCK) * BLOCK
        if offset + BLOCK > METADATA_LIMIT:
            raise RuntimeError("wslc tar metadata exceeds 64 KiB")
        if len(data) < offset:
            return None, offset + BLOCK
        if info.type in (tarfile.XHDTYPE, tarfile.XGLTYPE):
            records = data[needed:end]
            while records:
                length, separator, _ = records.partition(b" ")
                if not separator or not length.isdigit() or len(length) > 6:
                    raise RuntimeError("wslc returned a malformed PAX record")
                size = int(length)
                record = records[len(length) + 1 : size]
                if size > len(records) or not record.endswith(b"\n") or b"=" not in record:
                    raise RuntimeError("wslc returned a malformed PAX record")
                key, _, value = record[:-1].partition(b"=")
                if key.startswith(b"GNU.sparse."):
                    raise RuntimeError("wslc sparse tar entries are unsupported")
                if key in (b"size", b"uid", b"gid") and (not value.isdigit() or len(value) > 20):
                    raise RuntimeError("wslc returned an invalid PAX size or owner")
                records = records[size:]
    raise RuntimeError("wslc tar metadata exceeds 32 headers")


@dataclass(frozen=True)
class ArchiveResult:
    """One engine entry and diagnostics; a metadata-only read deliberately stops the CLI."""

    entry: tarfile.TarInfo | None
    data: bytes
    returncode: int
    stderr: bytes


async def read_archive_process(
    process: asyncio.subprocess.Process, *, max_bytes: int | None, timeout: float
) -> ArchiveResult:
    """Read metadata before any body, then require a complete single-file archive and exit.

    None requests metadata only. Cleanup kills and reaps the CLI with bounded pipe draining;
    it does not terminate guest programs.
    """
    assert process.stdout is not None and process.stderr is not None
    stdout, stderr = process.stdout, process.stderr

    async def diagnostics() -> bytes:
        head = bytearray()
        while chunk := await stderr.read(65536):
            head.extend(chunk[: max(0, _STDERR_LIMIT - len(head))])
        return bytes(head)

    async def discard(stream: asyncio.StreamReader) -> None:
        while await stream.read(65536):
            pass

    async def exact(size: int) -> bytes:
        try:
            return await stdout.readexactly(size)
        except asyncio.IncompleteReadError as exc:
            raise RuntimeError("wslc returned a truncated tar archive") from exc

    errors = asyncio.create_task(diagnostics())
    complete = False
    try:
        async with asyncio.timeout(timeout):
            prefix = bytearray()
            entry = None
            needed = BLOCK
            while entry is None:
                chunk = await stdout.read(needed - len(prefix))
                if not chunk:
                    diagnostic = await errors
                    await process.wait()
                    if prefix:
                        raise RuntimeError("wslc returned incomplete tar metadata")
                    complete = True
                    return ArchiveResult(None, b"", process.returncode or 0, diagnostic)
                prefix.extend(chunk)
                entry, needed = entry_prefix(bytes(prefix))
            if max_bytes is None:
                return ArchiveResult(entry, b"", 0, b"")
            if not entry.isreg():
                raise OSError("wslc output is not a regular file and is refused")
            if entry.size > max_bytes:
                raise SandboxTransferCapExceeded(
                    f"wslc output is {entry.size} bytes and the read limit is {max_bytes}"
                )
            body = await exact(entry.size)
            padding = await exact((-entry.size) % BLOCK)
            if any(padding):
                raise RuntimeError("wslc returned invalid tar padding")
            if any(await exact(2 * BLOCK)):
                raise RuntimeError("wslc returned extra entries or invalid tar termination")
            trailing = 0
            while chunk := await stdout.read(min(65536, METADATA_LIMIT + 1 - trailing)):
                trailing += len(chunk)
                if any(chunk) or trailing > METADATA_LIMIT:
                    raise RuntimeError("wslc returned excessive or invalid trailing tar data")
            if trailing % BLOCK:
                raise RuntimeError("wslc returned a partial trailing tar block")
            diagnostic = await errors
            await process.wait()
            complete = True
            return ArchiveResult(entry, body, process.returncode or 0, diagnostic)
    finally:
        if not complete:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            errors.cancel()
            await asyncio.gather(errors, return_exceptions=True)
            # The engine can retain a pipe handle after the CLI exits.
            with contextlib.suppress(Exception):
                async with asyncio.timeout(3):
                    await asyncio.gather(discard(stdout), discard(stderr), process.wait())
