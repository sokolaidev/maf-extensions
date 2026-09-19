"""Call ownership shared by every router over this process's Hyperlight registry."""

from __future__ import annotations

import asyncio
import math
import threading
import time
from collections.abc import AsyncGenerator, Generator
from contextlib import AbstractContextManager, asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field

from maf_sandbox import SandboxKey, SandboxQueuedTimeout


@dataclass(eq=False)
class _Lease:
    lock: threading.Lock = field(default_factory=threading.Lock)
    permit: object | None = None
    users: int = 0


_guard = threading.Lock()
_leases: dict[tuple[SandboxKey, str], _Lease] = {}
_current: ContextVar[frozenset[tuple[_Lease, object]]] = ContextVar(
    "hyperlight_calls", default=frozenset()
)


def require_owner(key: SandboxKey, kind: str, *, allow_idle: bool = False) -> None:
    """Refuse direct access or another call's authority before touching shared state."""
    with _guard:
        lease = _leases.get((key, kind))
        if lease is None or lease.permit is None:
            if allow_idle:
                return
        elif (lease, lease.permit) in _current.get():
            return
    raise RuntimeError("file-enabled Hyperlight access requires its active call_admission scope")


@contextmanager
def _activate(lease: _Lease, permit: object) -> Generator[None]:
    with _guard:
        if lease.permit is not permit:
            raise RuntimeError("Hyperlight call admission has expired")
        current = frozenset((held, token) for held, token in _current.get() if held.permit is token)
        entry = (lease, permit)
        added = entry not in current
        _current.set(current | {entry})
    try:
        yield
    finally:
        if added:
            _current.set(_current.get() - {entry})


@asynccontextmanager
async def admit(
    key: SandboxKey, kind: str, *, owner: str, timeout: float
) -> AsyncGenerator[AbstractContextManager[None]]:
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
            permit = lease.permit = object()
        # Cleanup can activate this lease in another task without granting other leases.
        with _activate(lease, permit):
            yield _activate(lease, permit)
    finally:
        with _guard:
            if acquired:
                lease.permit = None
                lease.lock.release()
            lease.users -= 1
            if not lease.users:
                del _leases[key, kind]
