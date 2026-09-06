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

**Two join columns, and they answer different questions.**  A :class:`~maf_sandbox.SandboxKey`
says which sandbox and which conversation, and at the default
:data:`~maf_sandbox.IsolationScope.CONVERSATION` it says nothing about a call: two calls in
flight on one thread carry the same key.  ``call`` is the other column — the id of the tool call
a record came from, so those two calls' records separate.  :class:`ToolCallEnded` always names
one, since it is where a call's other events join; the rest name none for what happened outside
a call — before one, which a disposal genuinely can, and after one, which a task the body left
running does.

**An observer is synchronous, thread-safe, and it cannot fail a call.**  It runs wherever the
call it records is served — an event-loop task, or the worker thread a synchronous tool body
runs on — so a blocking one blocks that call, and one shared by two calls is entered from two
threads at once.  Every delivery goes through :func:`record`, which contains whatever the
observer does with it: the failure is logged and the call runs on.

There are two registration points, because there are two host-policy objects:
:class:`~maf_sandbox.SandboxRouter` owns the sandbox lifecycle, and
:class:`~maf_sandbox.HostToolRegistry` owns what a guest may call back into.  A host that wires
one is not obliged to wire the other.  :func:`~maf_sandbox.collect_outputs` is neither — it is a
function a kind calls per collection, so it takes the observer and the key as arguments.

A backend is not a third registration point.  :class:`SandboxAcquired` records the mode and the
allowlist a sandbox was *served* under — what its spec asked for — and :class:`EgressObserved`
records what the guest then reached, which only the thing enforcing egress knows.  A backend
that can read its own enforcement implements :class:`ObservesEgress` and is handed a reporter
by the router it is registered on, so the host still wires exactly one observer.  A backend that
enforces in a service it does not run reports nothing, and says so through
:attr:`~maf_sandbox.BackendDeclarations.observes_egress` rather than by being silent.
"""

from __future__ import annotations

import inspect
import logging
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable

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
    "DisposalReport",
    "EgressDecision",
    "EgressDecisionCode",
    "EgressObserved",
    "EgressReporter",
    "HostToolCalled",
    "HostToolOutcome",
    "LandedOutput",
    "ObservesEgress",
    "OutputsCollected",
    "SandboxAcquired",
    "SandboxDisposed",
    "SandboxEvent",
    "SandboxObserver",
    "ScopeDisposed",
    "StoreFileRead",
    "StoreReadOutcome",
    "ToolCallEnded",
    "record",
    "refuse_an_unusable_observer",
]


@dataclass
class RecordedCall:
    """One tool call, as the sites that build events see it: an id, and whether it is still open.

    **Mutable, and that is the whole of it.**  A task starts from a copy of its parent's context,
    so a child the body left running keeps whatever :data:`RECORDED_CALL` held when it started —
    and resetting a bare id in the wrapper would not reach that copy.  A store read or an acquire
    from such a task would go on naming a call whose :class:`ToolCallEnded` has already been
    delivered, and whose ``keys`` can no longer account for what it touched.  ``closed`` is the
    one piece of this the copies share, which is why it is what a reader tests.
    """

    id: str
    closed: bool = False


#: The tool call whose records are being written here, or ``None`` outside one.
#:
#: Set by :func:`~maf_sandbox.maf.sandboxed_tool` around the body *and* its reclaim, and read by
#: every site that builds an event.  A `ContextVar` rather than an argument threaded through
#: :meth:`~maf_sandbox.SandboxRouter.acquire`, because the acquire and the disposal are on the
#: far side of the router's own boundary: the router knows nothing about ``sandboxed_tool`` and
#: must not start to.  It already imports this module to record at all, so reading one more
#: thing from the seam that owns the events adds no coupling that was not there.
#:
#: Read it through :func:`recorded_call`, never off the variable: a live record and a closed
#: one are both a :class:`RecordedCall`, and only the function tells them apart.
RECORDED_CALL: ContextVar[RecordedCall | None] = ContextVar(
    "maf_sandbox_recorded_call", default=None
)


def call_id_of(recorded: RecordedCall | None) -> str | None:
    """``recorded``'s id while its call is open, and ``None`` before one starts or once it ends.

    Held apart from :func:`recorded_call` for the one caller that cannot read the context where
    it records: a :class:`~maf_sandbox.HostToolRun` is built inside its call and used from the
    transport's own tasks, so it keeps the record and asks this.
    """
    return None if recorded is None or recorded.closed else recorded.id


def recorded_call() -> str | None:
    """The tool call whose records are being written here, or ``None`` outside one."""
    return call_id_of(RECORDED_CALL.get())


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
    handed over whole.  All three of ``backend``, ``isolation`` and ``declarations`` are ``None``
    for a refusal that landed before a backend was chosen.

    ``isolation`` and ``declarations`` are **also** ``None``, with ``backend`` set, where a
    backend was chosen and its declarations could not be read back for the record: those are
    property calls into somebody else's class, and an acquire must not start failing over the
    record of it.  So ``backend is None`` is the test for "routing never selected one", and a
    ``None`` beside a named backend is a degraded read of a sandbox that was served.

    ``call`` is ``None`` for an acquire a direct consumer of the router asked for, outside any
    tool call.
    """

    key: SandboxKey
    spec: SandboxSpec
    isolation_scope: IsolationScope
    backend: str | None
    isolation: Isolation | None
    declarations: BackendDeclarations | None
    seconds: float
    refusal: str | None = None
    #: The tool call this acquire was asked from — see :data:`RECORDED_CALL`.  Appended after
    #: ``refusal``, which already had a default, so it cannot rebind a positional caller's
    #: argument.
    call: str | None = None

    def deliver_to(self, observer: SandboxObserver) -> None:
        observer.sandbox_acquired(self)


#: How a disposal ended, as far as anyone can tell.  ``"gone"`` is a backend reporting no
#: failure — which the protocol reads as disposed, and which a backend with no way to check
#: also answers.  ``"may_remain"`` is a backend naming a failure.  ``"unknown"`` is a disposal
#: that never answered, where the delete may equally have landed.
DisposalReport = Literal["gone", "may_remain", "unknown"]


@dataclass(frozen=True)
class SandboxDisposed(SandboxEvent):
    """One backend's answer to one disposal — what it said, and how sure that makes anyone.

    One per backend asked, because a disposal fans out across every registered backend and each
    answers for itself.  ``failure`` carries the code a caller branches on beside the backend's
    own sentence, which is for a log rather than for parsing.

    ``outcome`` is three values rather than a flag because **a cleanup audit must not be told
    more than the protocol knows.**  :meth:`~maf_sandbox.SandboxBackend.dispose` answers
    ``None`` both for a delete it verified and for one it has no way to check — a conflation it
    documents and chooses — so ``"gone"`` is what the backend *reported*, not proof.
    ``"may_remain"`` is a backend naming a failure, and ``"unknown"`` is a disposal that never
    answered at all, where the delete may equally have completed.  A boolean here read the
    second and third as settled facts in opposite directions.

    ``call`` is the one event where its absence is ordinary rather than a gap: a disposal the
    reclaim asks for on a call's way out names that call, while a scope purge, a framework
    reclaim and a host disposing by hand happen outside any call and name none.
    """

    key: SandboxKey
    backend: str
    outcome: DisposalReport
    failure: DisposalFailure | None
    seconds: float
    #: The tool call this disposal ran inside — see :data:`RECORDED_CALL`.
    call: str | None = None

    def deliver_to(self, observer: SandboxObserver) -> None:
        observer.sandbox_disposed(self)


@dataclass(frozen=True)
class ScopeDisposed(SandboxEvent):
    """One backend's answer to one conversation's purge — how many went, and what stayed.

    The routine cleanup: :meth:`~maf_sandbox.SandboxRouter.dispose_scope` is what a thread
    deletion runs and what :meth:`~maf_sandbox.SandboxRouter.scope` runs when its block ends.
    One event per backend asked, because a purge fans out the same way a disposal does.

    **It is keyed on a conversation rather than on a sandbox**, which is why
    :class:`SandboxDisposed` cannot carry it: :meth:`~maf_sandbox.SandboxBackend.dispose_scope`
    answers with a count rather than with the keys it removed, so there is no key to put here.
    A recorder joins this to the rest on ``(scope, thread_id)``.

    ``disposed`` is what **this** backend reported removing, not the sweep's running total, and
    it is zero for a backend that raised and for one an interruption took — where zero is the
    absence of an answer rather than a report that nothing was there.  ``outcome`` is what
    separates those from a backend that genuinely had nothing to remove, and it reads exactly
    as :class:`SandboxDisposed`'s does.

    **What the purge did to the unclean ledger is not a field here.**  A purge every backend
    answered cleanly reopens the conversation's refused keys, and that is one state change for
    the whole purge rather than one per backend, so the number of keys it reopened is not
    recorded.  Every backend's ``outcome`` reading ``"gone"`` is the condition the ledger is
    cleared on, which is as close as these events come to stating it.
    """

    scope: str
    thread_id: str
    backend: str
    outcome: DisposalReport
    disposed: int
    failure: DisposalFailure | None
    seconds: float
    #: The tool call this purge ran inside — see :data:`RECORDED_CALL`.  ``None`` for both of
    #: the ordinary callers, a thread deletion and a ``scope`` block closing, since neither is
    #: inside a call.
    call: str | None = None

    def deliver_to(self, observer: SandboxObserver) -> None:
        observer.scope_disposed(self)


#: What an egress enforcer did with one CONNECT.  Both refusals are kept apart because they
#: refuse different things: ``"DENY"`` is a host absent from the spec's allowlist, and
#: ``"DENY-NONGLOBAL"`` is an allowlisted host that resolved to a private address — the shape a
#: guest reaching back at the host's own services takes, which an allowlist alone does not
#: catch.  ``"UNREACHABLE"`` *allowed* the tunnel and then failed to open it, so it belongs with
#: the permitted attempts rather than the refused ones.
EgressDecisionCode = Literal["ALLOW", "DENY", "DENY-NONGLOBAL", "UNREACHABLE"]


@dataclass(frozen=True)
class EgressDecision:
    """One CONNECT an egress enforcer answered.

    ``host`` is what the *guest* asked for rather than what the spec allowed — on a ``DENY`` it
    is a name the guest chose, and a recorder holds it to the same rule an artifact name is
    held to.
    """

    decision: EgressDecisionCode
    host: str
    port: int


@dataclass(frozen=True)
class EgressObserved(SandboxEvent):
    """What one sandbox's egress enforcement decided, keyed to the sandbox that caused it.

    This is the event that separates what a spec *allowed* from what a guest *attempted*.
    :class:`SandboxAcquired` carries the mode and the allowlist a sandbox was served under;
    this carries every ``CONNECT`` the enforcer answered and how it answered.  Only an
    ``ALLOW`` opened a tunnel — a ``DENY`` names a host the guest asked for and did not get
    — so a reader counting reached destinations filters on the verb rather than on the
    presence of a decision.

    **It arrives in batches, after the fact.**  A backend enforcing egress in a proxy of its own
    reads that proxy's record when it takes the proxy down, so one event covers a window rather
    than a request, and the decisions inside it are ordered as the enforcer wrote them and carry
    no timestamps of their own.  A backend enforcing in a service it does not run emits nothing
    at all — see :attr:`~maf_sandbox.BackendDeclarations.observes_egress`, which is what stops
    an empty record reading as a clean one.

    ``truncated`` says the window **may** be short of the bound's worth: a guest makes as many
    requests as it likes, so a drain reads a bounded tail and cannot tell a log that held one
    line more from one that held ten thousand.  It is a flag rather than a count because no
    enforcer read this way can say how many it withheld, and it errs towards *there may be
    more* — the direction a record is allowed to be wrong in.

    ``unreadable`` names why a drain came back with nothing where the enforcer was expected to
    have written something.  A window nobody can account for is exactly what an operator needs
    to see, so it is a field on an event rather than a line in a log.
    """

    key: SandboxKey
    backend: str
    decisions: tuple[EgressDecision, ...]
    truncated: bool
    unreadable: str | None
    seconds: float
    #: Always ``None``, and typed so that saying otherwise does not compile: a drain covers a
    #: *window* rather than a call, and the decisions in it span whatever calls happened
    #: between two removals.  :func:`recorded_call` would name the call that happened to
    #: collect them, which is the one thing this record must not say.  It stays a field so a
    #: recorder reading ``call`` across the events has no case to special-case.
    call: Literal[None] = None

    def deliver_to(self, observer: SandboxObserver) -> None:
        observer.egress_observed(self)


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

    The call is found where the :class:`~maf_sandbox.HostToolRun` was built rather than per
    record, because a guest's callback is served on a task of the transport's own, whose context
    is a copy rather than the body's.  ``call`` is ``None`` for a run built outside a tool call,
    and for one still answering after its call has ended.
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
    #: The tool call this run belongs to — see :data:`RECORDED_CALL`.
    call: str | None = None

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
    #: The tool call that read — see :data:`RECORDED_CALL`.
    call: str | None = None

    def deliver_to(self, observer: SandboxObserver) -> None:
        observer.store_file_read(self)


@dataclass(frozen=True)
class LandedOutput:
    """One artifact a collection delivered to the host's sink.

    ``name`` is the one the sink reported landing under, which is what
    :func:`~maf_sandbox.collect_outputs` returns and which a content-addressed sink need not
    spell as the declaration did.  ``size_bytes`` is what was read out of the sandbox.
    """

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
    ``per_call`` sink names the folder they landed in.  ``key`` reaches the conversation, and
    this reaches the folder.

    ``call`` is the separate question of which call collected, which this seam answers for
    itself rather than trusting a kind's argument for: a kind passing its own call's id spells
    the two the same, and one passing none — or a meaning of its own — still gets a record that
    joins.
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
    #: The tool call that collected — see :data:`RECORDED_CALL`.
    call: str | None = None

    def deliver_to(self, observer: SandboxObserver) -> None:
        observer.outputs_collected(self)


@dataclass(frozen=True)
class ToolCallEnded(SandboxEvent):
    """One sandboxed tool call, from the body's first line to the end of its reclaim.

    ``seconds`` covers the body *and* the removal the caller waits for, which is what the call
    actually cost.  For a body that awaits nothing, this record is delivered on the worker
    thread the framework ran the body on.

    **What each column joins to.**  ``keys`` associates the call with the sandboxes and the
    conversation it touched — not with *this call in particular*, since at the default
    :data:`~maf_sandbox.IsolationScope.CONVERSATION` a key carries no ``call_id`` and two calls
    running at once in one conversation carry the same one.  ``call`` is what separates them,
    and this is the one event where it is never ``None``: the record is where a call's other
    events join, so an anchor that could be absent would be no anchor.  It is the same id the
    call's own guest path and — at :data:`~maf_sandbox.IsolationScope.CALL` — its key are named
    by, so a recorder has one string for the call rather than two.

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
    #: This call's own id, which every event it emitted carries — see :data:`RECORDED_CALL`.
    call: str

    def deliver_to(self, observer: SandboxObserver) -> None:
        observer.tool_call_ended(self)


class SandboxObserver:
    """A host's record of what a sandbox did.  Subclass it and override what you want.

    Every method does nothing, and that is load-bearing rather than a convenience: this seam
    gains events as the suite learns to see more, and a host that already ships an observer must
    not stop type-checking — or start raising ``AttributeError`` — when it does.  A ``Protocol``
    would hand a structural implementer exactly that, so this is a class a host inherits from,
    and both registration points refuse anything that is not one.

    **Synchronous, thread-safe, and fast.**  Each method runs wherever the call it records is
    served: on the event-loop task for a tool body that awaits, and on the worker thread the
    framework gives a body that does not — so one observer is entered from two threads at
    once, and blocking in it blocks the call.  Hand the event to something built for that, a
    ``queue.Queue`` or an exporter that batches on a thread of its own; an ``asyncio.Queue``
    is not, and does not wake its reader from another thread.  An ``async def`` override is
    refused where the observer is registered rather than left as a coroutine nothing awaits.

    Failing is allowed and costs nothing but a warning — see :func:`record`.  An observer is
    never given the chance to change what a call returns.
    """

    def sandbox_acquired(self, event: SandboxAcquired) -> None:
        """A key was served a sandbox, or refused one."""

    def sandbox_disposed(self, event: SandboxDisposed) -> None:
        """One backend answered one disposal."""

    def scope_disposed(self, event: ScopeDisposed) -> None:
        """One backend answered one conversation's purge."""

    def egress_observed(self, event: EgressObserved) -> None:
        """A backend read what its egress enforcement decided for one sandbox."""

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
    "scope_disposed",
    "egress_observed",
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
            "observer is called synchronously where the call is served and nothing awaits it, "
            "so the coroutine would be collected unawaited and the event lost. Put the event on "
            "a thread-safe queue, or hand it to an exporter that batches on a thread of its own."
        )
    return observer


def record(observer: SandboxObserver | None, event: SandboxEvent, logger: logging.Logger) -> None:
    """Hand ``event`` to ``observer``, containing whatever it does with it.

    An observer is the host's code inside the call, so none of its failures may reach it: each
    is logged and the caller runs on.  ``SystemExit`` and ``KeyboardInterrupt`` are
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


#: What a backend reports an :class:`EgressObserved` through.  The router hands one over
#: already wrapped in :func:`record`, so a backend never holds the host's observer and never has
#: to contain its failures — calling this is safe from anywhere, including a cleanup path.
EgressReporter = Callable[[EgressObserved], None]


@runtime_checkable
class ObservesEgress(Protocol):
    """A backend that can say what its egress enforcement actually decided.

    Implementing this is a claim a backend has to be able to keep: that it enforces egress
    somewhere it can read afterwards.  A backend enforcing in a service it does not run cannot,
    and does not implement it — which is a different thing from having nothing to report, and
    :attr:`~maf_sandbox.BackendDeclarations.observes_egress` is where the difference is written
    down for a reader who only ever sees the records.

    The router calls :meth:`observe_egress` once, at the end of its construction, with its
    reporter or with ``None``.  ``None`` is what an *unobserved* router passes, and it is not
    the same as not calling: a backend instance may be registered on more than one router, so a
    router that collects nothing has to be able to switch a backend off rather than leave it
    reporting to whoever wired it last.  A backend does no reading at all until it holds a
    reporter, because reading a proxy's record costs an engine round trip per acquire and an
    uninstrumented deployment does not pay it.

    **One backend reports to one router — the last one constructed over it.**  The callback is a
    single slot, so a host that registers one backend on two *observed* routers gets that
    backend's records on whichever was built second, including for sandboxes the other served.
    The seam does not support that arrangement and cannot detect it from here; a backend replacing
    a live reporter with a different one should say so, which is what both shipped backends do.
    """

    def observe_egress(self, report: EgressReporter | None) -> EgressReporter | None:
        """Take the callback to report egress decisions through, and return the one it replaced.

        Returning the old one is what lets a router that fails to construct put a backend back
        as it found it, rather than switching off reporting a *different* router is still using.

        **It must be atomic.**  Either it takes the reporter and returns the previous one, or
        it raises having changed nothing.  A hook that stores the new one and then raises
        cannot be rolled back — the caller never received what it replaced — and the router's
        restore would put back the wrong thing.
        """
        ...
