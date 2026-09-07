"""Resolve cleanup and coordinate shared or exclusive use of each sandbox.

RECLAIM takes a shared hold; RESET and DISPOSE require exclusive use. Failed reclaim may
escalate to disposal under a sibling, as documented in docs/sandbox/tool-call.md."""

from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import dataclass, field

from ._protocol import CLEANUP_RANK, Capability, Cleanup, SandboxKey, SandboxSpec

__all__ = [
    "QUEUED_CALL_TIMEOUT",
    "ExclusiveSlots",
    "established_cleanup",
    "needs_exclusive_use",
    "resolve_cleanup",
]

#: Bounds waiting for an incompatible call, including its body and cleanup.
QUEUED_CALL_TIMEOUT = 120.0


def established_cleanup(spec: SandboxSpec, declared: frozenset[Capability]) -> frozenset[Cleanup]:
    """Return DISPOSE plus the rungs the workload and backend establish together."""
    rungs = {Cleanup.DISPOSE}
    if Capability.SNAPSHOT in declared:
        rungs.add(Cleanup.RESET)
    if spec.confined_to_guest_call_path and Capability.RECLAIM in declared:
        rungs.add(Cleanup.RECLAIM)
    return frozenset(rungs)


def resolve_cleanup(established: frozenset[Cleanup], floor: Cleanup) -> Cleanup:
    """Return the weakest established rung at or above the floor. DISPOSE must be present."""
    above = [rung for rung in established if CLEANUP_RANK[rung] >= CLEANUP_RANK[floor]]
    return min(above, key=CLEANUP_RANK.__getitem__)


def needs_exclusive_use(rung: Cleanup) -> bool:
    """Whether the rung removes or resets the whole sandbox."""
    return CLEANUP_RANK[rung] > CLEANUP_RANK[Cleanup.RECLAIM]


@dataclass
class _Slot:
    """Who is inside one ``(key, kind)`` right now. Plain data; the guard outside protects it."""

    #: Owners holding it shared — ``RECLAIM`` calls, which may run together.
    shared: set[str] = field(default_factory=set[str])
    #: The owner holding it exclusively, if any. Never set while ``shared`` is non-empty.
    exclusive: str | None = None
    #: One per waiting call, each on the loop that registered it.
    waiters: list[tuple[asyncio.AbstractEventLoop, asyncio.Future[None]]] = field(
        default_factory=list[tuple[asyncio.AbstractEventLoop, "asyncio.Future[None]"]]
    )


class ExclusiveSlots:
    """Call-owned holds shared across every event loop using this router.

    RECLAIM callers share; stronger cleanup excludes all other callers. Owners identify calls,
    not tasks, so a child task may acquire a hold the parent releases. Waiters are notified on
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
        while True:
            with self._guard:
                slot = self._slots.setdefault(at, _Slot())
                free = (
                    slot.exclusive is None and not slot.shared
                    if exclusive
                    else slot.exclusive is None
                )
                if free or slot.exclusive == owner or (not exclusive and owner in slot.shared):
                    if exclusive:
                        slot.exclusive = owner
                    else:
                        slot.shared.add(owner)
                    return
                waiter: asyncio.Future[None] = loop.create_future()
                slot.waiters.append((loop, waiter))
            remaining = deadline - time.monotonic()
            try:
                if remaining <= 0:
                    raise TimeoutError
                await asyncio.wait_for(waiter, remaining)
            except TimeoutError:
                self._forget_waiter(at, waiter)
                raise TimeoutError(
                    f"another call is using the sandbox for {key.scope}/{key.thread_id}/"
                    f"{key.agent_dir} and did not finish within {timeout:g}s. A workload cleaned "
                    "by anything stronger than a reclaim runs one call at a time in its sandbox, "
                    "because a reset or a delete cannot run under a sibling."
                ) from None
            except BaseException:
                self._forget_waiter(at, waiter)
                raise

    def _forget_waiter(self, at: tuple[SandboxKey, str], waiter: object) -> None:
        """Drop one abandoned waiter, and the slot with it when nothing is left in it."""
        with self._guard:
            slot = self._slots.get(at)
            if slot is None:
                return
            slot.waiters = [held for held in slot.waiters if held[1] is not waiter]
            self._drop_if_idle(at, slot)

    def release(self, key: SandboxKey, kind: str, *, owner: str) -> None:
        """Release only this owner's hold; a cancelled or timed-out waiter releases nothing."""
        at = (key, kind)
        with self._guard:
            slot = self._slots.get(at)
            if slot is None:
                return
            if slot.exclusive == owner:
                slot.exclusive = None
            elif owner in slot.shared:
                slot.shared.discard(owner)
            else:
                return
            waiters = slot.waiters
            slot.waiters = []
            self._drop_if_idle(at, slot)
        for loop, waiter in waiters:
            # Through the waiter's own loop: the router serves more than one, and resolving a
            # future from a foreign loop is undefined rather than merely unfair.
            loop.call_soon_threadsafe(_resolve, waiter)

    def _drop_if_idle(self, at: tuple[SandboxKey, str], slot: _Slot) -> None:
        """Forget a slot nobody holds or wants. Call under the guard."""
        if slot.exclusive is None and not slot.shared and not slot.waiters:
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
