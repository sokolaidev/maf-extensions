"""Experimental single-use execution with bounded capture and separately budgeted disposal."""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Literal, Protocol


@dataclass(frozen=True)
class Event:
    """One raw transport event, before any display decoding."""

    kind: Literal["stdout", "stderr", "exit"]
    data: bytes = b""
    exit_code: int | None = None


class Stream(Protocol):
    """A cancellable stream owned exclusively by one experiment."""

    def __aiter__(self) -> AsyncIterator[Event]: ...

    def cancel(self) -> object: ...


@dataclass
class Receipt:
    """Primary outcome and disposal evidence; cancellation is recorded, not propagated."""

    owner: dict[str, str]
    outcome: str = "transport_error"
    stdout: bytearray = field(default_factory=bytearray)
    stderr: bytearray = field(default_factory=bytearray)
    exit_code: int | None = None
    error: str | None = None
    primary_seconds: float = 0
    total_seconds: float = 0
    cleanup: dict[str, object] = field(default_factory=dict)
    cleanup_verified: bool = False
    ownership_retained: bool = True


class OutputLimitError(Exception):
    """A raw chunk exceeds the remaining aggregate capture budget."""


async def execute_owned(
    start: Callable[[], Stream],
    dispose: Callable[[], Awaitable[dict[str, object]]],
    *,
    owner: dict[str, str],
    seconds: float,
    output_limit: int,
    cleanup_seconds: float,
) -> Receipt:
    """Record an execution, then dispose even on success; never infer cleanup from stream closure."""
    if not all(math.isfinite(x) and x > 0 for x in (seconds, cleanup_seconds)):
        raise ValueError("execution and cleanup budgets must be positive and finite")
    if output_limit < 0:
        raise ValueError("output_limit must be nonnegative")
    receipt = Receipt(owner=dict(owner))
    began = time.monotonic()
    stream: Stream | None = None
    budget = asyncio.timeout(seconds)
    try:
        async with budget:
            stream = start()
            async for event in stream:
                if receipt.exit_code is not None:
                    raise RuntimeError("event after terminal exit")
                if event.kind == "exit":
                    if event.exit_code is None:
                        raise RuntimeError("missing exit code")
                    receipt.exit_code = event.exit_code
                else:
                    if len(receipt.stdout) + len(receipt.stderr) + len(event.data) > output_limit:
                        raise OutputLimitError("combined stdout/stderr limit exceeded")
                    target = receipt.stdout if event.kind == "stdout" else receipt.stderr
                    target.extend(event.data)
            if receipt.exit_code is None:
                raise RuntimeError("stream ended without exit")
            receipt.outcome = "exited"
    except OutputLimitError as exc:
        receipt.outcome, receipt.error = "output_limit", str(exc)
    except asyncio.CancelledError:
        caller = asyncio.current_task()
        receipt.outcome = (
            "cancelled" if caller is not None and caller.cancelling() else "transport_error"
        )
        if receipt.outcome == "transport_error":
            receipt.error = "transport cancelled without caller cancellation"
    except Exception as exc:
        receipt.outcome = "deadline" if budget.expired() else "transport_error"
        receipt.error = f"{type(exc).__name__}: {exc}"
    finally:
        receipt.primary_seconds = time.monotonic() - began
        if stream is not None:
            try:
                stream.cancel()
            except Exception as exc:
                receipt.error = f"{receipt.error or ''}; stream cancellation: {exc}"

        async def cleanup() -> None:
            try:
                async with asyncio.timeout(cleanup_seconds):
                    receipt.cleanup = await dispose()
                receipt.cleanup_verified = receipt.cleanup.get("verified") is True
            except Exception as exc:
                receipt.cleanup = {"error": f"{type(exc).__name__}: {exc}"}
            except asyncio.CancelledError:
                receipt.cleanup = {"error": "cleanup cancelled independently"}
            receipt.ownership_retained = not receipt.cleanup_verified

        # The cleanup task owns its budget independently of repeated caller cancellation.
        task = asyncio.create_task(cleanup())
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                if receipt.outcome == "exited":
                    receipt.outcome = "cancelled"
        await task
        receipt.total_seconds = time.monotonic() - began
    return receipt
