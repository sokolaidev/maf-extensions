"""Coordinate call admission and drain siblings before whole-instance cleanup."""

from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import dataclass, field
from typing import Literal

from ._protocol import (
    CLEANUP_RANK,
    Capability,
    Cleanup,
    Sandbox,
    SandboxBackend,
    SandboxKey,
    SandboxSpec,
)

__all__ = [
    "QUEUED_CALL_TIMEOUT",
    "ExclusiveSlots",
    "PendingCleanup",
    "established_cleanup",
    "resolve_cleanup",
]

#: Bounds waiting for an incompatible call, including its body and cleanup.
QUEUED_CALL_TIMEOUT = 120.0


def established_cleanup(spec: SandboxSpec, declared: frozenset[Capability]) -> frozenset[Cleanup]:
    """Return the cleanup operations the backend supports; the host decides sufficiency."""
    rungs = {Cleanup.DISPOSE}
    if Capability.SNAPSHOT in declared:
        rungs.add(Cleanup.RESET)
    if Capability.RECLAIM in declared:
        rungs.add(Cleanup.RECLAIM)
    return frozenset(rungs)


def resolve_cleanup(established: frozenset[Cleanup], floor: Cleanup) -> Cleanup:
    """Return the weakest established rung at or above the floor. DISPOSE must be present."""
    above = [rung for rung in established if CLEANUP_RANK[rung] >= CLEANUP_RANK[floor]]
    return min(above, key=CLEANUP_RANK.__getitem__)


@dataclass
class _Waiter:
    loop: asyncio.AbstractEventLoop
    future: asyncio.Future[None]
    exclusive: bool


@dataclass(eq=False)
class PendingCleanup:
    """One physical instance's folded cleanup and its loop-local completion waiters."""

    backend: SandboxBackend
    spec: SandboxSpec
    sandbox: Sandbox
    instance_id: str
    rung: Cleanup
    timeout: float
    unclean: str | None = None
    done: bool = False
    failure: str | None = None
    waiters: list[_Waiter] = field(default_factory=list[_Waiter])


@dataclass
class _Slot:
    """Who is inside one ``(key, kind)`` right now. Plain data; the guard outside protects it."""

    state: Literal["serving", "draining", "cleaning"] = "serving"
    pending: dict[tuple[int, str], PendingCleanup] = field(
        default_factory=dict[tuple[int, str], PendingCleanup]
    )
    #: Ordinary call bodies may overlap regardless of their cleanup rung.
    shared: set[str] = field(default_factory=set[str])
    #: The owner holding it exclusively, if any. Never set while ``shared`` is non-empty.
    exclusive: str | None = None
    #: One per waiting call, each on the loop that registered it.
    waiters: list[_Waiter] = field(default_factory=list[_Waiter])


class ExclusiveSlots:
    """Call-owned holds shared across every event loop using this router.

    Ordinary callers share until cleanup starts draining the entry. Explicit exclusive holds
    exclude every sibling. Owners identify calls, not tasks. Waiters are notified on
    their own loops. Sharing across routers or processes requires CALL isolation scope."""

    def __init__(self) -> None:
        # Guards the table only, never held across an await.
        self._guard = threading.Lock()
        self._slots: dict[tuple[SandboxKey, str], _Slot] = {}

    async def take(
        self, key: SandboxKey, kind: str, *, owner: str, exclusive: bool, timeout: float
    ) -> None:
        """Hold the sandbox; raise TimeoutError if incompatible owners outlast the bound."""
        at = (key, kind)
        loop = asyncio.get_running_loop()
        deadline = time.monotonic() + timeout
        queued: _Waiter | None = None
        while True:
            with self._guard:
                slot = self._slots.setdefault(at, _Slot())
                slot.waiters = [one for one in slot.waiters if not one.loop.is_closed()]
                ahead = slot.waiters[: slot.waiters.index(queued)] if queued else slot.waiters
                free = (
                    slot.exclusive is None and not slot.shared and not ahead
                    if exclusive
                    else slot.exclusive is None and not any(one.exclusive for one in ahead)
                )
                free = free and slot.state == "serving"
                if free or slot.exclusive == owner or (not exclusive and owner in slot.shared):
                    if queued is not None:
                        slot.waiters.remove(queued)
                    if exclusive:
                        slot.exclusive = owner
                    else:
                        slot.shared.add(owner)
                    return
                waiter: asyncio.Future[None] = loop.create_future()
                if queued is None:
                    queued = _Waiter(loop, waiter, exclusive)
                    slot.waiters.append(queued)
                else:
                    queued.future = waiter
            remaining = deadline - time.monotonic()
            try:
                if remaining <= 0:
                    raise TimeoutError
                await asyncio.wait_for(waiter, remaining)
            except TimeoutError:
                self._forget_waiter(at, queued)
                raise TimeoutError(
                    f"another call is using the sandbox for {key.scope}/{key.thread_id}/"
                    f"{key.agent_dir} and did not finish within {timeout:g}s. Admission waits "
                    "while the sandbox drains, cleans, or is held exclusively."
                ) from None
            except BaseException:
                self._forget_waiter(at, queued)
                raise

    def _forget_waiter(self, at: tuple[SandboxKey, str], waiter: _Waiter) -> None:
        """Drop one abandoned waiter, and the slot with it when nothing is left in it."""
        with self._guard:
            slot = self._slots.get(at)
            if slot is None:
                return
            slot.waiters = [held for held in slot.waiters if held is not waiter]
            self._drop_if_idle(at, slot)
        self._wake(at)

    def queue(
        self, key: SandboxKey, kind: str, *, owner: str, cleanup: PendingCleanup
    ) -> PendingCleanup:
        """Condemn an instance while its call still holds the entry; fold its strongest rung."""
        with self._guard:
            slot = self._slots[(key, kind)]
            if owner not in slot.shared and slot.exclusive != owner:
                raise RuntimeError("cleanup must be recorded by an admitted call")
            at = (id(cleanup.backend), cleanup.instance_id)
            existing = slot.pending.get(at)
            if existing is None:
                slot.pending[at] = cleanup
            else:
                existing.rung = max(existing.rung, cleanup.rung, key=CLEANUP_RANK.__getitem__)
                existing.sandbox = cleanup.sandbox
                existing.timeout = min(existing.timeout, cleanup.timeout)
                existing.unclean = existing.unclean or cleanup.unclean
                cleanup = existing
            slot.state = "draining"
            return cleanup

    def drain(self, key: SandboxKey, kind: str, *, owner: str) -> None:
        """Close admission for a finished call whose acquire has not returned yet."""
        with self._guard:
            slot = self._slots.get((key, kind))
            if slot is not None and (owner in slot.shared or slot.exclusive == owner):
                slot.state = "draining"

    def release(self, key: SandboxKey, kind: str, *, owner: str) -> list[PendingCleanup]:
        """Leave this hold and atomically claim cleanup if it was the last active call."""
        at = (key, kind)
        with self._guard:
            slot = self._slots.get(at)
            if slot is None:
                return []
            if slot.exclusive == owner:
                slot.exclusive = None
            elif owner in slot.shared:
                slot.shared.discard(owner)
            else:
                return []
            if not slot.shared and slot.exclusive is None and slot.pending:
                slot.state = "cleaning"
                return list(slot.pending.values())
            if not slot.shared and slot.exclusive is None:
                slot.state = "serving"
            self._drop_if_idle(at, slot)
        self._wake(at)
        return []

    async def wait(self, cleanup: PendingCleanup, *, timeout: float) -> str | None:
        """Wait on this event loop without transferring ownership of the pending cleanup."""
        loop = asyncio.get_running_loop()
        waiter = _Waiter(loop, loop.create_future(), False)
        with self._guard:
            if cleanup.done:
                return cleanup.failure
            cleanup.waiters.append(waiter)
        try:
            await asyncio.wait_for(waiter.future, timeout)
            return cleanup.failure
        finally:
            with self._guard:
                cleanup.waiters.remove(waiter)

    def complete(self, cleanup: PendingCleanup, failure: str | None) -> None:
        """Publish a landed cleanup or a failure already transferred to the router ledger."""
        with self._guard:
            cleanup.failure = failure
            cleanup.done = True
            waiters = list(cleanup.waiters)
        for waiter in waiters:
            _notify(waiter)

    def cleaned(self, key: SandboxKey, kind: str) -> None:
        """Reopen admission only after every claimed record has reached its completion path."""
        at = (key, kind)
        with self._guard:
            slot = self._slots[at]
            if not all(one.done for one in slot.pending.values()):
                raise RuntimeError("unfinished cleanup cannot reopen admission")
            slot.pending.clear()
            slot.state = "serving"
            self._drop_if_idle(at, slot)
        self._wake(at)

    def _wake(self, at: tuple[SandboxKey, str]) -> None:
        while True:
            with self._guard:
                slot = self._slots.get(at)
                if slot is None:
                    return
                slot.waiters = [one for one in slot.waiters if not one.loop.is_closed()]
                waiters = list(slot.waiters)
                self._drop_if_idle(at, slot)
            closed = False
            for waiter in waiters:
                try:
                    waiter.loop.call_soon_threadsafe(_resolve, waiter.future)
                except RuntimeError:
                    if not waiter.loop.is_closed():
                        raise
                    closed = True
            if not closed:
                return

    def _drop_if_idle(self, at: tuple[SandboxKey, str], slot: _Slot) -> None:
        """Forget a slot nobody holds or wants. Call under the guard."""
        if (
            slot.exclusive is None
            and not slot.shared
            and not slot.waiters
            and not slot.pending
            and slot.state == "serving"
        ):
            self._slots.pop(at, None)

    def holds(self, key: SandboxKey, kind: str, *, owner: str) -> bool:
        """Whether ``owner`` holds this pair, either way. For tests and diagnostics."""
        at = (key, kind)
        with self._guard:
            slot = self._slots.get(at)
            return slot is not None and (slot.exclusive == owner or owner in slot.shared)


def _resolve(waiter: asyncio.Future[None]) -> None:
    """Complete a waiter unless its own call already gave up on it."""
    if not waiter.done():
        waiter.set_result(None)


def _notify(waiter: _Waiter) -> None:
    """Notify only through the future's owning loop, tolerating loop shutdown."""
    try:
        waiter.loop.call_soon_threadsafe(_resolve, waiter.future)
    except RuntimeError:
        if not waiter.loop.is_closed():
            raise
