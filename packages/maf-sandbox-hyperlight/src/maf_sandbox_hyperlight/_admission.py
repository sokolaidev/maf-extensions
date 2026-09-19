"""Call ownership shared by every router over this process's Hyperlight registry."""

from __future__ import annotations

import asyncio
import math
import threading
import time
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field

from maf_sandbox import SandboxKey, SandboxQueuedTimeout


@dataclass(eq=False)
class _Lease:
    lock: threading.Lock = field(default_factory=threading.Lock)
    owner: str | None = None
    users: int = 0


_guard = threading.Lock()
_leases: dict[tuple[SandboxKey, str], _Lease] = {}
_current: ContextVar[tuple[_Lease, str] | None] = ContextVar("hyperlight_call", default=None)


def require_owner(key: SandboxKey, kind: str, *, allow_idle: bool = False) -> None:
    """Refuse direct access or another call's authority before touching shared state."""
    with _guard:
        lease = _leases.get((key, kind))
        if lease is None or lease.owner is None:
            if allow_idle:
                return
        elif _current.get() == (lease, lease.owner):
            return
    raise RuntimeError("file-enabled Hyperlight access requires its active call_admission scope")


@asynccontextmanager
async def admit(key: SandboxKey, kind: str, *, owner: str, timeout: float) -> AsyncGenerator[None]:
    if not owner or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("admission needs an owner and a positive finite timeout")
    deadline = time.monotonic() + timeout
    with _guard:
        lease = _leases.setdefault((key, kind), _Lease())
        lease.users += 1
    acquired = False
    try:
        while not acquired:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise SandboxQueuedTimeout("deadline expired waiting for Hyperlight call ownership")
            acquired = lease.lock.acquire(blocking=False)
            if not acquired:
                await asyncio.sleep(min(0.01, remaining))
        with _guard:
            lease.owner = owner
        # Router cleanup may exit in another task. A released token grants no authority.
        token = _current.set((lease, owner))
        try:
            yield
        finally:
            try:
                _current.reset(token)
            except ValueError:
                _current.set(None)
    finally:
        with _guard:
            if acquired:
                lease.owner = None
                lease.lock.release()
            lease.users -= 1
            if not lease.users:
                del _leases[key, kind]
