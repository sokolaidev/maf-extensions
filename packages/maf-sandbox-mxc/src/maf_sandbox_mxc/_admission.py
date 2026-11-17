"""Exclusive ownership through file staging, execution, delivery and cleanup."""

from __future__ import annotations

import asyncio
import math
import threading
import time
from collections.abc import AsyncGenerator, Generator
from contextlib import AbstractContextManager, asynccontextmanager, contextmanager
from contextvars import ContextVar

from maf_sandbox import SandboxQueuedTimeout


class Admission:
    """One live call per backend root; cleanup authority expires with its lease."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.permit: object | None = None
        self.target: object | None = None
        self.current: ContextVar[object | None] = ContextVar("mxc_call", default=None)

    def require(self, target: object, *, idle: bool = False) -> None:
        if self.permit is None and idle:
            return
        if self.permit is None or self.current.get() is not self.permit or self.target != target:
            raise RuntimeError("MXC access requires the active call_admission scope")

    @contextmanager
    def activate(self, permit: object) -> Generator[None]:
        if self.permit is not permit:
            raise RuntimeError("MXC call admission has expired")
        token = self.current.set(permit)
        try:
            yield
        finally:
            self.current.reset(token)

    @asynccontextmanager
    async def enter(
        self, target: object, timeout: float
    ) -> AsyncGenerator[AbstractContextManager[None]]:
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("admission requires a positive finite timeout")
        deadline = time.monotonic() + timeout
        acquired = False
        try:
            while not acquired:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise SandboxQueuedTimeout("deadline expired waiting for MXC ownership")
                acquired = self.lock.acquire(blocking=False)
                if not acquired:
                    await asyncio.sleep(min(0.01, remaining))
            self.permit = permit = object()
            self.target = target
            with self.activate(permit):
                yield self.activate(permit)
        finally:
            if acquired:
                self.permit = None
                self.target = None
                self.lock.release()
