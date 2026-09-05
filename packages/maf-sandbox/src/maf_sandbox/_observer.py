"""What a sandbox did, as events a host can record.

This package logs; it does not export.  A deployment asked *which conversation reached host X,
which host tools ran under whose authority, what crossed the boundary and with what label* has
to answer from records that share a key, and a `logging` line is neither structured nor keyed.
This module is the seam that hands those facts over: a :class:`SandboxObserver` a host
registers, and one frozen event per thing that happened.

Every event is written in this package's own vocabulary — a :class:`~maf_sandbox.SandboxKey`, a
:class:`~maf_sandbox.SandboxSpec`, a :class:`~maf_sandbox.SourceIntegrity` — and never in a
telemetry one.  Nothing here imports an exporter and nothing here redacts.  An observer runs in
the host's own process, so what reaches a wire, and whether a guest-chosen artifact name is on
it, is the recorder's decision; this seam's duty is to hand over what happened without deciding
that for it.

**An observer is synchronous, and it cannot fail a call.**  It runs on the task serving the tool
call, so a blocking one blocks the call it is recording.  Every delivery goes through
:func:`record`, which contains whatever the observer does with it: the failure is logged and the
call runs on.

There are two registration points, because there are two host-policy objects:
:class:`~maf_sandbox.SandboxRouter` owns the sandbox lifecycle, and
:class:`~maf_sandbox.HostToolRegistry` owns what a guest may call back into.  A host that wires
one is not obliged to wire the other.  :func:`~maf_sandbox.collect_outputs` is neither — it is a
function a kind calls per collection, so it takes the observer and the key as arguments.

What this seam does **not** see is the egress proxy's own decisions.  A docker or wslc proxy
prints its ``ALLOW``/``DENY`` lines inside its own container, and the backend reads that stream
once, at acquire; ACAS enforces egress in the service.  :class:`SandboxAcquired` records the
mode and the allowlist a sandbox was *served* under, which is what a spec asked for rather than
what a guest then reached.
"""

from __future__ import annotations

import inspect
import logging
from dataclasses import dataclass
from typing import Literal

from ._containment import CONTAINED, escapes_containment
from ._error_detail import error_detail
from ._protocol import (
    BackendDeclarations,
    DisposalFailure,
    Identity,
    Isolation,
    IsolationScope,
    SandboxKey,
    SandboxSpec,
    SourceIntegrity,
    TransferLimits,
)

__all__ = [
    "EVENT_METHODS",
    "HostToolCalled",
    "HostToolOutcome",
    "LandedOutput",
    "OutputsCollected",
    "SandboxAcquired",
    "SandboxDisposed",
    "SandboxEvent",
    "SandboxObserver",
    "StoreFileRead",
    "StoreReadOutcome",
    "ToolCallEnded",
    "record",
    "refuse_an_unusable_observer",
]


@dataclass(frozen=True)
class SandboxEvent:
    """One thing that happened, on its way to a host's observer.

    Subclasses carry the facts; :meth:`deliver_to` is how each one finds the method that answers
    for it, so nothing dispatches on an event's type anywhere else.
    """

    def deliver_to(self, observer: SandboxObserver) -> None:
        """Hand this event to the :class:`SandboxObserver` method that answers for it."""
        raise NotImplementedError


@dataclass(frozen=True)
class SandboxAcquired(SandboxEvent):
    """One acquire: the posture a key was served under, or the refusal that stopped it.

    The whole ``spec`` rather than a projection of it, because which of its fields a record
    needs is the recorder's question and not this package's — and a field added to
    :class:`~maf_sandbox.SandboxSpec` then reaches an existing recorder without a change here.
    ``isolation_scope`` sits beside it because it is the *resolved* one, which the spec alone
    does not answer: the host's floor can raise it.

    ``refusal`` is the class name of what was raised and nothing else of it — a refusal's
    message is what carries a backend's endpoint or an SDK's response body, and this record is
    handed over whole.  ``backend``, ``isolation`` and ``declarations`` are ``None`` for a
    refusal that landed before a backend was chosen.
    """

    key: SandboxKey
    spec: SandboxSpec
    isolation_scope: IsolationScope
    backend: str | None
    isolation: Isolation | None
    declarations: BackendDeclarations | None
    seconds: float
    refusal: str | None = None

    def deliver_to(self, observer: SandboxObserver) -> None:
        observer.sandbox_acquired(self)


@dataclass(frozen=True)
class SandboxDisposed(SandboxEvent):
    """One backend's answer to one disposal — the outcome, whether or not it failed.

    One per backend asked, because a disposal fans out across every registered backend and each
    answers for itself.  ``failure`` carries the code a caller branches on beside the backend's
    own sentence, which is for a log rather than for parsing.
    """

    key: SandboxKey
    backend: str
    landed: bool
    failure: DisposalFailure | None
    seconds: float

    def deliver_to(self, observer: SandboxObserver) -> None:
        observer.sandbox_disposed(self)


#: How one host-tool call ended.  ``"refused"`` covers every sentence the guest was answered
#: with — an exhausted cap, an unregistered name, a tool body that raised — because those are
#: one shape at the door, and ``refusal`` says which.  ``"failed"`` is the door itself breaking,
#: and ``"cancelled"`` is a call taken by a cancel, which may have left an outward effect behind.
HostToolOutcome = Literal["delivered", "refused", "cancelled", "failed"]


@dataclass(frozen=True)
class HostToolCalled(SandboxEvent):
    """One host-tool call: what was called, under what declaration, and what it cost.

    ``tool`` is the name a call **resolved** to, which is a key the host registered rather than
    guest text.  It is ``None`` wherever a call never got that far: a name that is not
    registered, one that arrived as something other than a string, and the cap refusal, which
    fires before the name is looked at.  The guest's own spelling is never recorded — the only
    place it survives is a bounded copy inside ``refusal``.

    The three declaration legs are flat rather than a
    :class:`~maf_sandbox.HostToolDeclaration`, because ``declared`` has to be readable on its
    own: an unstamped tool is not a weaker declaration but none at all, and it already fails
    safe into an untrusted source under :data:`~maf_sandbox.Identity.APP`.

    ``refusal`` is the sanitized sentence the guest was answered with.  It is safe for a
    transcript and so safe for a record, but it is not purely host vocabulary: the two refusals
    that fire before a name resolves quote a bounded copy of what the guest asked for.

    ``key`` is the sandbox the run belongs to, where the transport was given one.  ``calls`` is
    how many calls this run has made including this one, so a run that spent its cap is visible
    without differencing.  ``response_bytes`` is what this call delivered, framing included, and
    zero for everything else.
    """

    run_id: str
    key: SandboxKey | None
    tool: str | None
    declared: bool
    source: SourceIntegrity | None
    sink: str | None
    identity: Identity | None
    outcome: HostToolOutcome
    refusal: str | None
    response_bytes: int
    calls: int
    seconds: float

    def deliver_to(self, observer: SandboxObserver) -> None:
        observer.host_tool_called(self)


#: What became of one store read.  ``"read"`` is text crossing, empty text included;
#: ``"absent"`` is the store answering that there is no such file; ``"refused"`` is no answer
#: at all — it raised, or a cancel took the read away.
StoreReadOutcome = Literal["read", "absent", "refused"]


@dataclass(frozen=True)
class StoreFileRead(SandboxEvent):
    """One file a call read out of the host's store, and what the read said it was worth.

    ``integrity`` is what :meth:`~maf_sandbox.maf.SandboxToolSession.read_file` answered with
    after folding the listing's label with the host's record across the read — ``None`` where
    nothing is established, which is not a synonym for untrusted.  ``name`` is the *host's*
    listing key, never the model's spelling of it.

    ``characters`` is the length of the text that came back, not a byte count: the store answers
    with ``str`` and nothing here encodes it to find out.  It is zero for a read that was
    refused, and for one whose file had gone.

    ``outcome`` is what became of the read, and it is three values rather than a flag because
    the three are genuinely different facts: ``"read"`` means text crossed — possibly empty
    text, which is why a length cannot stand in for this — ``"absent"`` means the store
    answered that there is no such file, and ``"refused"`` means it did not answer at all,
    because it raised or a cancel took the read away. A record that could not tell an empty
    file from a missing one could not answer whether anything crossed the boundary, which is
    the question it exists for.
    """

    key: SandboxKey | None
    tool: str
    name: str
    integrity: SourceIntegrity | None
    characters: int
    outcome: StoreReadOutcome

    def deliver_to(self, observer: SandboxObserver) -> None:
        observer.store_file_read(self)


@dataclass(frozen=True)
class LandedOutput:
    """One artifact a collection delivered to the host's sink."""

    name: str
    size_bytes: int
    media_type: str | None


@dataclass(frozen=True)
class OutputsCollected(SandboxEvent):
    """One collection: what was declared, what landed, and under which caps.

    ``declared`` counts the spec's outputs and the call-time ones together, which is what
    ``limits`` bounds; ``landed`` holds only the artifacts a sink accepted, so a ``CONSUME``
    output is in the count and not in the list.  ``refusal`` is the class name of what the
    collection raised, and a refusal part-way still reports the artifacts already delivered —
    a ``deliver`` is a push nothing takes back.

    ``call_id`` is what the collection was given to stamp on each artifact, which for a
    ``per_call`` sink names the folder they landed in.  ``key`` reaches the conversation; this
    reaches the folder.
    """

    key: SandboxKey | None
    kind: str
    declared: int
    limits: TransferLimits
    landed: tuple[LandedOutput, ...]
    seconds: float
    refusal: str | None = None
    #: Appended after ``refusal``, which already had a default, so it cannot rebind a
    #: positional caller's argument.
    call_id: str | None = None

    def deliver_to(self, observer: SandboxObserver) -> None:
        observer.outputs_collected(self)


@dataclass(frozen=True)
class ToolCallEnded(SandboxEvent):
    """One sandboxed tool call, from the body's first line to the end of its reclaim.

    ``seconds`` covers the body *and* the removal the caller waits for, which is what the call
    actually cost.

    **What this joins to, and what it does not.**  ``keys`` associates the call with the
    sandboxes and the conversation it touched — not with *this call in particular*.  At the
    default :data:`~maf_sandbox.IsolationScope.CONVERSATION` a key carries no ``call_id``, so
    two calls running at once in one conversation carry the same one and their records
    interleave with nothing to tell them apart.  A recorder that needs per-call correlation has
    the framework's own span context, or waits for
    `#922 <https://github.com/sokolaidev/maf-extensions/issues/922>`_.

    ``keys`` is every key the call **touched**, in order, and empty for one that touched none.
    Touched rather than acquired: a refused acquire is named here, so its own
    :class:`SandboxAcquired` has a call to join to, and so is a key the call only read the
    store under — a kind may read and return before it ever acquires, which is a normal failure
    path rather than an edge.  A recorder wanting only what was served reads the acquire records
    rather than filtering this.  A tuple because one call may reach more than one sandbox.

    ``failure`` is the class name of what the **body** raised, or ``None`` where it returned. It
    is read before the reclaim runs, so a reclaim that raises on its way out does not overwrite
    what the body did; the reclaim's own trouble arrives as ``unclean`` and, where a disposal
    was asked for, as :class:`SandboxDisposed`.  ``unclean`` counts what a transport noted about
    the sandbox during the call — a stop that did not reach everything a program started.
    """

    tool: str
    kind: str
    keys: tuple[SandboxKey, ...]
    seconds: float
    failure: str | None
    unclean: int

    def deliver_to(self, observer: SandboxObserver) -> None:
        observer.tool_call_ended(self)


class SandboxObserver:
    """A host's record of what a sandbox did.  Subclass it and override what you want.

    Every method does nothing, and that is load-bearing rather than a convenience: this seam
    gains events as the suite learns to see more, and a host that already ships an observer must
    not stop type-checking — or start raising ``AttributeError`` — when it does.  A ``Protocol``
    would hand a structural implementer exactly that, so this is a class a host inherits from,
    and both registration points refuse anything that is not one.

    **Synchronous, and fast.**  Each method runs on the task serving the tool call, so an
    observer that blocks blocks the call, and an ``async def`` override is refused where the
    observer is registered rather than left as a coroutine nothing awaits.  Put the event on a
    queue, or hand it to an exporter that batches on a thread of its own; do not do the I/O
    here.

    Failing is allowed and costs nothing but a warning — see :func:`record`.  An observer is
    never given the chance to change what a call returns.
    """

    def sandbox_acquired(self, event: SandboxAcquired) -> None:
        """A key was served a sandbox, or refused one."""

    def sandbox_disposed(self, event: SandboxDisposed) -> None:
        """One backend answered one disposal."""

    def host_tool_called(self, event: HostToolCalled) -> None:
        """A guest program called back into the host."""

    def store_file_read(self, event: StoreFileRead) -> None:
        """A call read a file out of the host's store."""

    def outputs_collected(self, event: OutputsCollected) -> None:
        """A collection pulled a spec's declared outputs."""

    def tool_call_ended(self, event: ToolCallEnded) -> None:
        """A sandboxed tool call returned, and its reclaim finished."""


#: Every event method on :class:`SandboxObserver`, written down once so the registration check
#: and the exhaustiveness test read the same list.
EVENT_METHODS: tuple[str, ...] = (
    "sandbox_acquired",
    "sandbox_disposed",
    "host_tool_called",
    "store_file_read",
    "outputs_collected",
    "tool_call_ended",
)


def _awaits(handler: object) -> bool:
    """Whether calling ``handler`` gives something to await.

    An instance with an async ``__call__`` is as awaitable as a coroutine function, and only its
    ``__call__`` is the coroutine function :mod:`inspect` can see.
    """
    return inspect.iscoroutinefunction(handler) or inspect.iscoroutinefunction(
        getattr(handler, "__call__", None)
    )


def refuse_an_unusable_observer(observer: object, *, argument: str) -> SandboxObserver:
    """Return ``observer`` if it can be recorded to, and raise otherwise.

    Both failures are configuration mistakes with no call-time symptom worth waiting for: a
    plain object answers no event at all, and an ``async def`` override returns a coroutine
    nothing awaits, which surfaces as a warning from whichever task happens to collect it.

    Raises:
        TypeError: when ``observer`` is not a :class:`SandboxObserver`, or when it overrides an
            event method with a coroutine function.
    """
    if not isinstance(observer, SandboxObserver):
        raise TypeError(
            f"{argument} must be a SandboxObserver, not {type(observer).__name__}. Subclass it "
            "and override the events you want: the base class answers every event with nothing, "
            "which is what lets a later release add one without breaking an observer written "
            "today."
        )
    asynchronous = [name for name in EVENT_METHODS if _awaits(getattr(observer, name, None))]
    if asynchronous:
        raise TypeError(
            f"{argument} overrides {', '.join(asynchronous)} with a coroutine function. An "
            "observer runs on the task serving the tool call and nothing awaits it, so the "
            "coroutine would be collected unawaited and the event lost. Put the event on a "
            "queue, or hand it to an exporter that batches on a thread of its own."
        )
    return observer


def record(observer: SandboxObserver | None, event: SandboxEvent, logger: logging.Logger) -> None:
    """Hand ``event`` to ``observer``, containing whatever it does with it.

    An observer is the host's code on the call's own task, so none of its failures may reach the
    call: each is logged and the caller runs on.  ``SystemExit`` and ``KeyboardInterrupt`` are
    the host's control flow rather than an observer failure, so they escape — including when
    one arrives as a leaf of a group, which is why the group is unwrapped rather than trusted
    for being one.
    """
    if observer is None:
        return
    try:
        event.deliver_to(observer)
    except CONTAINED as exc:  # noqa: BLE001 - a call is not the observer's to fail
        if escapes_containment(exc):
            raise
        logger.warning(
            "sandbox observer: %s was not recorded: %s", type(event).__name__, error_detail(exc)
        )
