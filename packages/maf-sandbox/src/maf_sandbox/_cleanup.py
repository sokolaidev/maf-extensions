"""The cleanup ladder: which rung a call ends on, and who may run it.

Two things live here.  :func:`established_cleanup` answers what a spec and a backend *between
them* establish, which is the whole of "dispose by default" — a workload that claims nothing is
cleaned by disposal.  :class:`CleanupGate` answers *when* a rung above ``RECLAIM`` may run,
because deleting or resetting a sandbox under a call still using it is a failed call.

``docs/sandbox/tool-call.md`` carries the decision; this is the mechanism.

**The gate is loop-neutral by construction.**  A router serves more than one event loop, and an
:mod:`asyncio` primitive binds to the loop that first contends it.  So the state here is plain
data under a :class:`threading.Lock`, and a waiter registers a future on *its own* loop that the
last call out resolves through that loop's ``call_soon_threadsafe``.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum

from ._protocol import CLEANUP_RANK, Capability, Cleanup, SandboxKey, SandboxSpec

__all__ = [
    "CleanupGate",
    "PendingCleanup",
    "established_cleanup",
    "resolve_cleanup",
]


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


@dataclass(frozen=True)
class PendingCleanup:
    """One sandbox's owed cleanup, as the last call out will run it.

    Keyed by the backend and the sandbox's ``instance_id`` rather than by the key, because two
    calls under one key may have been served by two sandboxes — an adoption, a reset, a router
    selecting per spec — and running one call's rung against the other's sandbox would clean the
    wrong thing.
    """

    backend: str
    instance_id: str
    rung: Cleanup
    #: The kind whose sandbox this is, so a disposal narrows to it rather than taking a sibling
    #: kind's warm sandbox with it.
    kind: str
    #: Set when this record came from a failed reclaim or an unstopped program rather than from
    #: the routine ladder.  The rung is a disposal either way; what this changes is that the key
    #: is marked unclean, so no later call is admitted until a disposal lands.
    unclean: str | None = None

    def strongest(self, other: PendingCleanup) -> PendingCleanup:
        """Whichever of the two owes more — the stronger rung, and any unclean reason kept."""
        winner = self if CLEANUP_RANK[self.rung] >= CLEANUP_RANK[other.rung] else other
        reason = self.unclean or other.unclean
        return winner if reason == winner.unclean else replace_reason(winner, reason)


def replace_reason(record: PendingCleanup, reason: str | None) -> PendingCleanup:
    """``record`` with its unclean reason set — spelled out because the class is frozen."""
    return PendingCleanup(
        backend=record.backend,
        instance_id=record.instance_id,
        rung=record.rung,
        kind=record.kind,
        unclean=reason,
    )


class _State(StrEnum):
    """Where an entry is between admitting calls and being cleaned."""

    #: Calls are admitted and the sandbox is reused.
    SERVING = "serving"
    #: A call out has recorded a cleanup above ``RECLAIM``, so the sandbox is condemned. Calls
    #: already running finish; no new one is admitted, because handing a newcomer the first
    #: call's residue is what the default exists to stop.
    DRAINING = "draining"
    #: The count reached nought and the pending records are being run.
    CLEANING = "cleaning"


@dataclass
class _Entry:
    """The gate's state for one ``(key, kind)``. Plain data; the lock outside guards it."""

    state: _State = _State.SERVING
    in_flight: int = 0
    pending: dict[tuple[str, str], PendingCleanup] = field(
        default_factory=dict[tuple[str, str], PendingCleanup]
    )
    #: One per waiting acquire, each on the loop that registered it.
    waiters: list[tuple[asyncio.AbstractEventLoop, asyncio.Future[None]]] = field(
        default_factory=list[tuple[asyncio.AbstractEventLoop, "asyncio.Future[None]"]]
    )


class CleanupGate:
    """Counts calls in flight per ``(key, kind)`` and decides when a cleanup may run.

    A `RESET` or a `DISPOSE` runs when the count reaches nought; a ``RECLAIM`` runs at once,
    since the call directory is the call's own and no sibling is using it.

    **The zero transition is a gate, not only a number.**  A count that reached nought and then
    awaited its cleanup would leave a window in which a new call increments, acquires the warm
    sandbox through get-or-create, and has it deleted underneath it.  So the state moves to
    ``CLEANING`` before the first await, and an acquire arriving in ``DRAINING`` or ``CLEANING``
    waits rather than being served.

    **The count is per router, and that is the bound.**  Two routers over one engine, in one
    process or across replicas, each count to nought on their own and the first there deletes the
    sandbox under the other's call.  No engine here can count acquires across replicas and a
    marker inside the sandbox is the guest's to forge, so this does not pretend to a lease it
    cannot hold: cleanup above ``RECLAIM`` at
    :data:`~maf_sandbox.IsolationScope.CONVERSATION` is sound where a conversation's turns pass
    through one router at a time.  A host that cannot promise that raises
    ``min_isolation_scope`` to :data:`~maf_sandbox.IsolationScope.CALL`, where there is no
    shared sandbox to delete out from under anyone.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: dict[tuple[SandboxKey, str], _Entry] = {}

    def _entry(self, key: SandboxKey, kind: str) -> _Entry:
        """The entry for this pair, created on first use. Call under the lock."""
        return self._entries.setdefault((key, kind), _Entry())

    async def enter(self, key: SandboxKey, kind: str, *, timeout: float) -> None:
        """Admit one call, waiting out a cleanup already condemned or in flight.

        Raises:
            TimeoutError: the condemned sandbox was not cleaned within ``timeout``. Refusing is
                the safe direction: the alternative is being served a sandbox a cleanup is about
                to delete, or hanging a call forever behind a cleanup that will never land
                because whoever owed it never arrived.
        """
        while True:
            with self._lock:
                entry = self._entry(key, kind)
                if entry.state is _State.SERVING:
                    entry.in_flight += 1
                    return
                waiter: asyncio.Future[None] = asyncio.get_running_loop().create_future()
                entry.waiters.append((asyncio.get_running_loop(), waiter))
            try:
                await asyncio.wait_for(waiter, timeout)
            except TimeoutError:
                with self._lock:
                    entry = self._entry(key, kind)
                    entry.waiters = [held for held in entry.waiters if held[1] is not waiter]
                raise TimeoutError(
                    f"the sandbox for {key.scope}/{key.thread_id}/{key.agent_dir} kind {kind!r} "
                    f"is being cleaned and did not finish within {timeout:g}s, so this call was "
                    "not admitted to it. A cleanup above RECLAIM runs when the last call using "
                    "the sandbox returns; a call that acquired without ever finishing holds the "
                    "count open."
                ) from None

    def owes(self, key: SandboxKey, kind: str, record: PendingCleanup) -> None:
        """Record what one call's sandbox owes, folded with anything already owed for it.

        Condemns the entry as soon as a rung above ``RECLAIM`` is recorded: the sandbox is going
        from that moment, and admitting a third call into it while a second is still in flight
        would hand the newcomer the first call's residue under a default that promises neither.
        """
        with self._lock:
            entry = self._entry(key, kind)
            at = (record.backend, record.instance_id)
            held = entry.pending.get(at)
            entry.pending[at] = record if held is None else held.strongest(record)
            if CLEANUP_RANK[entry.pending[at].rung] > CLEANUP_RANK[Cleanup.RECLAIM]:
                if entry.state is _State.SERVING:
                    entry.state = _State.DRAINING

    def leave(self, key: SandboxKey, kind: str) -> tuple[PendingCleanup, ...]:
        """Retire one call, and return the records to run when it was the last one out.

        Empty means somebody else is still in flight, or nothing is owed.  A non-empty answer
        moves the entry to ``CLEANING`` **before the caller's first await**, which is what makes
        the zero transition a gate rather than a number.
        """
        with self._lock:
            entry = self._entry(key, kind)
            entry.in_flight = max(0, entry.in_flight - 1)
            if entry.in_flight > 0 or not entry.pending:
                if entry.in_flight == 0 and not entry.pending:
                    self._release(key, kind, entry)
                return ()
            entry.state = _State.CLEANING
            return tuple(entry.pending.values())

    def cleaned(self, key: SandboxKey, kind: str) -> None:
        """Every record landed: forget the entry and wake whoever was waiting for it.

        Called whether the records succeeded or not.  A cleanup that failed has already marked
        the key unclean, and that refusal — not a waiter left hanging — is what keeps the next
        call out.
        """
        with self._lock:
            entry = self._entries.get((key, kind))
            if entry is None:
                return
            entry.pending.clear()
            self._release(key, kind, entry)

    def _release(self, key: SandboxKey, kind: str, entry: _Entry) -> None:
        """Drop the entry and resolve its waiters. Call under the lock."""
        waiters = entry.waiters
        entry.waiters = []
        self._entries.pop((key, kind), None)
        for loop, waiter in waiters:
            # Through the waiter's own loop: the router serves more than one, and resolving a
            # future from a foreign loop is undefined rather than merely unfair.
            loop.call_soon_threadsafe(_resolve, waiter)

    def in_flight(self, key: SandboxKey, kind: str) -> int:
        """How many calls are using this pair's sandbox right now. For tests and diagnostics."""
        with self._lock:
            entry = self._entries.get((key, kind))
            return 0 if entry is None else entry.in_flight

    def forget(self, keys: Iterable[SandboxKey]) -> None:
        """Drop every entry for these keys, waking anyone waiting.

        What a scope purge and a hand disposal owe: the sandboxes are gone, so a waiter blocked
        on a cleanup that will now never run would wait out its whole timeout for nothing.
        """
        with self._lock:
            for key, kind in [pair for pair in self._entries if pair[0] in set(keys)]:
                entry = self._entries[(key, kind)]
                entry.pending.clear()
                self._release(key, kind, entry)


def _resolve(waiter: asyncio.Future[None]) -> None:
    """Complete a waiter unless its own call already gave up on it."""
    if not waiter.done():
        waiter.set_result(None)
