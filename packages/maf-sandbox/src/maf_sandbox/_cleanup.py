"""The cleanup ladder: which rung a call ends on, and who may be inside the sandbox while it does.

Two things live here.  :func:`established_cleanup` answers what a spec and a backend *between
them* establish, which is the whole of "dispose by default" — a workload that claims nothing is
cleaned by disposal.  :class:`ExclusiveSlots` answers who else may be in the sandbox, and the
rule is one sentence: **a sandbox cleaned by anything above** :data:`~maf_sandbox.Cleanup.RECLAIM`
**serves one call at a time.**

That is what makes the strong rungs safe rather than a counting scheme.  A reset or a delete
under a running sibling is a failed call, so the alternative would be counting calls in flight,
condemning the sandbox when one leaves, and making later arrivals wait out a cleanup — three
states and a race for each.  Serialising instead means the call running the cleanup is provably
the only call there, so there is nothing to count and nothing to condemn.

``RECLAIM`` takes no slot and behaves exactly as it always has: calls share the sandbox, and the
one case that still deletes it under a sibling — a reclaim that failed, or a program that would
not stop — kills that sibling.  ``docs/sandbox/tool-call.md`` § Concurrency has always priced
that as the accepted cost of an escalation, and it stays rare because it is a failure path.

``docs/sandbox/tool-call.md`` carries the decision; this is the mechanism.
"""

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

#: How long a call waits for the sandbox it needs when a sibling is using it exclusively.
#:
#: It is waiting for a whole tool call rather than for a cleanup, so seconds are too few — and
#: it cannot be unbounded, because a call that never returns would take its conversation with
#: it.  A constant rather than a router keyword until a host asks for one: the number that
#: matters to a host is its own call timeout, and this only has to be larger than a typical
#: sibling and smaller than forever.
QUEUED_CALL_TIMEOUT = 120.0


def established_cleanup(spec: SandboxSpec, declared: frozenset[Capability]) -> frozenset[Cleanup]:
    """Which rungs this spec and this backend establish together.

    Never empty: :data:`Cleanup.DISPOSE` is established by construction on every backend, which
    is what makes :func:`resolve_cleanup` total and what makes disposal the honest default for a
    workload that claims nothing.

    ``RECLAIM`` needs both halves and neither alone — the kind's
    :attr:`~maf_sandbox.SandboxSpec.confined_to_guest_call_path` claim *and* the backend's
    :data:`Capability.RECLAIM`.  A backend that can take the directory does not make the
    program stay inside it, and a kind that stays inside it cannot be cleaned by a backend that
    will not delete.
    """
    rungs = {Cleanup.DISPOSE}
    if Capability.SNAPSHOT in declared:
        rungs.add(Cleanup.RESET)
    if spec.confined_to_guest_call_path and Capability.RECLAIM in declared:
        rungs.add(Cleanup.RECLAIM)
    return frozenset(rungs)


def resolve_cleanup(established: frozenset[Cleanup], floor: Cleanup) -> Cleanup:
    """The weakest established rung at or above ``floor``.

    Total, because ``DISPOSE`` is always established and is the ladder's top: a floor no weaker
    rung reaches still resolves there.  Nothing is refused for the cleanup axis, so this returns
    rather than raising — a host that distrusts a kind's claim raises the floor instead.
    """
    above = [rung for rung in established if CLEANUP_RANK[rung] >= CLEANUP_RANK[floor]]
    return min(above, key=CLEANUP_RANK.__getitem__)


def needs_exclusive_use(rung: Cleanup) -> bool:
    """Whether a call ending on ``rung`` must hold the sandbox to itself.

    The whole of the concurrency rule, written once: everything above ``RECLAIM`` removes the
    sandbox or rewinds it, which cannot happen under a sibling.  A ``RECLAIM`` call still takes
    a *shared* hold — see :class:`ExclusiveSlots` for why it cannot simply skip the slot.
    """
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
    """Who may be inside a sandbox: shared for ``RECLAIM``, exclusive for anything above it.

    **Every caller takes a hold, and that is what makes the exclusion real.**  Letting a
    ``RECLAIM`` call skip the slot outright looks equivalent and is not: the rung is resolved
    from the *arriving spec*, while the sandbox is shared by ``(key, kind)``.  Two tools can
    attach the same kind with the same confinement claim and different ``min_cleanup``, so one
    resolves to ``RECLAIM`` and the other to ``DISPOSE`` over one sandbox — and a caller that
    skipped the slot is invisible to the one holding it, whichever order they arrive in.  A
    shared hold costs a caller nothing while no exclusive one is outstanding, and it is what
    lets a disposal wait for a sibling it would otherwise never have seen.

    **A hold belongs to the call that took it**, named by an opaque ``owner`` the caller
    supplies.  Release is by owner rather than by pair, because a waiter that was cancelled or
    timed out reaches the same cleanup path a holder does: the call that entered and the call
    still queueing behind it name one ``(key, kind)``, so a release keyed on the pair alone
    hands the holder's sandbox to a third call while the holder is still running in it.

    The owner is the *call*, not the task, and that distinction is load-bearing in the other
    direction: a tool body may spawn a task that acquires, and the ``finally`` that gives the
    sandbox back runs in the body's own task.  Keyed by task, that release would find itself a
    stranger and leak the hold.

    **Per running event loop**, because a waiter's future belongs to the loop that awaits it and
    this router serves more than one.  Two calls in different loops therefore do not exclude
    each other, which is the same bound the router's disposal locks carry and the same bound the
    isolation scope answers: a host that serves one conversation from more than one loop or
    process raises ``min_isolation_scope`` to :data:`~maf_sandbox.IsolationScope.CALL`, where
    every call has a sandbox of its own and there is nothing to share.
    """

    def __init__(self) -> None:
        # Guards the table only, never held across an await.
        self._guard = threading.Lock()
        self._slots: dict[tuple[asyncio.AbstractEventLoop, SandboxKey, str], _Slot] = {}

    def _at(self, key: SandboxKey, kind: str) -> tuple[asyncio.AbstractEventLoop, SandboxKey, str]:
        return (asyncio.get_running_loop(), key, kind)

    async def take(
        self, key: SandboxKey, kind: str, *, owner: str, exclusive: bool, timeout: float
    ) -> None:
        """Hold this sandbox for the calling task, waiting out whoever is incompatible with it.

        Raises:
            TimeoutError: the hold was not free within ``timeout``. Refusing beats waiting for
                ever behind a call that is never going to return.
        """
        at = self._at(key, kind)
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
                if free:
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
                # Cancellation, most often. The waiter never held anything, so there is nothing
                # to give back — only the registration to take back, so a later holder's release
                # does not try to wake a future nobody is awaiting.
                self._forget_waiter(at, waiter)
                raise

    def _forget_waiter(
        self, at: tuple[asyncio.AbstractEventLoop, SandboxKey, str], waiter: object
    ) -> None:
        """Drop one abandoned waiter, and the slot with it when nothing is left in it."""
        with self._guard:
            slot = self._slots.get(at)
            if slot is None:
                return
            slot.waiters = [held for held in slot.waiters if held[1] is not waiter]
            self._drop_if_idle(at, slot)

    def release(self, key: SandboxKey, kind: str, *, owner: str) -> None:
        """Give back what ``owner`` holds. An owner holding nothing releases nothing.

        That last sentence is the guard rather than a convenience: a cancelled or timed-out
        waiter reaches here through the same cleanup path a holder does.
        """
        at = self._at(key, kind)
        with self._guard:
            slot = self._slots.get(at)
            if slot is None:
                return
            if slot.exclusive == owner:
                slot.exclusive = None
            elif owner in slot.shared:
                slot.shared.discard(owner)
            else:
                # Never held it. Waking its waiters would be harmless; freeing the holder's
                # sandbox would not, and that is what a pair-keyed release used to do.
                return
            waiters = slot.waiters
            slot.waiters = []
            self._drop_if_idle(at, slot)
        for loop, waiter in waiters:
            # Through the waiter's own loop: the router serves more than one, and resolving a
            # future from a foreign loop is undefined rather than merely unfair.
            loop.call_soon_threadsafe(_resolve, waiter)

    def _drop_if_idle(
        self, at: tuple[asyncio.AbstractEventLoop, SandboxKey, str], slot: _Slot
    ) -> None:
        """Forget a slot nobody holds or wants. Call under the guard."""
        if slot.exclusive is None and not slot.shared and not slot.waiters:
            self._slots.pop(at, None)

    def holds(self, key: SandboxKey, kind: str, *, owner: str) -> bool:
        """Whether ``owner`` holds this pair, either way. For tests and diagnostics."""
        try:
            at = self._at(key, kind)
        except RuntimeError:
            return False
        with self._guard:
            slot = self._slots.get(at)
            return slot is not None and (slot.exclusive == owner or owner in slot.shared)


def _resolve(waiter: asyncio.Future[None]) -> None:
    """Complete a waiter unless its own call already gave up on it."""
    if not waiter.done():
        waiter.set_result(None)
