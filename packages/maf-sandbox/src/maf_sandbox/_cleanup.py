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
import weakref

from ._protocol import CLEANUP_RANK, Capability, Cleanup, SandboxKey, SandboxSpec

__all__ = [
    "QUEUED_CALL_TIMEOUT",
    "ExclusiveSlots",
    "established_cleanup",
    "needs_a_slot",
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


def needs_a_slot(rung: Cleanup) -> bool:
    """Whether a call ending on ``rung`` must hold the sandbox to itself.

    The whole of the concurrency rule, written once: everything above ``RECLAIM`` removes the
    sandbox or rewinds it, which cannot happen under a sibling.
    """
    return CLEANUP_RANK[rung] > CLEANUP_RANK[Cleanup.RECLAIM]


class ExclusiveSlots:
    """One lock per ``(key, kind)``, held for a whole call, taken only above ``RECLAIM``.

    **Per running event loop**, because an :class:`asyncio.Lock` binds to the loop that first
    waits on it and this router serves more than one.  The shape is the one
    :class:`~maf_sandbox.SandboxRouter` already uses for its disposal locks: a weak-keyed table
    of loops, each holding a weak-valued table of locks, so a lock lives exactly as long as
    something is holding or waiting on it and a contended lock never keeps its loop alive.

    Two calls in *different* loops therefore do not exclude each other, which is the same bound
    the router's disposal locks carry and the same bound the isolation scope answers: a host that
    serves one conversation from more than one loop or process raises ``min_isolation_scope`` to
    :data:`~maf_sandbox.IsolationScope.CALL`, where every call has a sandbox of its own and there
    is nothing to share.
    """

    def __init__(self) -> None:
        self._loops: weakref.WeakKeyDictionary[
            asyncio.AbstractEventLoop,
            weakref.WeakValueDictionary[tuple[SandboxKey, str], asyncio.Lock],
        ] = weakref.WeakKeyDictionary()
        # Guards the two tables only, never held across an await.
        self._guard = threading.Lock()
        # Strong references to the locks a caller is currently holding, so a weak-valued entry
        # is not collected while its holder is between `take` and `release` — which would let a
        # second call build a fresh lock and walk straight into the sandbox.
        self._held: dict[tuple[asyncio.AbstractEventLoop, SandboxKey, str], asyncio.Lock] = {}

    def _lock_for(self, key: SandboxKey, kind: str) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        with self._guard:
            per_loop: weakref.WeakValueDictionary[tuple[SandboxKey, str], asyncio.Lock] | None = (
                self._loops.get(loop)
            )
            if per_loop is None:
                per_loop = weakref.WeakValueDictionary[tuple[SandboxKey, str], asyncio.Lock]()
                self._loops[loop] = per_loop
            lock = per_loop.get((key, kind))
            if lock is None:
                lock = asyncio.Lock()
                per_loop[(key, kind)] = lock
            return lock

    async def take(self, key: SandboxKey, kind: str, *, timeout: float) -> None:
        """Hold this sandbox for the calling task, waiting out a sibling that has it.

        Raises:
            TimeoutError: a sibling held it for longer than ``timeout``. Refusing beats waiting
                for ever behind a call that is never going to return.
        """
        lock = self._lock_for(key, kind)
        loop = asyncio.get_running_loop()
        try:
            async with asyncio.timeout(timeout):
                await lock.acquire()
        except TimeoutError:
            raise TimeoutError(
                f"another call is using the sandbox for {key.scope}/{key.thread_id}/"
                f"{key.agent_dir} and did not finish within {timeout:g}s. A workload cleaned by "
                "anything stronger than a reclaim runs one call at a time in its sandbox, "
                "because a reset or a delete cannot run under a sibling."
            ) from None
        with self._guard:
            self._held[(loop, key, kind)] = lock

    def release(self, key: SandboxKey, kind: str) -> None:
        """Give the sandbox back. A pair this task does not hold is ignored, not an error."""
        loop = asyncio.get_running_loop()
        with self._guard:
            lock = self._held.pop((loop, key, kind), None)
        if lock is not None and lock.locked():
            lock.release()

    def holds(self, key: SandboxKey, kind: str) -> bool:
        """Whether this loop is holding the pair. For tests and diagnostics."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return False
        with self._guard:
            return (loop, key, kind) in self._held
