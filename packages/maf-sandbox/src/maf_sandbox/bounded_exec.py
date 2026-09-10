"""Optional execution with a host-enforced output bound, and a CLI backend helper."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from ._protocol import ExecResult

__all__ = ["BoundedExec", "SandboxExecOutputLimitExceeded", "read_bounded_process_output"]


class SandboxExecOutputLimitExceeded(ValueError):
    """Execution output exceeded its host-side byte budget; no partial result is returned."""


@runtime_checkable
class BoundedExec(Protocol):
    """Optional sandbox surface for callers that must bound untrusted execution output."""

    async def exec_bounded(
        self,
        command: str | Sequence[str],
        *,
        working_directory: str,
        timeout: float,
        max_output_bytes: int,
    ) -> ExecResult:
        """Apply ``exec`` semantics with a positive combined stdout/stderr byte budget.

        Enforce the budget while receiving output, before decoding or buffering the whole
        response. Transport framing may consume the budget too. Overflow raises
        ``SandboxExecOutputLimitExceeded``; timeout/cancellation preserve their usual meaning.
        Closing the host transport does not establish that guest processes have stopped.
        """
        ...


async def read_bounded_process_output(
    process: asyncio.subprocess.Process, *, max_output_bytes: int, timeout: float | None
) -> tuple[bytes, bytes]:
    """Read both pipes within one byte budget, reaping the child on any incomplete read.

    The process must have piped stdout/stderr and no pending stdin input. Pipe buffers and
    read chunks add fixed overhead; timeout/cancellation cleanup has a separate three seconds.
    """
    if type(max_output_bytes) is not int or max_output_bytes <= 0:
        raise ValueError("max_output_bytes must be a positive integer")
    assert process.stdout is not None and process.stderr is not None
    streams = (process.stdout, process.stderr)
    received = 0

    async def read(stream: asyncio.StreamReader) -> bytes:
        nonlocal received
        chunks: list[bytes] = []
        while chunk := await stream.read(min(65536, max_output_bytes - received + 1)):
            received += len(chunk)
            if received > max_output_bytes:
                raise SandboxExecOutputLimitExceeded("execution output exceeded its byte budget")
            chunks.append(chunk)
        return b"".join(chunks)

    async def discard(stream: asyncio.StreamReader) -> None:
        while await stream.read(65536):
            pass

    tasks = [asyncio.create_task(read(stream)) for stream in streams]
    complete = False
    try:
        async with asyncio.timeout(timeout):
            stdout, stderr = await asyncio.gather(*tasks)
            await process.wait()
        complete = True
        return stdout, stderr
    finally:
        if not complete:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            with contextlib.suppress(Exception):
                async with asyncio.timeout(3):
                    await asyncio.gather(*(discard(stream) for stream in streams), process.wait())
