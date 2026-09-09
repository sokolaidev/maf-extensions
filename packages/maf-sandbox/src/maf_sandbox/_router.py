"""Backend selection: the layer between a host application and any sandbox provider.

``app -> SandboxRouter -> backend -> the sandbox itself``.  The router owns what no
individual backend can own: **which** backend serves a request, and the rules that decide
whether it may — a minimum-isolation floor, a capability match, the guest's shape, the
transfer ceilings, the egress rule, the scope one sandbox may serve, and the host's outright
denials (capabilities and identities this posture refuses whatever the backend could do).
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import inspect
import logging
import math
import threading
import time
import weakref
from collections.abc import AsyncGenerator, Iterable, Sequence
from contextlib import asynccontextmanager
from enum import StrEnum
from typing import cast

from ._cleanup import (
    QUEUED_CALL_TIMEOUT,
    ExclusiveSlots,
    established_cleanup,
    needs_exclusive_use,
    resolve_cleanup,
)
from ._containment import CONTAINED, escapes_containment
from ._effective_state import (
    EffectiveState,
    effective_state_is_noted,
    note_effective_state,
)
from ._error_detail import error_detail
from ._host_tools_over_exec import fold_host_tool_call_transfer_limits
from ._observer import (
    DisposalReport,
    EgressObserved,
    EgressReporter,
    ObservesEgress,
    SandboxAcquired,
    SandboxDisposed,
    SandboxObserver,
    ScopeDisposed,
    record,
    recorded_call,
    refuse_an_unusable_observer,
)
from ._protocol import (
    CLEANUP_RANK,
    DEFAULT_BACKEND_DECLARATIONS,
    ISOLATION_RANK,
    ISOLATION_SCOPE_RANK,
    BackendDeclarations,
    Capability,
    Cleanup,
    DisposalCode,
    DisposalFailure,
    EgressRule,
    Identity,
    Isolation,
    IsolationScope,
    OsFamily,
    Sandbox,
    SandboxBackend,
    SandboxKey,
    SandboxLimits,
    SandboxSpec,
    ScopePurge,
    TransferLimits,
    fold_disposal_failures,
    meets_floor,
)
from ._reclaim import DEFAULT_RECLAIM_CONFIG, FailedReclaimPolicy, ReclaimConfig

logger = logging.getLogger(__name__)

__all__ = [
    "ATTACH_REFUSALS",
    "NoSandboxBackend",
    "SandboxBackendNotPermitted",
    "SandboxCapabilityDenied",
    "SandboxCapabilityNotSupported",
    "SandboxEgressNotEnforced",
    "SandboxIdentityDenied",
    "SandboxOsFamilyNotSupported",
    "SandboxRouter",
    "SandboxScopeNotEnforced",
    "SandboxTransferLimitsNotPermitted",
    "SandboxUnclean",
    "ScopeDisposal",
    "Selection",
]


@dataclasses.dataclass
class ScopeDisposal:
    """What :meth:`SandboxRouter.scope` reclaimed, filled in once its block has ended.

    Mutable and read afterwards rather than returned, because a context manager's value is
    bound before the work it wraps has happened.  Inside the block it reads zero and means
    nothing.

    ``undisposed`` is a :class:`~maf_sandbox.DisposalFailure` when a sandbox is still there,
    or ``None``.  A conversation whose delete
    did not land is the case a host most needs to hear about, and the count alone cannot say
    it: zero reclaimed reads the same whether there was nothing to reclaim or nothing worked.
    """

    disposed: int = 0
    undisposed: DisposalFailure | None = None


#: The rungs, weakest first, rendered once for the refusal messages.
_LADDER = ", ".join(map(str, ISOLATION_RANK))

#: The directions a `SandboxLimits` carries, read off the dataclass so a message naming them
#: cannot drift from the type a backend is being asked for.
_DIRECTION_FIELDS = tuple(field.name for field in dataclasses.fields(SandboxLimits))


class NoSandboxBackend(LookupError):
    """No backend is configured, or the named one is not registered."""


class SandboxBackendNotPermitted(PermissionError):
    """The selected backend may not serve: its boundary is below the floor, or it declares
    itself in a way this package cannot read.

    Two families, both a misconfiguration a person fixes in code rather than something a
    caller recovers from. The **boundary** one is the original: the rung the backend claims is
    below the floor the host — or a spec — requires, or is not on the ladder at all. Raised
    rather than degraded on purpose — silently falling back to a stronger backend
    would hide a misconfiguration, and silently proceeding with the weaker one would break
    the boundary every claim about the execution surface rests on.

    The **declaration** one covers a backend this package cannot read, and *when* it is raised
    depends on the selection. A backend still carrying one of the attributes
    :class:`~maf_sandbox.BackendDeclarations` replaced, or a ``declarations`` that is not one,
    is a property of the backend alone, so it is refused at construction under either. A
    mis-shaped field — a ``capabilities`` or ``egress_modes`` that is not a set — is read where
    the match consumes it, so under :data:`Selection.FIXED` it surfaces per spec. Under
    :data:`Selection.PER_SPEC` it is refused at **construction** instead, and has to be:
    routing catches this class to try the next backend, so an unreadable declaration left until
    then would be indistinguishable from an honest refusal and quietly routed past.
    """


class SandboxCapabilityNotSupported(RuntimeError):
    """The selected backend cannot do something the workload's spec requires.

    A functionality mismatch rather than a safety one — register a backend that implements
    the capability, or ask for less.
    """


class SandboxOsFamilyNotSupported(RuntimeError):
    """The selected backend hands out a guest of a shape the workload was not written for.

    A functionality mismatch, so it sits beside :class:`SandboxCapabilityNotSupported` rather
    than among the safety refusals: the workload would run, on a backend serving the family it
    asked for.  What it is *not* is a statement about what the guest has installed — a spec
    asking for ``POSIX`` and getting it can still meet an image with no shell, which is a
    different question answered somewhere else entirely.
    """


class SandboxCapabilityDenied(PermissionError):
    """The workload requires a capability this host's router denies outright.

    The posture counterpart of :class:`SandboxCapabilityNotSupported`: not "the backend
    cannot", but "this host will not", whatever the backend declares.  A hard stop rather
    than awareness — the deny list exists for hosts whose policy about a capability
    (``HOST_TOOLS`` above all) is a refusal, not a classification.
    """


class SandboxIdentityDenied(PermissionError):
    """The workload's host tools exercise an identity this host's router denies.

    Same posture as :class:`SandboxCapabilityDenied`, on the identity axis: a host that
    forbids model-orchestrated user authority states ``denied_identities={Identity.USER}``
    once, and a spec whose registry-derived ``identities`` carries it is refused at attach —
    before anything runs, where every other posture question is answered.
    """


class SandboxUnclean(PermissionError):
    """The sandbox for this key was left unclean — data the framework could not remove, or a
    program a stop did not provably take down — and the disposal that would have made it go
    did not land.

    Raised by :meth:`SandboxRouter.acquire` until a disposal for the key lands — through
    :meth:`~SandboxRouter.dispose`, :meth:`~SandboxRouter.dispose_scope`, or the framework's
    own next attempt. Refused rather than served: ``acquire`` is get-or-create, so serving
    the key would hand the next call everything the last one could not take back. Better a
    failed run than leaked data. This is in-process knowledge only — another replica holds
    no such record, which is the same bound ``dispose_scope`` exists to reach past.

    :attr:`code` is the :data:`DisposalCode` the last disposal reported, or ``None``. Branch
    on it rather than on the message. The backend's detail is not here at all: it can carry an
    endpoint or a raw response body, and it stays in the log.
    """

    def __init__(self, *args: object, code: DisposalCode | None = None) -> None:
        # `*args` keeps the inherited `OSError` constructors; `code` is keyword-only, additive.
        super().__init__(*args)
        self.code = code


class SandboxEgressNotEnforced(PermissionError):
    """The selected backend cannot enforce the egress mode the workload runs in.

    Refuse, never degrade: the router will not substitute a mode for the one the spec declares
    — a more open one silently widens what the workload reaches, a more isolated one hands it a
    posture it was not built for. So a backend that cannot deliver the asked mode turns the
    workload away rather than serving it behind a different boundary. See
    ``docs/sandbox/research/egress-resolution.md``.
    """


class SandboxScopeNotEnforced(PermissionError):
    """The selected backend cannot give the workload a sandbox at the scope it runs at.

    The :class:`SandboxEgressNotEnforced` rule on a second axis, refused for the same reason: a
    backend serving one sandbox per conversation would answer a workload asking for one per call
    by sharing, and every call would succeed while the separation it asked for was never there.
    """


class SandboxTransferLimitsNotPermitted(PermissionError):
    """The workload's spec asks to move more data than the selected backend allows.

    A safety claim rather than a functionality one, which is why an undeclared ``limits`` is
    read as :data:`~maf_sandbox.DEFAULT_SANDBOX_LIMITS` and a bigger ask refused, where an
    undeclared ``capabilities`` is read charitably.  Also raised for a ``limits`` this package
    cannot read at all — a declaration nobody can compare against is refused, not guessed at.
    """


#: The refusals a spec can meet: every one this module defines except the two that answer with
#: a sentence of their own, `NoSandboxBackend` and `SandboxUnclean`.
#:
#: **What membership does not confer is trust in the text.** These classes are exported, and
#: `acquire` forwards what a backend raises, so an instance may carry a message this package
#: never wrote — an SDK response, an endpoint. What `maf.py` reads off the type is that the
#: workload was *refused*, which is worth a sentence of its own beside the one for an outage;
#: the message stays in the log, the way `SandboxUnclean` passes a code and leaves the detail
#: behind.
#:
#: Public by necessity — this package's strict pyright refuses a private name across modules —
#: and absent from `__init__`, so it stays internal. `test_maf_glue.py` derives the membership
#: independently and fails if a refusal added above is left out.
ATTACH_REFUSALS: tuple[type[Exception], ...] = (
    SandboxBackendNotPermitted,
    SandboxCapabilityDenied,
    SandboxCapabilityNotSupported,
    SandboxEgressNotEnforced,
    SandboxIdentityDenied,
    SandboxOsFamilyNotSupported,
    SandboxScopeNotEnforced,
    SandboxTransferLimitsNotPermitted,
)


def _with_snapshotted_labels(spec: SandboxSpec) -> SandboxSpec:
    """``spec`` with its own copy of ``labels``, so a delivered record cannot move afterwards.

    ``labels`` is the only mutable field, so copying it is the whole duty — and it is copied
    even when empty, because an empty dict is still the *caller's* dict and an observer writing
    into one reaches the spec the caller holds.
    """
    return dataclasses.replace(spec, labels=dict(spec.labels))


def _coded(backend_name: str, reported: object) -> DisposalFailure:
    """One backend's answer as a :class:`~maf_sandbox.DisposalFailure`, named by the backend.

    ``object`` because this is where a backend's answer stops being trusted. A bare ``str`` is
    a backend that has not moved to the class yet; anything else — a bool, an exception, a
    backend built against a newer protocol — broke its own annotation. Both read as
    ``"unknown"``, because reading ``.code`` off one would raise out of a caller that never does.
    """
    if isinstance(reported, DisposalFailure):
        return DisposalFailure(reported.code, f"{backend_name}: {reported.detail}")
    return DisposalFailure("unknown", f"{backend_name}: {reported}")


def _refuse_an_invalid_sandbox(sandbox: Sandbox) -> None:
    """Require cleanup and instance tracking members, even when reclamation is withheld."""
    if not callable(getattr(sandbox, "reclaim", None)):
        raise TypeError(
            f"{type(sandbox).__name__} does not implement `Sandbox.reclaim`, a required protocol "
            "member. Implement safe reclamation or an explicit refusal; declare RECLAIM only "
            "when safe reclamation is established. "
            "`maf_sandbox.conformance.assert_reclaim_conformance` checks that declaration."
        )
    instance_id = getattr(sandbox, "instance_id", None)
    if not isinstance(instance_id, str) or not instance_id:
        raise TypeError(f"{type(sandbox).__name__} must expose a nonempty `Sandbox.instance_id`")


async def _reset_instance(sandbox: Sandbox, *, timeout: float) -> str:
    """Reset and validate the replacement identity, returning the retired ID."""
    previous = sandbox.instance_id
    await sandbox.reset(timeout=timeout)
    _refuse_an_invalid_sandbox(sandbox)
    if sandbox.instance_id == previous:
        raise TypeError("Sandbox.reset must establish a new instance_id")
    return previous


def _declared_isolation(backend: SandboxBackend) -> Isolation:
    """The rung ``backend`` claims, refusing any value this package does not recognise.

    The enum constructor *is* the refuse-unknown policy: a value nobody ranked cannot be
    compared against a floor, and guessing in either direction is worse than stopping.
    """
    raw = str(backend.isolation)
    try:
        return Isolation(raw)
    except ValueError as exc:
        raise SandboxBackendNotPermitted(
            f"sandbox backend {backend.name!r} declares {raw!r} isolation, which is not a "
            f"rung on the ladder ({_LADDER}). Refused rather than ranked: nothing here can "
            "tell whether an unrecognised boundary is stronger or weaker than the floor."
        ) from exc


#: The attributes :class:`~maf_sandbox.BackendDeclarations` replaced. Transitional: it exists to
#: name the 0.26 migration while backends are still moving, and can go once none are.
_SUPERSEDED_DECLARATIONS = ("capabilities", "limits", "egress_modes", "os_families")


#: Sentinel for the two lookups in :func:`_declarations`. ``None`` cannot serve as one: an
#: attribute explicitly set to ``None`` is a declaration to refuse, not an absent one to read as
#: silence.
_MISSING = object()


def _has_attribute(backend: SandboxBackend, name: str) -> bool:
    """Whether ``backend`` defines ``name``, **without running it**.

    Not :func:`hasattr`, which calls the descriptor and answers ``False`` when it raises. Every
    superseded declaration was written as a ``property``, so the half-migrated backend this is
    looking for is the one whose leftover property raises — and ``hasattr`` reads exactly that
    as "no such attribute" and waves it through. Not executing it is the second reason: a
    declaration this package has stopped reading should not be run to find out it is there.
    """
    return inspect.getattr_static(backend, name, _MISSING) is not _MISSING


def _claims_egress_observation(backend: SandboxBackend) -> bool:
    """Whether ``backend`` claims it can report what its egress enforcement decided.

    Defensive on purpose.  This runs at construction, before any spec exists, and an unreadable
    declaration is the per-spec checks' to refuse with the message they have for it — reading
    one here would move that failure to the constructor and describe it as an observability
    problem.  So an unreadable declaration answers "no claim", and the reader that is allowed to
    fail sees it a moment later.
    """
    try:
        declared: object = _declarations(backend)
    except Exception:  # noqa: BLE001 - not this reader's failure to report
        return False
    return getattr(declared, "observes_egress", False) is True


def _declarations(backend: SandboxBackend) -> BackendDeclarations:
    """The one object every optional declaration is read from: one ``getattr``, six fields.

    Not a Protocol member, so declaring nothing is legal and reads as
    :data:`~maf_sandbox.DEFAULT_BACKEND_DECLARATIONS`.  *Declaring nothing* is narrower than it
    looks: an attribute set to ``None``, and one whose descriptor raises, are both declarations
    this package cannot read, and each is refused rather than defaulted.

    A backend still carrying one of the attributes this object replaced is refused, **whether or
    not it also declares the object** — moving three fields and leaving the fourth behind is the
    likelier mistake, and it is the silent one: nothing reads the stray attribute, so its value
    is replaced by that field's default. On ``limits`` that *widens* a ceiling the backend
    declared to be narrow. Nothing in the type system marks any of this, because none of the
    four was ever a Protocol member and ``isinstance`` holds either way.
    """
    superseded = [name for name in _SUPERSEDED_DECLARATIONS if _has_attribute(backend, name)]
    if superseded:
        raise SandboxBackendNotPermitted(
            f"sandbox backend {backend.name!r} declares {', '.join(superseded)} directly, "
            f"which {BackendDeclarations.__name__} replaced. Move each value into a "
            "`declarations` attribute holding one, under the same field name, and delete the "
            "attribute. Refused rather than ignored: nothing reads those attributes now, so "
            "each one left behind is silently replaced by that field's default."
        )
    # `getattr`, not the static lookup, because this one wants the **value**: a backend that
    # forwards `declarations` through `__getattr__` — a wrapper delegating to an inner backend —
    # has declared it, and a static lookup does not see it. Reading it as silence there would
    # substitute the defaults for what that backend actually said.
    declared: object = getattr(backend, "declarations", _MISSING)
    if declared is _MISSING:
        # Absent, or defined and raised. Only the static lookup tells those apart, and they are
        # not the same answer: silence is legal, a declaration that cannot be read is not.
        if _has_attribute(backend, "declarations"):
            raise SandboxBackendNotPermitted(
                f"sandbox backend {backend.name!r} defines `declarations` and reading it "
                "raised AttributeError. Refused rather than read as silence: a backend that "
                "states its declarations and cannot produce them has not declared nothing."
            )
        return DEFAULT_BACKEND_DECLARATIONS
    if isinstance(declared, BackendDeclarations):
        return declared
    kind = type(declared)
    raise SandboxBackendNotPermitted(
        f"sandbox backend {backend.name!r} declares declarations as "
        f"{kind.__module__}.{kind.__qualname__}, and only "
        f"{BackendDeclarations.__module__}.{BackendDeclarations.__qualname__} can be read as "
        "one. Both module paths are named because they are the same when this is an ordinary "
        "type error, and differ when two copies of maf_sandbox are on the path — a vendored "
        "one, or two versions resolved into one environment. Declare nothing at all to accept "
        "every default."
    )


def _declared_set(backend: SandboxBackend, declared: object, field: str) -> frozenset[object]:
    """A set-valued declaration, refusing any other shape.

    ``capabilities`` and ``egress_modes`` are consumed by set arithmetic and by ``in``; handed
    a string or a list they raise ``TypeError`` out of a host's agent factory, or match nothing
    and read as an honest refusal. Refused here instead, on :func:`_declared_limits`'s policy:
    a declaration this package cannot read is refused rather than guessed at.

    The members are not checked. :class:`~maf_sandbox.Egress` and
    :class:`~maf_sandbox.Capability` are ``StrEnum``, so a backend declaring plain strings
    matches exactly as the members would, and that tolerance is deliberate.
    """
    if isinstance(declared, frozenset | set):
        return frozenset(cast("Iterable[object]", declared))
    raise SandboxBackendNotPermitted(
        f"sandbox backend {backend.name!r} declares {field} as {type(declared).__name__}, and "
        "only a set can be read as one — the router subtracts it, tests membership in it and "
        "sorts it for the refusal message. Declare nothing at all to accept the default."
    )


def _declared_os_families(declared: BackendDeclarations) -> frozenset[OsFamily]:
    """The guest shapes a backend claims it hands out, empty when it claims none.

    A value that is not a set of :class:`~maf_sandbox.OsFamily` is read as empty rather than
    refused, deliberately, and this is the one place that choice is made: unlike a mis-shaped
    ``limits``, a mis-shaped value here cannot widen anything — the worst it does is refuse a
    workload that would have been served, loudly, with the declaration named.
    """
    # Read as `object` rather than at the field's own type: a frozen dataclass validates no
    # field, so an out-of-tree backend puts whatever it likes here and every element is checked.
    families = cast("object", declared.os_families)
    if not isinstance(families, frozenset | set):
        return frozenset()
    members = cast("Iterable[object]", families)
    return frozenset(family for family in members if isinstance(family, OsFamily))


def _declared_isolation_scopes(
    backend: SandboxBackend, declared: BackendDeclarations
) -> frozenset[object]:
    """The scopes a backend claims it serves, defaulting to the sharing every backend already does.

    Saying nothing — an absent field, or an empty set — reads as
    :data:`~maf_sandbox.IsolationScope.CONVERSATION`, and this is the one declaration whose
    silence is a claim: get-or-create is what :meth:`~maf_sandbox.SandboxBackend.acquire` has
    always obliged, so a backend written before this axis serves exactly what it served.

    A value that is not a set is **refused**, on :func:`_declared_set`'s policy rather than
    :func:`_declared_os_families`'s.  There a mis-shape resolves to the empty answer and can
    only refuse a workload; here reading one as silence would mint a claim, and a backend that
    mis-shapedly declared only :data:`~maf_sandbox.IsolationScope.CALL` would be served the
    conversation workloads its readable declaration turns away.  A posture nobody can read is
    refused at the router rather than guessed in the workload's favour.

    The members are not checked, for the reason :func:`_declared_set` gives: this is a
    ``StrEnum``, so a backend declaring plain strings matches exactly as the members do.
    """
    scopes = _declared_set(backend, cast("object", declared.isolation_scopes), "isolation_scopes")
    return scopes or frozenset({IsolationScope.CONVERSATION})


def _instance_id(sandbox: object) -> str | None:
    """Read an acquired ID even when another required protocol member is invalid."""
    value = getattr(sandbox, "instance_id", None)
    return value if isinstance(value, str) and value else None


@dataclasses.dataclass(eq=False)
class _PendingDisposal:
    backend: SandboxBackend
    kind: str | None
    instance_id: str | None = None


@dataclasses.dataclass
class CallAdmission:
    """A call's backend and cleanup rung, retained until its hold is released."""

    backend: SandboxBackend
    rung: Cleanup
    served: bool = False


@dataclasses.dataclass
class _Serving:
    """Which backend an in-flight acquire chose, for the record that covers every way out."""

    backend: SandboxBackend | None = None


def _recorded_name(backend: SandboxBackend) -> str:
    """``backend.name`` for a record, never raising.

    ``name`` is a property on somebody else's class, and an event's fields are evaluated in the
    *caller's* frame — before :func:`record` is entered — so a property that raises here escapes
    the containment the observer's own failures get, and fails an operation that had succeeded.
    """
    try:
        return backend.name
    except CONTAINED as raised:  # noqa: BLE001 - `_containment` carries the rule
        if escapes_containment(raised):
            raise
        return type(backend).__name__


def _reported(failure: DisposalFailure | None) -> DisposalReport:
    """What a backend's answer to ``dispose`` says about whether the sandbox is gone.

    ``None`` is ``"gone"`` and not "verified gone": :meth:`SandboxBackend.dispose` documents
    that a backend with no way to check answers ``None`` too, and chooses that conflation
    deliberately.  The record therefore says what was reported, and leaves a reader to know
    what a given backend can actually see.

    ``"unknown"`` comes off the code rather than from the caller, because every route into it
    already sets one: a ``dispose`` that raised and an interrupted one are both folded into
    :data:`~maf_sandbox.DisposalCode` ``"unknown"`` before they reach a record.  Reading it
    here keeps a synthesised failure from being reported as a backend saying the sandbox is
    still there.
    """
    if failure is None:
        return "gone"
    return "unknown" if failure.code == "unknown" else "may_remain"


def _note_what_held(acquired: SandboxAcquired) -> None:
    """Collect the posture a served acquire ran under, where something is collecting it.

    Asked again rather than left to :func:`note_effective_state`'s own no-op: an observer alone
    reaches here on every acquire, and it must not pay for a snapshot nobody is holding.

    Contained, and for the reason :func:`_recorded_name` is: the snapshot reads a backend's own
    declarations, so a backend author's ``frozenset`` runs here — and this sits in a ``finally``
    over an acquire that has already succeeded, where a raise would replace the sandbox with an
    exception about the record of it.
    """
    if not effective_state_is_noted():
        return
    try:
        state = EffectiveState.of(acquired)
    except CONTAINED as raised:  # noqa: BLE001 - `_containment` carries the rule
        if escapes_containment(raised):
            raise
        logger.warning(
            "sandbox effective state: what %r was served was not recorded: %s",
            acquired.spec.kind,
            error_detail(raised),
        )
        return
    if state is not None:
        note_effective_state(state)


def _recorded_declarations(
    backend: SandboxBackend | None,
) -> tuple[Isolation | None, BackendDeclarations | None]:
    """What ``backend`` declares, read for a record and never raising.

    Both reads refuse a backend whose declarations cannot be read, and by here one already
    passed them — but an acquire must not start failing over the record of it, so a second
    answer replaces neither the sandbox nor the refusal the caller is owed.
    """
    if backend is None:
        return (None, None)
    try:
        return (_declared_isolation(backend), _declarations(backend))
    except CONTAINED as raised:  # noqa: BLE001 - `_containment` carries the rule
        # Both are property reads, so a backend author's code runs here and an acquire must not
        # start failing over the record of it.
        if escapes_containment(raised):
            raise
        return (None, None)


def _declared_limits(backend: SandboxBackend, declared: BackendDeclarations) -> SandboxLimits:
    """The ceilings a backend claims, refusing a declaration that is not the right shape.

    Same policy as :func:`_declared_isolation`, for the same reason: a declaration this package
    cannot read is refused rather than guessed at.  The mistake worth naming is the adjacent
    one — :class:`~maf_sandbox.TransferLimits` is a cap for **one** direction and
    :class:`~maf_sandbox.SandboxLimits` is the pair, both exported from one module, and the
    wrong one here used to surface as a bare ``AttributeError`` out of a host's agent factory.
    """
    # As `object` for the reason :func:`_declared_os_families` gives: a backend pyright never
    # saw can still hand over the adjacent type.
    limits = cast("object", declared.limits)
    if isinstance(limits, SandboxLimits):
        return limits
    raise SandboxTransferLimitsNotPermitted(
        f"sandbox backend {backend.name!r} declares limits as "
        f"{type(limits).__name__}, and only {SandboxLimits.__name__} can be read as "
        f"one — it carries a "
        f"{TransferLimits.__name__} per direction ({', '.join(_DIRECTION_FIELDS)}), where a "
        f"bare {TransferLimits.__name__} is one direction's caps and says nothing about the "
        "other. Declare nothing at all to accept the default ceilings."
    )


class Selection(StrEnum):
    """How a router decides which registered backend serves a workload.

    :data:`FIXED` is the default.  What turning on :data:`PER_SPEC` costs, and it is the reason
    it is opt-in: a spec that is refused today becomes a *running* sandbox, which on a remote
    backend has a price.  ``docs/sandbox/capabilities.md`` carries the argument and the
    migration case.
    """

    #: One backend, resolved at construction — the one ``selected`` names, or the first
    #: registered.  Every workload gets that one, and a spec it cannot serve is refused with
    #: the other registered backends untouched, however well one of them would have done.
    FIXED = "fixed"
    #: The first registered backend that can serve *this* spec, decided per workload against
    #: the same checks :meth:`SandboxRouter.ensure_can_serve` runs.  Registration order is the
    #: preference order.  The route is a pure function of the spec and the backends'
    #: declarations — never of load, health, latency or cost — so one spec always routes to the
    #: same backend and the warm sandbox ``acquire`` reuses stays reachable.  Per *spec*, not
    #: per conversation: two kinds under one key may route apart by design, which is why
    #: :meth:`SandboxRouter.dispose` fans out across every registered backend.
    PER_SPEC = "per_spec"


class SandboxRouter:
    """Routes a sandbox request to a backend.

    Args:
        backends: The registered backends, in preference order — which is read past the
            first only under ``selection=Selection.PER_SPEC``.
        min_isolation: The weakest boundary this host accepts. Defaults to
            :data:`Isolation.MICROVM`.
        min_isolation_scope: The most sharing this host accepts — how much of a conversation
            one sandbox may serve. Defaults to
            :data:`~maf_sandbox.IsolationScope.CONVERSATION`, which is what every backend
            already did. Raised to :data:`~maf_sandbox.IsolationScope.CALL` it gives every
            workload this router serves a sandbox of its own per call, whatever the workload's
            own spec asks for, and refuses a backend that cannot create one.
        min_cleanup: Weakest cleanup this host accepts. Defaults to :data:`Cleanup.RECLAIM`,
            which permits reuse only when the workload and backend establish it. Without a
            confinement claim or snapshot capability, cleanup still resolves to disposal.
            A spec may raise this floor, never lower it.
        selected: Name of the backend to use. ``None`` picks the first registered one, which
            with a single backend is the whole selection story and stays correct when more
            arrive. A pin, and refused together with ``selection=Selection.PER_SPEC``:
            "prefer this one, and route past it when it cannot serve" is the cheapest-first
            policy this router declines to have, wearing another name. A host that wants a
            different preference reorders ``backends``, which is a diff a reviewer reads —
            and a host **migrating a pinned router to per-spec selection** has to, since
            dropping the pin makes routing start at the first registered backend and a
            workload the pinned one was serving would otherwise move.
        selection: How a backend is chosen — one resolved at construction
            (:data:`Selection.FIXED`, the default, and what this package has always done), or
            the first registered one that can serve each spec (:data:`Selection.PER_SPEC`).
            :class:`Selection` carries why routing is opt-in.
        denied_capabilities: Capabilities this host refuses outright, whatever a backend
            declares — a spec *requiring* one is refused at attach. The hard stop for a
            posture: ``denied_capabilities={Capability.HOST_TOOLS}`` closes the
            middleware-bypass channel for every workload this router serves.
        denied_identities: Identities this host refuses host tools to exercise — a
            spec whose ``identities`` carries one is refused at attach.
            ``denied_identities={Identity.USER}`` is how a host forbids model-orchestrated
            user authority in one statement instead of auditing each registration.
        reclaim: Host-wide policy and handlers for tool call reclaim (timeout, failure policy,
            and failure callback). Defaults to :data:`~maf_sandbox.DEFAULT_RECLAIM_CONFIG`
            (:class:`~maf_sandbox.ReclaimConfig` with ``timeout=30.0``,
            ``failed_reclaim_policy=FailedReclaimPolicy.DISPOSE``, and no callback). A kind
            cannot set the policy: it is the host's call to loosen, never a workload's.
        observer: Where this router's sandbox lifecycle is recorded — every acquire, served or
            refused, and every backend's answer to every disposal. Also what
            :func:`~maf_sandbox.maf.sandboxed_tool` reads, so a call's own events reach the same
            place without a second wiring. Default ``None`` records nothing and costs nothing:
            no event is built for a router with no observer. What a guest may call back into is
            :class:`~maf_sandbox.HostToolRegistry`'s to record, and a host may wire either alone.

    Raises:
        SandboxBackendNotPermitted: at construction, when the selected backend declares a
            rung below ``min_isolation`` or one this package does not recognise, or when its
            declarations cannot be read — an attribute
            :class:`~maf_sandbox.BackendDeclarations` replaced, or a ``declarations`` that is
            not one. Failing here rather than at first use means a misconfigured deployment
            cannot start with the feature apparently enabled and quietly unsafe. Under
            :data:`Selection.PER_SPEC` every registered backend is read rather than one, and
            the floor is judged across all of them together: the refusal is for a deployment
            where *nothing* registered clears it. A single backend below the floor is not an
            error there — it is one no spec is ever routed to, named by a warning at
            construction, and still reached by disposal, which is why it stays registered.
        ValueError: at construction, when ``min_isolation`` is not a rung — or
            ``min_isolation_scope`` not a scope — this package recognises, raised by
            :class:`Isolation` and :class:`IsolationScope` themselves rather than surfacing as a
            bare ``KeyError`` out of a rank comparison, which would only happen once a backend
            was registered and a floor was actually compared against — or when a denied
            capability or identity is not a member this package recognises: a deny list that
            silently never matches would read as protection and provide none; or when
            ``reclaim.timeout`` is not a finite positive number; or when ``selected`` names a
            backend *and* ``selection`` routes per spec, which are two different answers to
            the one question this router exists to answer.
        TypeError: at construction, when ``observer`` is not a
            :class:`~maf_sandbox.SandboxObserver` or overrides an event with a coroutine
            function. Neither has a call-time symptom worth waiting for: a plain object answers
            no event, and a coroutine one is never awaited.
    """

    def __init__(
        self,
        backends: Sequence[SandboxBackend],
        *,
        min_isolation: Isolation = Isolation.MICROVM,
        min_isolation_scope: IsolationScope = IsolationScope.CONVERSATION,
        min_cleanup: Cleanup = Cleanup.RECLAIM,
        selected: str | None = None,
        selection: Selection = Selection.FIXED,
        denied_capabilities: Iterable[Capability] = (),
        denied_identities: Iterable[Identity] = (),
        reclaim: ReclaimConfig = DEFAULT_RECLAIM_CONFIG,
        observer: SandboxObserver | None = None,
    ) -> None:
        self._backends = list(backends)
        self._observer = (
            None if observer is None else refuse_an_unusable_observer(observer, argument="observer")
        )
        if not math.isfinite(reclaim.timeout) or reclaim.timeout <= 0:
            raise ValueError(
                f"reclaim.timeout must be a finite positive number of seconds, not "
                f"{reclaim.timeout}."
            )
        self._reclaim = reclaim
        # Keys whose sandbox holds data the framework could not remove and could not dispose
        # of. An entry leaves when a disposal lands; a key that keeps failing stays refused.
        # Keyed, not a set, so a refusal can say why; `None` for a key marked before a try.
        self._unclean: dict[SandboxKey, DisposalFailure | None] = {}
        self._pending_disposals: dict[
            SandboxKey, dict[tuple[int, str | None, str | None], _PendingDisposal]
        ] = {}
        self._unclean_guard = threading.Lock()
        # Disposals for one key run one at a time. Only they: a disposal body awaits once per
        # backend while it rewrites the ledger, and two interleaved leave one clearing the key
        # while the other is still deleting (#642 race E). `acquire` takes nothing — its ledger
        # reads carry no await and are already atomic, and a lock held across a cold create
        # would block the very disposal that exists to bound a dirty sandbox's life.
        # Per loop, because an `asyncio.Lock` binds to the loop it first waits on. Weak on both
        # sides: a lock lives only while a disposal holds it, so keys do not accumulate one
        # apiece — and a *contended* lock references its loop, which through a strong value
        # would keep that loop alive in the weak-keyed table for ever.
        self._disposal_locks: weakref.WeakKeyDictionary[
            asyncio.AbstractEventLoop, weakref.WeakValueDictionary[SandboxKey, asyncio.Lock]
        ] = weakref.WeakKeyDictionary()
        self._min_isolation = Isolation(str(min_isolation))
        self._min_isolation_scope = IsolationScope(str(min_isolation_scope))
        # The host's floor, and the ladder's weakest rung by default — which is *not* a default
        # of "reclaim". What a call actually ends on is the weakest rung the spec and the
        # backend establish at or above this, and a spec that claims nothing establishes only
        # DISPOSE, so silence here still leaves nothing behind. Raising it is how a host
        # overrides a kind's claim without arguing with the kind.
        self._min_cleanup = Cleanup(str(min_cleanup))
        # A sandbox cleaned by anything above RECLAIM serves one call at a time, so the call
        # running the cleanup is the only call there — see `ExclusiveSlots`.
        self._slots = ExclusiveSlots()
        self._adoptions = ExclusiveSlots()
        self._seen: dict[tuple[SandboxKey, str, int], set[str]] = {}
        self._served: dict[tuple[SandboxKey, str, int], tuple[SandboxBackend, set[str]]] = {}
        self._seen_guard = threading.Lock()
        self._selected_name = selected
        self._selection = Selection(str(selection))
        if self._selection is Selection.PER_SPEC and selected is not None:
            raise ValueError(
                f"selected={selected!r} names one backend and selection="
                f"{str(self._selection)!r} asks for the first that can serve each spec, which "
                "are two answers to one question. Refused rather than ranked, because both "
                "ways of ranking them are wrong: honouring the pin makes the selection "
                "argument silently do nothing, and treating it as a preference to route past "
                "is the cheapest-first policy this router declines to have. Drop the pin and "
                "put the preferred backend first in `backends`, which is the same statement "
                "somewhere a reviewer reads it."
            )
        self._denied_capabilities = frozenset(
            Capability(str(capability)) for capability in denied_capabilities
        )
        self._denied_identities = frozenset(
            Identity(str(identity)) for identity in denied_identities
        )
        if self._selection is Selection.PER_SPEC:
            # No one backend to resolve, so `backend` has no answer to give and `_candidates`
            # is what every later decision reads instead. In registration order, because that
            # order is the preference and this is the only place it is fixed.
            self._backend = None
            self._candidates = self._eligible()
        else:
            self._backend = self._resolve()
            self._candidates = [] if self._backend is None else [self._backend]
        # Last, because everything above can raise and this reaches *outside* the object: a
        # reporter installed before a refusal would leave the backend reading logs on every
        # acquire and reporting them into a router the host never received and cannot switch
        # off. Nothing after this line fails.
        self._hand_out_the_egress_reporter()

    def _hand_out_the_egress_reporter(self) -> None:
        """Tell every backend that can report egress where to report it, or that it must not.

        Every one is told, including by a router with no observer, which hands over ``None``:
        one backend instance may be registered on two routers, and leaving a live reporter in
        place would charge this router's acquires to the other one's observer.  **All or none,
        and back as it was** — ``observe_egress`` is a backend's code and may raise, so each one
        that took a reporter is handed its previous one back before the failure propagates.
        That restore is exact only because the hook is required to be atomic; a backend that
        stores a reporter and *then* raises never handed over what it replaced, and nothing
        here can recover it.
        """
        installed: list[tuple[ObservesEgress, EgressReporter | None]] = []
        report = self._egress_reporter() if self._observer is not None else None
        try:
            for backend in self._backends:
                reports = isinstance(backend, ObservesEgress)
                if not reports and _claims_egress_observation(backend):
                    logger.warning(
                        "sandbox backend %r declares observes_egress and implements no "
                        "observe_egress, so it reports no egress decisions and a reader has no "
                        "way to tell that from a guest that attempted none. Implement "
                        "ObservesEgress, or drop the declaration — the default says the honest "
                        "thing.",
                        _recorded_name(backend),
                    )
                if reports:
                    # Enrolled *after* the call returns, because only then is there a previous
                    # reporter to put back. A hook that raises changed nothing — the contract
                    # requires that — and one that broke the contract handed over nothing this
                    # could restore, so a placeholder entry would roll back to `None` and
                    # silence the router whose reporter it was.
                    installed.append((backend, backend.observe_egress(report)))
        except BaseException:
            # Reverse order: the same backend instance may be registered twice, and unwinding
            # forwards would end by reinstating this router's reporter rather than the original.
            for taken, previous in reversed(installed):
                # `BaseException`, not `Exception`: this block is entered for one, so a restore
                # hook raising a cancel or an interrupt would both replace the failure being
                # preserved and skip every backend after it. The original is what propagates.
                with contextlib.suppress(BaseException):
                    taken.observe_egress(previous)
            raise

    def _egress_reporter(self) -> EgressReporter:
        """A reporter that records through this router without keeping it alive.

        Weak on purpose: a backend outlives the router that registered it whenever a host keeps
        one and drops the other, and a bound method there would pin the router, and through it
        the host's observer, for as long as the backend lived.  Once the router is collected the
        reporter answers nothing, which is the state a dropped router should leave behind.
        """
        reference = weakref.ref(self)

        def report(event: EgressObserved) -> None:
            router = reference()
            if router is not None:
                record(router._observer, event, logger)  # noqa: SLF001 - its own attribute

        return report

    def _resolve(self) -> SandboxBackend | None:
        if not self._backends:
            return None
        if self._selected_name is None:
            backend = self._backends[0]
        else:
            matches = [b for b in self._backends if b.name == self._selected_name]
            if not matches:
                names = ", ".join(sorted(b.name for b in self._backends)) or "none"
                raise NoSandboxBackend(
                    f"sandbox backend {self._selected_name!r} is not registered "
                    f"(registered: {names})"
                )
            backend = matches[0]

        _declarations(backend)
        declared = _declared_isolation(backend)
        if not meets_floor(declared, self._min_isolation):
            raise SandboxBackendNotPermitted(
                f"sandbox backend {backend.name!r} declares {str(declared)!r} isolation, "
                f"below this host's {str(self._min_isolation)!r} minimum-isolation floor "
                f"(ladder, weakest first: {_LADDER}). Refused rather than degraded: falling "
                "back to a stronger backend would hide the misconfiguration, and proceeding "
                "with the weaker one would break the boundary the host asked for. A host "
                "that means to run here lowers the floor explicitly with min_isolation."
            )
        return backend

    def _eligible(self) -> list[SandboxBackend]:
        """Every registered backend, once each is readable and at least one clears the floor.

        Every one of them rather than the subset above the floor, since a dropped backend would
        still be in ``self._backends`` and that is what disposal sweeps.

        The trap is what must be validated *here* rather than left to the per-spec checks:
        :meth:`_refusal_serving` catches :data:`ATTACH_REFUSALS`, and the field readers raise
        members of it for a declaration this package cannot read — so past this point an
        unreadable declaration is indistinguishable from a backend honestly declining one spec,
        and the next candidate would quietly serve.
        """
        if not self._backends:
            return []
        floor = self._min_isolation
        rungs = [(backend, _declared_isolation(backend)) for backend in self._backends]
        for backend in self._backends:
            declared = _declarations(backend)
            # Every field, not only the object: each of these raises a member of
            # `ATTACH_REFUSALS` for a shape this package cannot read, and past this point such
            # a raise is indistinguishable from a backend honestly refusing one spec.
            _declared_set(backend, cast("object", declared.capabilities), "capabilities")
            _declared_set(backend, cast("object", declared.egress_modes), "egress_modes")
            if declared.egress_method_tokens is not None:
                _declared_set(
                    backend, cast("object", declared.egress_method_tokens), "egress_method_tokens"
                )
            _declared_isolation_scopes(backend, declared)
            _declared_limits(backend, declared)
        below = [(backend, rung) for backend, rung in rungs if not meets_floor(rung, floor)]
        if below and len(below) != len(rungs):
            # Warned rather than raised: a weaker backend beside a stronger one is the
            # arrangement this mode serves. Warned rather than silent: the per-spec refusal
            # names it only when *nothing* serves, which is not this case. Two things the
            # message must not say — it must not advise unregistering, since `dispose` and
            # `dispose_scope` reach every registered backend and a host that changed which
            # one serves relies on that; and it must not promise what *will* serve, since no
            # spec exists yet and an above-floor backend may still refuse every workload.
            logger.warning(
                "sandbox router: %s registered below this host's %r minimum-isolation floor, so "
                "no workload is ever routed there and only the backends clearing it are "
                "considered. It stays registered and disposal still reaches it, which is what "
                "a host that changed backends relies on — so unregistering it would strand "
                "whatever it still holds. Lower min_isolation if this host means to accept "
                "that boundary.",
                ", ".join(f"{backend.name!r} ({str(rung)})" for backend, rung in below),
                str(floor),
            )
        if not any(meets_floor(rung, floor) for _, rung in rungs):
            named = ", ".join(f"{backend.name!r} ({str(rung)})" for backend, rung in rungs)
            raise SandboxBackendNotPermitted(
                f"no registered sandbox backend meets this host's "
                f"{str(floor)!r} minimum-isolation floor (ladder, weakest "
                f"first: {_LADDER}). Registered: {named}. This router selects per spec, so "
                "one backend below the floor is not an error — it is simply never routed to. "
                "None of them clearing it is different: no workload can be served at all, and "
                "a deployment in that state should not start with the feature apparently "
                "enabled. A host that means to run here lowers the floor explicitly with "
                "min_isolation."
            )
        return list(self._backends)

    @property
    def backend(self) -> SandboxBackend | None:
        """The one backend this router always uses, or ``None`` when there is no such thing.

        ``None`` has two causes and they are not the same thing: no backend is configured, or
        this router selects per spec and the question has no fixed answer. :attr:`enabled` is
        what tells them apart, and :meth:`backend_for` is what answers the routed question.
        """
        return self._backend

    @property
    def enabled(self) -> bool:
        """Whether this router has a backend to try at all. A host attaches no tools if not.

        Registration rather than capability, and the gap is worth stating: a candidate is a
        backend whose declarations could be read, where at least one of them clears this host's
        floor.  A backend that then refuses every spec leaves this ``True`` — an empty
        ``egress_modes`` is the plainest way, since it enforces no mode and so can serve none.
        Whether *this* workload can be served is :meth:`ensure_can_serve`'s answer, and a much
        stricter question.

        Read off the candidates rather than off :attr:`backend`, which under
        :data:`Selection.PER_SPEC` is ``None`` while the router is perfectly able to serve.
        """
        return bool(self._candidates)

    @property
    def selection(self) -> Selection:
        """How this router chooses a backend."""
        return self._selection

    @property
    def reclaim(self) -> ReclaimConfig:
        """Host-wide policy and handlers for tool call reclaim."""
        return self._reclaim

    @property
    def observer(self) -> SandboxObserver | None:
        """Where this router records, or ``None`` when the host registered nowhere.

        Read by :func:`~maf_sandbox.maf.sandboxed_tool` so a call's own events go where the
        lifecycle's do, and readable by a host that wants to confirm it is recording nothing.
        """
        return self._observer

    def _effective_floor(self, spec: SandboxSpec) -> Isolation:
        """The stricter of the host's floor and the spec's — a spec may raise, never lower."""
        if spec.min_isolation is None:
            return self._min_isolation
        return max(self._min_isolation, spec.min_isolation, key=ISOLATION_RANK.__getitem__)

    def effective_isolation_scope(self, spec: SandboxSpec) -> IsolationScope:
        """The stricter of the host's scope floor and the spec's — a spec may raise, never lower.

        Public because a caller has to build the key from it: whether a key carries a
        ``call_id`` is what makes a sandbox call-scoped, and the answer is not in the spec alone.
        :class:`~maf_sandbox.maf.SandboxToolSession` reads it per call to fill the key;
        :func:`~maf_sandbox.maf.sandboxed_tool` reads it once at attach, for what the tool
        declares.

        The answer is always a member.  :class:`SandboxSpec` normalises the field it is built
        with, and this coerces again rather than trusting that from a distance: every gate that
        makes the scope a boundary is an ``is``, and a caller reaching past the constructor would
        otherwise be handed a string that fails all of them.
        """
        return IsolationScope(
            str(
                max(
                    self._min_isolation_scope,
                    spec.isolation_scope,
                    key=ISOLATION_SCOPE_RANK.__getitem__,
                )
            )
        )

    def _effective_cleanup_floor(self, spec: SandboxSpec) -> Cleanup:
        """The stricter of the host's floor and the spec's — a spec may raise, never lower."""
        if spec.min_cleanup is None:
            return self._min_cleanup
        return max(self._min_cleanup, spec.min_cleanup, key=CLEANUP_RANK.__getitem__)

    def effective_cleanup(self, spec: SandboxSpec) -> Cleanup:
        """Resolve cleanup from the serving backend, workload evidence and host/spec floors.

        CALL scope always disposes. Other scopes choose the cheapest established rung meeting
        both floors; DISPOSE is always available. An unservable spec raises the same refusal as
        ensure_can_serve."""
        backend = self._refuse_unless_backend_can_serve(spec)
        return self._cleanup_on(backend, spec)

    def _cleanup_on(self, backend: SandboxBackend, spec: SandboxSpec) -> Cleanup:
        """Resolve cleanup on the chosen backend, ignoring unknown capabilities."""
        if self.effective_isolation_scope(spec) is IsolationScope.CALL:
            return Cleanup.DISPOSE
        declared = _declared_set(
            backend, cast("object", _declarations(backend).capabilities), "capabilities"
        )
        known = {str(member) for member in Capability}
        capabilities = frozenset(
            Capability(str(capability)) for capability in declared if str(capability) in known
        )
        return resolve_cleanup(
            established_cleanup(spec, capabilities), self._effective_cleanup_floor(spec)
        )

    def _refuse_unless_backend_can_serve(self, spec: SandboxSpec) -> SandboxBackend:
        """The backend that will serve ``spec``, or raise saying why none of them will.

        The REFUSING half of the policy, shared by :meth:`ensure_can_serve` and
        :meth:`acquire`, and the one place the two selections meet: under
        :data:`Selection.FIXED` the candidate list is the single resolved backend, so this is
        exactly the one check this router has always run.

        Callers guarantee at least one candidate. :meth:`ensure_can_serve` is where the
        no-backend case returns instead — nothing runs there, so nothing reaches anything.
        """
        self._refuse_host_denials(spec)
        served, passed_over = self._route(spec)
        if served is None:
            raise self._nothing_can_serve(spec, passed_over)
        return served

    def _route(
        self, spec: SandboxSpec
    ) -> tuple[SandboxBackend | None, list[tuple[SandboxBackend, Exception]]]:
        """The first candidate that can serve ``spec``, and each one refused ahead of it.

        Selection uses the spec and current declarations, never load, health, latency or cost.
        An admitted call retains its chosen backend through cleanup.
        """
        passed_over: list[tuple[SandboxBackend, Exception]] = []
        for backend in self._candidates:
            refusal = self._refusal_serving(backend, spec)
            if refusal is None:
                return backend, passed_over
            passed_over.append((backend, refusal))
        return None, passed_over

    def _refusal_serving(self, backend: SandboxBackend, spec: SandboxSpec) -> Exception | None:
        """``backend``'s reason for not serving ``spec``, or ``None`` when it can serve it.

        Caught through :data:`ATTACH_REFUSALS` rather than a list written here, because that
        tuple has a test deriving its membership from this module: a refusal added later joins
        the routing automatically instead of escaping it as an unrelated error.
        """
        try:
            self._refuse_unless_this_backend_can_serve(backend, spec)
        except ATTACH_REFUSALS as refusal:
            return refusal
        return None

    def _nothing_can_serve(
        self, spec: SandboxSpec, passed_over: Sequence[tuple[SandboxBackend, Exception]]
    ) -> Exception:
        """The refusal to raise when routing reached the end of the preference order.

        The **most preferred** candidate's own refusal, with the rest named after it, and its
        *type* is preserved deliberately: these classes are exported, hosts catch them
        individually, and :data:`ATTACH_REFUSALS` is what ``SandboxToolSession`` matches on —
        so a new class invented here would be caught by nobody who catches
        :class:`SandboxCapabilityNotSupported` today.

        With one candidate the message is returned untouched, so a single-backend host sees
        exactly what it has always seen and every refusal sentence already written stays the
        sentence a reader meets.
        """
        first, refusal = passed_over[0]
        if len(passed_over) == 1:
            return refusal
        rest = ", ".join(
            f"{backend.name!r} ({type(other).__name__})" for backend, other in passed_over[1:]
        )
        return type(refusal)(
            f"{refusal}\n\nThat is sandbox backend {first.name!r}'s refusal, and it is the one "
            f"raised because registration order is this router's preference order. Every other "
            f"registered backend was tried for the {spec.kind!r} workload, in that order, and "
            f"refused it too: {rest}. Nothing was served and nothing was created. Register a "
            "backend that can serve this spec, in the position you want it reached, or narrow "
            "what the workload asks for."
        )

    def _refuse_host_denials(self, spec: SandboxSpec) -> None:
        """The two refusals no backend can soften, raised once rather than once per candidate.

        ``denied_capabilities`` and ``denied_identities`` are statements about the spec
        against this host's posture, not about what a backend could do, so routing has nothing
        to offer them: there is no next backend to try.
        """
        denied_capabilities = spec.required_capabilities & self._denied_capabilities
        if denied_capabilities:
            raise SandboxCapabilityDenied(
                f"the {spec.kind!r} workload requires "
                f"{', '.join(sorted(str(capability) for capability in denied_capabilities))}, "
                "which this host's router denies outright (denied_capabilities). A hard stop "
                "rather than a missing feature: whatever backend is registered, this posture "
                "refuses the capability — serve the workload on a host that permits it, or "
                "narrow what it requires."
            )
        denied_identities = spec.identities & self._denied_identities
        if denied_identities:
            raise SandboxIdentityDenied(
                f"the {spec.kind!r} workload's host tools exercise "
                f"{', '.join(sorted(str(identity) for identity in denied_identities))} "
                "authority, which this host's router denies outright (denied_identities). "
                "Remove the tools declaring that identity from the workload's registry, or "
                "serve it on a host whose posture permits them."
            )

    def _refuse_unless_this_backend_can_serve(
        self, backend: SandboxBackend, spec: SandboxSpec
    ) -> None:
        """Raise unless ``backend`` may serve ``spec``: floor, capabilities, guest shape,
        limits, egress, scope.

        One backend's half of the policy, with the host's own denials left to
        :meth:`_refuse_host_denials` — everything here is a question about *this* backend, so
        everything here is a question routing can answer by trying the next one.
        """
        declarations = _declarations(backend)
        floor = self._effective_floor(spec)
        declared = _declared_isolation(backend)
        if not meets_floor(declared, floor):
            raise SandboxBackendNotPermitted(
                f"the {spec.kind!r} workload requires at least {str(floor)!r} isolation, and "
                f"sandbox backend {backend.name!r} declares {str(declared)!r} "
                f"(ladder, weakest first: {_LADDER}). A spec may raise this host's floor and "
                "never lower it, so the workload is refused here rather than served behind a "
                "boundary it was written not to trust."
            )

        capabilities = _declared_set(
            backend, cast("object", declarations.capabilities), "capabilities"
        )
        missing = spec.required_capabilities - capabilities
        if missing:
            raise SandboxCapabilityNotSupported(
                f"sandbox backend {backend.name!r} does not support "
                f"{', '.join(sorted(missing))}, which the {spec.kind!r} workload requires "
                f"(it declares "
                f"{', '.join(sorted(str(c) for c in capabilities)) or 'nothing'}). Refused "
                "rather than attempted: a workload that reaches for a capability the backend "
                "never implemented fails inside the sandbox, where the reason is hardest to "
                "see."
            )

        tokens = declarations.egress_method_tokens
        if tokens is not None:
            supported = _declared_set(backend, cast("object", tokens), "egress_method_tokens")
            requested = {
                method
                for entry in spec.egress_allow
                if isinstance(entry, EgressRule)
                for method in entry.methods or ()
            }
            unsupported = requested - supported
            if unsupported:
                raise SandboxCapabilityNotSupported(
                    f"sandbox backend {backend.name!r} cannot enforce egress methods "
                    f"{', '.join(sorted(unsupported))} as written for {spec.kind!r}"
                )

        # After the capability match and before the ceilings, because it is the same kind of
        # question the capability match asks — can this backend serve this workload at all —
        # and a workload refused for the wrong guest shape was never going to reach a transfer.
        if spec.requires_os_family is not None:
            families = _declared_os_families(declarations)
            if spec.requires_os_family not in families:
                served = ", ".join(sorted(str(family) for family in families))
                raise SandboxOsFamilyNotSupported(
                    f"sandbox backend {backend.name!r} hands out "
                    f"{served or 'no guest whose shape it states'}, and the {spec.kind!r} "
                    f"workload is written for a {str(spec.requires_os_family)!r} guest. Its "
                    "commands, its scripts and the paths it composes assume that shape, so "
                    "running it here would fail inside the sandbox at the first command "
                    "rather than here. Register a backend serving that family, or attach a "
                    "workload written for the one this backend has."
                )

        limits = _declared_limits(backend, declarations)
        asked_in, asked_out = spec.files_in, spec.files_out
        if spec.host_tools is not None:
            # The transport moves its own files, bounded by the registry rather than by what the
            # workload declared. Fold that worst case in transiently, so a backend that cannot
            # serve it is refused here rather than overrun mid-run. The spec's stored caps stay
            # untouched: the kind's runtime tally enforces against those, and folding the stored
            # values would double-count the transport against the workload's own budget.
            folded = fold_host_tool_call_transfer_limits(
                spec.files_in, spec.files_out, spec.host_tools
            )
            asked_in, asked_out = folded.files_in, folded.files_out
        for direction, asked, declared, ceiling in (
            (Capability.FILES_IN, asked_in, spec.files_in, limits.files_in),
            (Capability.FILES_OUT, asked_out, spec.files_out, limits.files_out),
        ):
            if not asked.within(ceiling):
                # Only when the fold is what caused *this* refusal — the bare declaration would
                # have been served. A workload already over the ceiling on its own must not be
                # pointed at the transport, however much the fold also raised.
                folded_note = (
                    " (folded to include the wired host tools' call transport, so above the "
                    "workload's own declaration)"
                    if declared.within(ceiling)
                    else ""
                )
                raise SandboxTransferLimitsNotPermitted(
                    f"the {spec.kind!r} workload declares {str(direction)} limits above what "
                    f"sandbox backend {backend.name!r} allows: it asks for {asked}"
                    f"{folded_note} and the backend permits {ceiling}. Refused rather than "
                    "clamped: a workload served a smaller cap than it declared fails part-way "
                    "through a collection, and a partial artifact set is worse than none because "
                    "the model cannot tell what it did not get."
                )

        # Egress is resolved, not matched: the workload runs in exactly one mode, and the
        # backend must be able to enforce it. Refuse, never degrade — no more-open substitute
        # (a silent widening) and no more-isolated one (a quietly different posture). See
        # docs/sandbox/research/egress-resolution.md.
        modes = _declared_set(backend, cast("object", declarations.egress_modes), "egress_modes")
        if spec.egress not in modes:
            enforced = ", ".join(sorted(str(mode) for mode in modes)) or "nothing"
            raise SandboxEgressNotEnforced(
                f"sandbox backend {backend.name!r} cannot enforce the {str(spec.egress)!r} "
                f"egress the {spec.kind!r} workload runs in (it enforces {enforced}). A workload "
                "is served in exactly the mode it declares or refused — never a different one, "
                "because a more open mode silently widens what it reaches and a more isolated "
                "one changes the posture it was built for."
            )

        # Resolved rather than matched, for the reason egress is: a workload runs at exactly one
        # scope. Why it is refused rather than served down a rung is `SandboxScopeNotEnforced`.
        scope = self.effective_isolation_scope(spec)
        scopes = _declared_isolation_scopes(backend, declarations)
        if scope not in scopes:
            serves = ", ".join(sorted(str(one) for one in scopes))
            raise SandboxScopeNotEnforced(
                f"sandbox backend {backend.name!r} cannot serve the {spec.kind!r} workload "
                f"one sandbox per {str(scope)} (it serves one per {serves}). A backend declares "
                f"{str(IsolationScope.CALL)!r} once it folds the key's call_id into whatever "
                "names a sandbox — until it does, two calls asking not to share would be handed "
                "the same one."
            )

    def ensure_can_serve(self, spec: SandboxSpec) -> None:
        """Raise unless ``spec`` may be served: denials, floor, capabilities, guest shape,
        limits, egress, scope.

        Called for you by :func:`maf_sandbox.maf.sandboxed_tool`, and it is also the whole of
        a host's own wiring test::

            router.ensure_can_serve(bicep_sandbox_spec())

        The spec's ``egress`` mode is resolved against the backend: served iff the backend
        enforces it, refused otherwise — never a different mode (see :class:`~maf_sandbox.Egress`
        and ``docs/sandbox/research/egress-resolution.md``).  With no backend configured this
        returns: nothing runs, so nothing reaches anything.

        Raises:
            SandboxCapabilityDenied: when the spec requires a capability this host denies.
            SandboxIdentityDenied: when the spec's ``identities`` carry one this host denies.
            SandboxBackendNotPermitted: when the backend's declarations cannot be read, or
                when the spec raises the floor above what the backend declares.
            SandboxCapabilityNotSupported: when the backend cannot do what the spec requires.
            SandboxOsFamilyNotSupported: when the spec asks for a guest shape the backend
                does not hand out.
            SandboxTransferLimitsNotPermitted: when the spec's caps exceed the backend's,
                or when the backend declares its ceilings as something other than a
                ``SandboxLimits``.
            SandboxEgressNotEnforced: when the backend cannot enforce the spec's egress mode.
            SandboxScopeNotEnforced: when the backend cannot serve the workload at the isolation
                scope this host and the spec resolve to.
        """
        if not self._candidates:
            return
        self._refuse_unless_backend_can_serve(spec)

    def backend_for(self, spec: SandboxSpec) -> SandboxBackend | None:
        """Which backend would serve ``spec``, or ``None`` when none would.

        The routed counterpart to :attr:`backend`, and the form of the question that has an
        answer under either selection: :meth:`ensure_can_serve` says *whether*, this says
        *which*. It refuses nothing and raises nothing, so a caller wanting the reason asks
        the other one.

        Nothing is created and nothing is reached — the answer comes from declarations this
        router already holds. It is also stable: the route is a pure function of the spec, so
        asking twice cannot name two backends.
        """
        if not self._candidates:
            return None
        if spec.required_capabilities & self._denied_capabilities:
            return None
        if spec.identities & self._denied_identities:
            return None
        return self._route(spec)[0]

    def _record_disposal(
        self,
        key: SandboxKey,
        backend: SandboxBackend,
        failure: DisposalFailure | None,
        started: float,
    ) -> None:
        """Record one backend's answer to one disposal, wherever the disposal was asked from.

        Three places ask, and only one of them is the sweep: an acquire refused mid-create and
        an acquire refused over a sandbox nothing can reclaim each delete on the one backend
        that served them, directly.  A record that covered the sweep alone would show the two
        deletes that answer a refusal as never having happened.
        """
        if self._observer is None:
            return
        record(
            self._observer,
            SandboxDisposed(
                key=key,
                backend=_recorded_name(backend),
                outcome=_reported(failure),
                failure=failure,
                seconds=time.monotonic() - started,
                call=recorded_call(),
            ),
            logger,
        )

    def _record_an_interrupted_disposal(
        self, key: SandboxKey, backend: SandboxBackend, started: float, by: BaseException
    ) -> None:
        """Record a disposal interrupted mid-flight, with ``by`` named in the detail.

        The backend was asked and never answered, so whether the delete landed is unknowable
        rather than merely unclassified.  ``by`` is named because this is reached from a
        ``BaseException`` catch, which sees more than a cancel.

        The no-observer check is repeated here rather than left to ``_record_disposal``, because
        everything below it is record-only work: building the failure reads ``backend.name``,
        and that property can raise — replacing the interruption this was called to report with
        one of its own, for a host that registered no observer at all.
        """
        if self._observer is None:
            return
        self._record_disposal(
            key,
            backend,
            DisposalFailure(
                "unknown",
                f"{_recorded_name(backend)}: the disposal was interrupted by {type(by).__name__}",
            ),
            started,
        )

    def _record_purge(
        self,
        scope: str,
        thread_id: str,
        backend: SandboxBackend,
        disposed: int,
        failure: DisposalFailure | None,
        started: float,
    ) -> None:
        """Record one backend's answer to one conversation's purge.

        ``disposed`` is this backend's own count rather than the sweep's, the way
        :meth:`_record_disposal` records this backend's own answer.
        """
        if self._observer is None:
            return
        record(
            self._observer,
            ScopeDisposed(
                scope=scope,
                thread_id=thread_id,
                backend=_recorded_name(backend),
                outcome=_reported(failure),
                disposed=disposed,
                failure=failure,
                seconds=time.monotonic() - started,
                call=recorded_call(),
            ),
            logger,
        )

    def _record_an_interrupted_purge(
        self,
        scope: str,
        thread_id: str,
        backend: SandboxBackend,
        started: float,
        by: BaseException,
    ) -> None:
        """Record a purge interrupted mid-flight, with ``by`` named in the detail.

        The no-observer check is repeated here for the reason
        :meth:`_record_an_interrupted_disposal` gives: everything below it reads
        ``backend.name``, which is somebody else's property.
        """
        if self._observer is None:
            return
        self._record_purge(
            scope,
            thread_id,
            backend,
            0,
            DisposalFailure(
                "unknown",
                f"{_recorded_name(backend)}: the purge was interrupted by {type(by).__name__}",
            ),
            started,
        )

    def _disposal_lock(self, key: SandboxKey) -> asyncio.Lock:
        """The disposal lock for one key on the running loop (see ``__init__``)."""
        per_loop = self._disposal_locks.setdefault(
            asyncio.get_running_loop(), weakref.WeakValueDictionary()
        )
        lock = per_loop.get(key)
        if lock is None:
            lock = per_loop[key] = asyncio.Lock()
        # Returned, so the caller's reference is what keeps it in the table: two disposals
        # overlapping both hold it and share it, and it goes when neither does.
        return lock

    async def _refuse_a_key_closed_during_the_create(
        self, key: SandboxKey, backend: SandboxBackend, *, kind: str, instance_id: str | None
    ) -> None:
        """Dispose the refused acquire without clearing another instance's pending cleanup."""
        if self._reclaim.failed_reclaim_policy is not FailedReclaimPolicy.KEEP:
            self.mark_unclean(key, backend=backend, kind=kind, instance_id=instance_id)
        try:
            async with asyncio.timeout(self._reclaim.timeout):
                async with self._disposal_lock(key):
                    failure = await self._dispose_the_kind(
                        key,
                        SandboxSpec(kind=kind),
                        backend,
                        None,
                        self._reclaim.timeout,
                        instance_id=instance_id,
                    )
        except TimeoutError:
            failure = "the refused acquire's disposal timed out"
        _, reported = self._unclean_state(key)
        outcome = "disposed" if failure is None else "could not be disposed either"
        raise SandboxUnclean(
            f"the sandbox for {key.scope}/{key.thread_id}/{key.agent_dir} was refused "
            f"while this acquire was creating it; the {kind!r} instance {outcome}",
            code=reported.code if reported is not None else None,
        )

    async def acquire(
        self, key: SandboxKey, spec: SandboxSpec, *, _admission: CallAdmission | None = None
    ) -> Sandbox:
        """Return a running sandbox for ``key``, creating one if needed.

        Runs the same floor, capability, limit and egress checks as :meth:`ensure_can_serve`
        before ever reaching the backend, so a caller that skips :meth:`ensure_can_serve` is
        still refused rather than served behind a boundary or capability set the spec did not
        agree to.  Under :data:`Selection.PER_SPEC` those checks are also what *chooses* the
        backend, and everything after the create — the reclaim refusal's disposal, the
        mid-create disposal — is aimed at the one that served rather than at all of them.

        Every way out is recorded as one :class:`~maf_sandbox.SandboxAcquired`, and a way out
        that *served* also leaves an :class:`~maf_sandbox.EffectiveState` for whoever is
        collecting one — see :func:`~maf_sandbox.maf.effective_state_middleware`.  Both are
        skipped whole when nobody is listening for either.

        Raises:
            NoSandboxBackend: when no backend is configured. Callers that check
                :attr:`enabled` before attaching a tool never reach this.
            SandboxUnclean: when a previous call left this key's sandbox unclean and no disposal
                has since landed. An expected outcome for a direct consumer, not a backend
                failure: the refusal persists until :meth:`dispose_unclean` or
                :meth:`dispose_scope` succeeds for the key. Raised for a key closed *while*
                this call was creating its sandbox too, and that sandbox is disposed first.
            SandboxCapabilityDenied: when the spec requires a capability this host denies.
            SandboxIdentityDenied: when the spec's ``identities`` carry one this host denies.
            SandboxBackendNotPermitted: when the backend's declarations cannot be read, or
                when the spec raises the floor above what the backend declares.
            SandboxCapabilityNotSupported: when the backend cannot do what the spec requires.
            SandboxOsFamilyNotSupported: when the spec asks for a guest shape the backend
                does not hand out.
            SandboxTransferLimitsNotPermitted: when the spec's caps exceed the backend's,
                or when the backend declares its ceilings as something other than a
                ``SandboxLimits``.
            SandboxEgressNotEnforced: when the backend cannot confine egress to this spec.
            SandboxScopeNotEnforced: when the backend cannot serve the workload at the isolation
                scope this host and the spec resolve to.
            ValueError: when ``key`` and the workload's effective scope disagree — a
                per-call workload whose key names no call, which get-or-create would serve by
                sharing, or a conversation-scoped one whose key names a call, whose sandbox the
                cleanup would then delete out from under the conversation.
            TypeError: when the backend hands back a sandbox without :meth:`Sandbox.reclaim`
                or a nonempty :attr:`Sandbox.instance_id`.
                That sandbox is disposed (this backend, best effort) before the refusal
                reaches the caller: a backend that cannot reclaim can never clean it, and a
                refused acquire must not leave a billable sandbox running.
        """
        if self._observer is None and not effective_state_is_noted():
            return await self._acquire(key, spec, _Serving(), _admission)
        serving = _Serving()
        started = time.monotonic()
        refusal: str | None = None
        try:
            return await self._acquire(key, spec, serving, _admission)
        except BaseException as exc:
            refusal = type(exc).__name__
            raise
        finally:
            isolation, declarations = _recorded_declarations(serving.backend)
            acquired = SandboxAcquired(
                key=key,
                # A snapshot, because `SandboxSpec` is frozen only shallowly: `labels` is a
                # plain dict the caller keeps a reference to. Handing the live one over
                # would let a later write change a record already delivered, and let an
                # observer write back into the caller's spec through the event.
                spec=_with_snapshotted_labels(spec),
                isolation_scope=self.effective_isolation_scope(spec),
                backend=(None if serving.backend is None else _recorded_name(serving.backend)),
                isolation=isolation,
                declarations=declarations,
                seconds=time.monotonic() - started,
                refusal=refusal,
                call=recorded_call(),
            )
            # The observer first, because that ordering is the one that already existed.
            record(self._observer, acquired, logger)
            _note_what_held(acquired)

    async def _acquire(
        self,
        key: SandboxKey,
        spec: SandboxSpec,
        serving: _Serving,
        admission: CallAdmission | None,
    ) -> Sandbox:
        owner = str(id(serving))
        await self._adoptions.take(
            key, spec.kind, owner=owner, exclusive=True, timeout=QUEUED_CALL_TIMEOUT
        )
        try:
            return await self._acquire_and_adopt(key, spec, serving, admission)
        finally:
            self._adoptions.release(key, spec.kind, owner=owner)

    async def _acquire_and_adopt(
        self,
        key: SandboxKey,
        spec: SandboxSpec,
        serving: _Serving,
        admission: CallAdmission | None,
    ) -> Sandbox:
        """The whole of :meth:`acquire`, split so one record covers every way out of it.

        ``serving`` is filled in as soon as a backend is chosen, so a refusal raised *after* the
        create still names the backend that served it — which the return value cannot.
        """
        if not self._candidates:
            raise NoSandboxBackend("no sandbox backend is configured")
        refused, reported = self._unclean_state(key)
        if refused:
            # The code only: a detail can carry an endpoint or a raw response body, and this
            # message reaches hosts that do not sanitize. The detail is in the log beside it.
            because = f" ({reported.code})" if reported is not None else ""
            raise SandboxUnclean(
                f"the sandbox for {key.scope}/{key.thread_id}/{key.agent_dir} was left unclean — "
                "a tool call's data could not be removed, or a program it started may still be "
                f"running — and disposing it did not land{because}. It is refused until a "
                "disposal lands — dispose(key) or dispose_scope(scope, thread_id) — rather than "
                "served unclean.",
                code=reported.code if reported is not None else None,
            )
        if admission is None:
            served = self._refuse_unless_backend_can_serve(spec)
        else:
            served = admission.backend
            self._refuse_host_denials(spec)
            self._refuse_unless_this_backend_can_serve(served, spec)
        serving.backend = served
        scope = self.effective_isolation_scope(spec)
        if scope is IsolationScope.CALL and not key.call_id:
            raise ValueError(
                f"the {spec.kind!r} workload runs one sandbox per call and this key names no "
                "call (call_id is empty), so get-or-create would hand it the conversation's "
                "sandbox — the sharing the scope refuses. A key comes from "
                "SandboxToolSession.key(), which fills call_id at this scope; a caller building "
                "its own supplies one that is unique per tool call."
            )
        if scope is IsolationScope.CONVERSATION and key.call_id:
            raise ValueError(
                f"the {spec.kind!r} workload runs one sandbox per conversation and this key "
                f"names a call ({key.call_id!r}). A backend serving that scope keys a sandbox by "
                "the other three fields, so it would hand back the conversation's — and the "
                "framework reads the scope off the key, so the cleanup would then delete that "
                "shared sandbox at the end of one call. Drop the call id, or raise the "
                "workload's isolation_scope."
            )
        if admission is not None:
            admission.served = True
        snapshot = Capability.SNAPSHOT in _declarations(served).capabilities
        sandbox = await served.acquire(key, spec)
        if self._unclean_state(key)[0]:
            # Read again after the create: the check above is only as fresh as the moment
            # before the await, and a disposal that begins during it closes the key without
            # this call ever seeing the mark. One that began earlier is caught above, since a
            # disposal marks the key before its own first await.
            await self._refuse_a_key_closed_during_the_create(
                key, served, kind=spec.kind, instance_id=_instance_id(sandbox)
            )
        try:
            _refuse_an_invalid_sandbox(sandbox)
        except TypeError:
            await self._dispose_the_kind(
                key,
                spec,
                served,
                "the acquired sandbox violates the protocol",
                self._reclaim.timeout,
                instance_id=_instance_id(sandbox),
            )
            raise
        if scope is not IsolationScope.CALL:
            sandbox = await self._adopt(key, spec, served, sandbox, snapshot=snapshot)
        else:
            self._remember_instance(key, spec.kind, served, sandbox)
        return sandbox

    async def _adopt(
        self,
        key: SandboxKey,
        spec: SandboxSpec,
        backend: SandboxBackend,
        sandbox: Sandbox,
        *,
        snapshot: bool,
    ) -> Sandbox:
        """Clean an unfamiliar instance before any input reaches it, under the acquire gate."""
        at = (key, spec.kind, id(backend))
        with self._seen_guard:
            if sandbox.instance_id in self._seen.get(at, set()):
                return sandbox
        bound = self._reclaim.timeout
        instance_id = sandbox.instance_id
        if snapshot:
            try:
                async with asyncio.timeout(bound):
                    previous = await _reset_instance(sandbox, timeout=bound)
            except (asyncio.CancelledError, GeneratorExit):
                if self._reclaim.failed_reclaim_policy is not FailedReclaimPolicy.KEEP:
                    self.mark_unclean(
                        key,
                        backend=backend,
                        kind=spec.kind,
                        instance_id=_instance_id(sandbox) or instance_id,
                    )
                raise
            except Exception as unreset:  # noqa: BLE001 — disposal is the fallback
                logger.warning("sandbox adoption reset failed: %s", error_detail(unreset))
            else:
                if self._unclean_state(key)[0]:
                    await self._refuse_a_key_closed_during_the_create(
                        key, backend, kind=spec.kind, instance_id=_instance_id(sandbox)
                    )
                self._remember_instance(key, spec.kind, backend, sandbox, previous=previous)
                return sandbox
        failure = await self._dispose_the_kind(
            key, spec, backend, None, bound, instance_id=_instance_id(sandbox) or instance_id
        )
        if failure is not None:
            if self._reclaim.failed_reclaim_policy is not FailedReclaimPolicy.KEEP:
                _, reported = self._unclean_state(key)
                raise SandboxUnclean(
                    "the unfamiliar sandbox could not be cleaned before use",
                    code=None if reported is None else reported.code,
                )
        else:
            sandbox = await backend.acquire(key, spec)
            try:
                _refuse_an_invalid_sandbox(sandbox)
            except TypeError:
                await self._dispose_the_kind(
                    key,
                    spec,
                    backend,
                    None,
                    bound,
                    instance_id=_instance_id(sandbox),
                )
                raise
        if self._unclean_state(key)[0]:
            await self._refuse_a_key_closed_during_the_create(
                key, backend, kind=spec.kind, instance_id=_instance_id(sandbox)
            )
        _refuse_an_invalid_sandbox(sandbox)
        self._remember_instance(key, spec.kind, backend, sandbox)
        return sandbox

    def _remember_instance(
        self,
        key: SandboxKey,
        kind: str,
        backend: SandboxBackend,
        sandbox: Sandbox,
        *,
        previous: str | None = None,
    ) -> None:
        with self._seen_guard:
            known = self._seen.setdefault((key, kind, id(backend)), set())
            if previous is not None:
                known.discard(previous)
            known.add(sandbox.instance_id)
            served = self._served.setdefault((key, kind, id(backend)), (backend, set()))[1]
            if previous is not None:
                served.discard(previous)
            served.add(sandbox.instance_id)

    def _forget_instances(
        self,
        backend: SandboxBackend,
        *,
        key: SandboxKey | None = None,
        kind: str | None = None,
        instance_id: str | None = None,
        scope: str | None = None,
        thread_id: str | None = None,
    ) -> None:
        with self._seen_guard:
            for at in self._seen.keys() | self._served.keys():
                held, workload, provider = at
                if (
                    provider == id(backend)
                    and (key is None or held == key)
                    and (kind is None or workload == kind)
                    and (scope is None or held.scope == scope)
                    and (thread_id is None or held.thread_id == thread_id)
                ):
                    if instance_id is None:
                        self._seen.pop(at, None)
                        self._served.pop(at, None)
                    else:
                        known = self._seen.get(at)
                        if known is not None:
                            known.discard(instance_id)
                            if not known:
                                self._seen.pop(at, None)
                        served = self._served.get(at)
                        if served is not None:
                            served[1].discard(instance_id)
                            if not served[1]:
                                self._served.pop(at, None)

    async def enter_call(
        self,
        key: SandboxKey,
        spec: SandboxSpec,
        *,
        owner: str,
        timeout: float = QUEUED_CALL_TIMEOUT,
    ) -> CallAdmission:
        """Admit a call and retain the backend and cleanup rung its hold permits.

        RECLAIM takes a shared hold; stronger rungs take an exclusive one. The caller must retain
        the admission for acquire and cleanup, then call finish_call or release_call.
        Raises TimeoutError when incompatible owners outlast the bound."""
        deadline = time.monotonic() + timeout
        while True:
            backend = self._refuse_unless_backend_can_serve(spec)
            rung = self._cleanup_on(backend, spec)
            exclusive = needs_exclusive_use(rung)
            await self._slots.take(
                key,
                spec.kind,
                owner=owner,
                exclusive=exclusive,
                timeout=max(0, deadline - time.monotonic()),
            )
            try:
                self._refuse_host_denials(spec)
                self._refuse_unless_this_backend_can_serve(backend, spec)
                current = self._cleanup_on(backend, spec)
                if needs_exclusive_use(current) == exclusive:
                    return CallAdmission(backend, current)
            except BaseException:
                self._slots.release(key, spec.kind, owner=owner)
                raise
            self._slots.release(key, spec.kind, owner=owner)
            if time.monotonic() >= deadline:
                raise TimeoutError("cleanup requirements changed while waiting for admission")

    def release_call(self, key: SandboxKey, kind: str, *, owner: str) -> None:
        """Release this call's hold without cleaning; an owner holding nothing releases nothing."""
        self._slots.release(key, kind, owner=owner)

    async def finish_call(
        self,
        key: SandboxKey,
        spec: SandboxSpec,
        *,
        admission: CallAdmission,
        sandbox: Sandbox | None,
        owner: str,
        unclean: str | None = None,
        timeout: float | None = None,
        sandboxes: Sequence[Sandbox] | None = None,
    ) -> str | None:
        """Clean under the admitted rung and release its hold.

        Returns a failure reason or None; cancellation propagates. The caller must own an
        exclusive hold. timeout bounds reset and disposal separately, defaulting to the router
        policy; RECLAIM uses the framework's directory removal instead."""
        bound = self._reclaim.timeout if timeout is None else timeout
        try:
            held = {
                one.instance_id: one for one in sandboxes or (() if sandbox is None else (sandbox,))
            }
            failures: list[str] = []
            remaining = list(held.items())
            for index, (_, one) in enumerate(remaining):
                try:
                    failure = await self._run_the_rung(
                        key, spec, admission.backend, admission.rung, one, unclean, bound
                    )
                except (asyncio.CancelledError, GeneratorExit):
                    if self._reclaim.failed_reclaim_policy is not FailedReclaimPolicy.KEEP:
                        for instance_id, unfinished in remaining[index:]:
                            self.mark_unclean(
                                key,
                                backend=admission.backend,
                                kind=spec.kind,
                                instance_id=_instance_id(unfinished) or instance_id,
                            )
                    raise
                if failure is not None:
                    failures.append(failure)
            return "; ".join(failures) or None
        finally:
            self._slots.release(key, spec.kind, owner=owner)

    async def _run_the_rung(
        self,
        key: SandboxKey,
        spec: SandboxSpec,
        backend: SandboxBackend,
        rung: Cleanup,
        sandbox: Sandbox | None,
        unclean: str | None,
        bound: float,
    ) -> str | None:
        """The rung itself, with a reset that failed escalating to the disposal below it."""
        instance_id = None if sandbox is None else sandbox.instance_id
        if rung is Cleanup.RESET and sandbox is not None:
            try:
                async with asyncio.timeout(bound):
                    previous = await _reset_instance(sandbox, timeout=bound)
            except (asyncio.CancelledError, GeneratorExit):
                raise
            except Exception as unreset:  # noqa: BLE001 — escalates rather than propagates
                logger.warning(
                    "sandbox router: resetting the %s sandbox for %s/%s failed, so it is "
                    "disposed instead: %s",
                    _recorded_name(backend),
                    key.scope,
                    key.thread_id,
                    error_detail(unreset),
                )
            else:
                self._remember_instance(key, spec.kind, backend, sandbox, previous=previous)
                return None
            unclean = unclean or "the reset failed"
        return await self._dispose_the_kind(
            key,
            spec,
            backend,
            unclean,
            bound,
            instance_id=(_instance_id(sandbox) or instance_id),
        )

    async def _dispose_the_kind(
        self,
        key: SandboxKey,
        spec: SandboxSpec,
        backend: SandboxBackend,
        unclean: str | None,
        bound: float,
        *,
        instance_id: str | None = None,
    ) -> str | None:
        """Dispose the serving instance; failures retain that exact target for retry."""
        refuse = self._reclaim.failed_reclaim_policy is not FailedReclaimPolicy.KEEP
        if unclean is not None and refuse:
            self.mark_unclean(key, backend=backend, kind=spec.kind, instance_id=instance_id)
        pending = self._pending_for(key, [backend], spec.kind, instance_id)
        started = time.monotonic()
        try:
            async with asyncio.timeout(bound):
                reported = await backend.dispose(key, kind=spec.kind, instance_id=instance_id)
        except TimeoutError:
            reported = DisposalFailure("timeout", f"the delete did not finish within {bound:g}s")
        except (asyncio.CancelledError, GeneratorExit) as interrupted:
            self._record_an_interrupted_disposal(key, backend, started, interrupted)
            if self._reclaim.failed_reclaim_policy is not FailedReclaimPolicy.KEEP:
                self.mark_unclean(key, backend=backend, kind=spec.kind, instance_id=instance_id)
            raise
        except Exception as undisposed:  # noqa: BLE001 — a `finally` must not raise over a result
            reported = str(undisposed)
        failure = None if reported is None else _coded(_recorded_name(backend), reported)
        self._record_disposal(key, backend, failure, started)
        if failure is None:
            self._forget_instances(backend, key=key, kind=spec.kind, instance_id=instance_id)
            self._forget_pending(key, pending)
            return None
        logger.warning(
            "sandbox router: the cleanup disposal for %s/%s (%s) did not land: %s",
            key.scope,
            key.thread_id,
            spec.kind,
            failure,
        )
        if self._reclaim.failed_reclaim_policy is not FailedReclaimPolicy.KEEP:
            # A failed delete leaves the call's residue available to the next acquire.
            self.mark_unclean(
                key, failure, backend=backend, kind=spec.kind, instance_id=instance_id
            )
        return f"{failure}" if unclean is None else f"{unclean}; {failure}"

    async def dispose(self, key: SandboxKey) -> None:
        """Delete every kind's sandbox for ``key``. Best-effort across every registered backend."""
        async with self._disposal_lock(key):
            await self._dispose_each(key)

    async def dispose_kind(
        self, key: SandboxKey, kind: str, *, instance_id: str | None = None, timeout: float
    ) -> bool:
        """Sweep a kind, or delete one engine instance on the backend that served it.

        timeout bounds the lock wait and all deletes. Failed instance cleanup refuses the key
        unless KEEP was chosen; host sweeps create no refusal. Success retires only covered
        attempts. Returns False on failure or timeout; cancellation propagates. Hosts must
        coordinate active calls before disposal.
        """
        if not isinstance(cast(object, kind), str):
            raise TypeError("kind must be a string; use dispose(key) to delete every kind")
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError(f"timeout must be a finite positive number of seconds, not {timeout}")
        try:
            async with asyncio.timeout(timeout):
                async with self._disposal_lock(key):
                    backends = self._backends_for_instance(key, kind, instance_id)
                    if instance_id is not None:
                        landed = True
                        for backend in backends:
                            failure = await self._dispose_the_kind(
                                key,
                                SandboxSpec(kind=kind),
                                backend,
                                None,
                                timeout,
                                instance_id=instance_id,
                            )
                            landed = failure is None and landed
                        return landed
                    return await self._dispose_each(key, kind=kind)
        except (asyncio.CancelledError, GeneratorExit):
            if (
                instance_id is not None
                and self._reclaim.failed_reclaim_policy is not FailedReclaimPolicy.KEEP
            ):
                for backend in self._backends_for_instance(key, kind, instance_id):
                    self.mark_unclean(key, backend=backend, kind=kind, instance_id=instance_id)
            raise
        except TimeoutError:
            if (
                instance_id is not None
                and self._reclaim.failed_reclaim_policy is not FailedReclaimPolicy.KEEP
            ):
                for backend in self._backends_for_instance(key, kind, instance_id):
                    self.mark_unclean(
                        key,
                        DisposalFailure("timeout", "instance disposal timed out"),
                        backend=backend,
                        kind=kind,
                        instance_id=instance_id,
                    )
            logger.warning(
                "sandbox router: disposing %s/%s/%s (%s) did not finish within %ss",
                key.scope,
                key.thread_id,
                key.agent_dir,
                kind,
                timeout,
            )
            return False

    async def _dispose_each(
        self,
        key: SandboxKey,
        *,
        refuse: bool = False,
        backends: Sequence[SandboxBackend] | None = None,
        kind: str | None = None,
        instance_id: str | None = None,
        pending: Sequence[_PendingDisposal] | None = None,
    ) -> bool:
        """Dispose the selected targets and retire only records this attempt covered."""
        selected = self._backends if backends is None else backends
        if pending is None:
            pending = self._pending_for(key, selected, kind, instance_id)
        reasons: list[DisposalFailure] = []
        for backend in selected:
            started = time.monotonic()
            # This backend's own answer, kept apart from `reasons` — which accumulates across
            # the sweep, so its tail is not what this one said.
            answered: DisposalFailure | None = None
            try:
                undisposed = (
                    await backend.dispose(key)
                    if kind is None and instance_id is None
                    else await backend.dispose(key, kind=kind, instance_id=instance_id)
                )
            except Exception as exc:  # noqa: BLE001 - disposal must not fail a caller
                # Nothing a backend says while breaking never-raises can be classified.
                answered = DisposalFailure("unknown", f"{backend.name} raised: {exc}")
                reasons.append(answered)
                logger.warning(
                    "sandbox router: backend %s failed to dispose: %s", backend.name, exc
                )
            except BaseException as interrupted:
                # The bound expiring mid-sweep is the case this catch exists for: it cancels
                # the backend that was mid-dispose, and that backend is the one the record is
                # about. The `reasons` fold below never runs, so this is its only event.
                self._record_an_interrupted_disposal(key, backend, started, interrupted)
                raise
            else:
                if undisposed is not None:
                    answered = _coded(backend.name, undisposed)
                    reasons.append(answered)
                    logger.warning(
                        "sandbox router: backend %s did not dispose %s/%s/%s: %s",
                        backend.name,
                        key.scope,
                        key.thread_id,
                        key.agent_dir,
                        undisposed,
                    )
            self._record_disposal(key, backend, answered, started)
            if answered is None:
                self._forget_instances(backend, key=key, kind=kind, instance_id=instance_id)
                self._forget_pending(key, [one for one in pending if one.backend is backend])
            if refuse and answered is not None:
                with self._unclean_guard:
                    targets = self._pending_disposals.setdefault(key, {})
                    targets.setdefault(
                        (id(backend), kind, instance_id),
                        _PendingDisposal(backend, kind, instance_id),
                    )
                    recorded = self._unclean.get(key)
                    self._unclean[key] = fold_disposal_failures(
                        [answered] if recorded is None else [recorded, answered]
                    )
        if reasons:
            return False
        return True

    async def dispose_call(
        self,
        key: SandboxKey,
        *,
        timeout: float,
        spec: SandboxSpec | None = None,
        _admission: CallAdmission | None = None,
    ) -> bool:
        """Delete the sandbox a call-scoped key owns, bounded, and say whether it landed.

        :meth:`dispose_unclean`'s bound and its answer without its ledger entry, because the two
        protect different things.  A key marked unclean refuses the conversation's *next*
        acquire; a call-scoped key has no next acquire, so the entry would never be read and
        never cleared.  What a ``False`` leaves is a sandbox no later call can address — the
        caller reports it, and the conversation's purge is what eventually reaches it.

        ``FailedReclaimPolicy`` is not consulted, and that is the point: it loosens an
        escalation — disposing a sandbox a removal could not clean — where this delete is the
        call's own cleanup and the separation the workload asked for.

        Framework cleanup supplies the retained admission to reach the serving backend without
        reading its declarations again. Direct callers route by ``spec``; without one, a
        per-spec router asks only backends declaring CALL scope, since deleting through a
        conversation-scoped backend could remove a sandbox this call never owned.

        Raises:
            ValueError: when ``key`` names no call, which is a conversation's key and not this
                method's to delete; or when ``timeout`` is not a finite positive number of
                seconds, for the reason :meth:`dispose_unclean` gives.
        """
        if not key.call_id:
            raise ValueError(
                f"dispose_call was given a key naming no call ({key.scope}/{key.thread_id}/"
                f"{key.agent_dir}), which is a conversation's. Deleting it here would take every "
                "kind's sandbox for that conversation and skip the ledger that refuses the key "
                "when the delete does not land — the protection this method drops precisely "
                "because a call-scoped key has no next acquire. Use dispose(key), or "
                "dispose_unclean(key, timeout=...) when a call could not leave it clean."
            )
        if _admission is None:
            serving, sweep = self._serving_for_call(spec)
        else:
            serving = _admission.backend
            sweep = [serving] if _admission.served else []
        if _admission is None and serving is not None:
            serves = _declared_isolation_scopes(serving, _declarations(serving))
            if IsolationScope.CALL not in serves:
                raise ValueError(
                    f"dispose_call was given a key naming a call, and sandbox backend "
                    f"{serving.name!r} does not serve that scope, so it has no sandbox of "
                    "that call's to delete. What it does have is the conversation's, which its "
                    "dispose sweeps by scope, thread and agent — deleting it out from under "
                    "every later call. Use dispose(key) for a conversation."
                )
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError(f"timeout must be a finite positive number of seconds, not {timeout}")
        try:
            async with asyncio.timeout(timeout):
                async with self._disposal_lock(key):
                    # This backend alone. A key minted for one call was served by the backend
                    # this router routed it to and by nothing else, and asking the others
                    # would report their failures as this call's leak. Nothing to ask means
                    # nothing was ever served, which `_dispose_each` answers as a landed
                    # delete.
                    return await self._dispose_each(key, backends=sweep)
        except TimeoutError:
            logger.warning(
                "sandbox router: disposing the call sandbox %s/%s/%s/%s did not finish within %ss",
                key.scope,
                key.thread_id,
                key.agent_dir,
                key.call_id,
                timeout,
            )
            return False

    def _serving_for_call(
        self, spec: SandboxSpec | None
    ) -> tuple[SandboxBackend | None, list[SandboxBackend]]:
        """Resolve a call-scoped delete without an admission, including host denials.

        Without a spec, PER_SPEC selection reaches only backends declaring CALL scope:
        a conversation-scoped backend could otherwise delete a sandbox this call never owned.
        """
        if self._selection is not Selection.PER_SPEC:
            return self._backend, ([] if self._backend is None else [self._backend])
        if spec is None:
            return None, [
                backend
                for backend in self._backends
                if IsolationScope.CALL
                in _declared_isolation_scopes(backend, _declarations(backend))
            ]
        served = self.backend_for(spec)
        return served, ([] if served is None else [served])

    async def dispose_unclean(
        self,
        key: SandboxKey,
        *,
        kind: str | None = None,
        instance_id: str | None = None,
        timeout: float,
    ) -> bool:
        """Retry recorded backend/kind/instance targets, optionally narrowed by the selectors.

        The key stays refused until every pending target lands. KEEP suppresses refusal;
        timeout bounds the lock wait and all deletes together. Returns False on failure or
        timeout; cancellation propagates. Raises ValueError for a call-scoped key or a timeout
        that is not finite and positive.
        """
        if not self._may_be_refused(key):
            raise ValueError(
                f"dispose_unclean was given a key naming a call ({key.call_id}). Refusing it "
                "afterwards protects nothing — that key has no next acquire — so this method has "
                "nothing to offer over dispose_call(key, timeout=...), which deletes the "
                "call's sandbox and reports whether it landed."
            )
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError(f"timeout must be a finite positive number of seconds, not {timeout}")
        # The opt-down is from closing the key, not from the bound: this still runs after a
        # tool call's body. So the bound wraps both paths; only the ledger writes differ.
        refuse = self._reclaim.failed_reclaim_policy is not FailedReclaimPolicy.KEEP
        if refuse and (kind is not None or instance_id is not None):
            for backend in self._backends_for_instance(key, kind, instance_id):
                self.mark_unclean(key, backend=backend, kind=kind, instance_id=instance_id)
        elif refuse:
            with self._unclean_guard:
                if key not in self._unclean:
                    self._mark_unclean(key, None, backend=None, kind=None, instance_id=None)
        try:
            async with asyncio.timeout(timeout):
                # Inside the bound: waiting on another disposal for this key is still the
                # caller's time, and a bound covering only part of the wait is not the bound
                # this docstring promises.
                async with self._disposal_lock(key):
                    pending = self._pending_for(key, kind=kind, instance_id=instance_id)
                    if not pending:
                        return await self._dispose_each(
                            key,
                            refuse=refuse,
                            kind=kind,
                            instance_id=instance_id,
                            backends=self._backends_for_instance(key, kind, instance_id),
                        )
                    landed = True
                    for target in pending:
                        landed = (
                            await self._dispose_each(
                                key,
                                refuse=refuse,
                                backends=[target.backend],
                                kind=target.kind,
                                instance_id=target.instance_id,
                                pending=[target],
                            )
                            and landed
                        )
                    return landed and not self._unclean_state(key)[0]
        except TimeoutError:
            logger.warning(
                "sandbox router: disposing %s/%s/%s did not finish within %ss",
                key.scope,
                key.thread_id,
                key.agent_dir,
                timeout,
            )
            if not refuse:
                # Nothing to record: not closing the key is the whole of the opt-down.
                return False
            with self._unclean_guard:
                if key not in self._unclean:
                    return False
                expired = DisposalFailure(
                    "timeout", f"the disposal did not finish within {timeout}s"
                )
                recorded = self._unclean.get(key)
                self._unclean[key] = fold_disposal_failures(
                    [expired] if recorded is None else [recorded, expired]
                )
            return False

    def _may_be_refused(self, key: SandboxKey) -> bool:
        """Whether the ledger can protect ``key`` at all.

        It closes a key against its **next** acquire, and a call-scoped key has none — the entry
        would be read by nobody and cleared by nothing, so writing one is an unbounded map on a
        host that mints a key per call. What the entry would have refused is refused anyway: the
        conditions that mark a key — a sandbox this backend cannot reclaim, a delete that did not
        land — are re-read on the next acquire rather than remembered.
        """
        return not key.call_id

    def mark_unclean(
        self,
        key: SandboxKey,
        reason: DisposalFailure | None = None,
        *,
        backend: SandboxBackend | None = None,
        kind: str | None = None,
        instance_id: str | None = None,
    ) -> None:
        """Refuse a conversation key and retain its cleanup targets for a later retry.

        Omitting backend or kind requests the corresponding sweep. A new mark survives a
        disposal already in flight; call-scoped keys have no next acquire to refuse.
        """
        if not self._may_be_refused(key):
            return
        with self._unclean_guard:
            self._mark_unclean(key, reason, backend=backend, kind=kind, instance_id=instance_id)

    def _mark_unclean(
        self,
        key: SandboxKey,
        reason: DisposalFailure | None,
        *,
        backend: SandboxBackend | None,
        kind: str | None,
        instance_id: str | None,
    ) -> None:
        """Record targets and a reason while holding the ledger guard."""
        targets = self._pending_disposals.setdefault(key, {})
        for serving in self._backends if backend is None else [backend]:
            targets[(id(serving), kind, instance_id)] = _PendingDisposal(serving, kind, instance_id)
        if self._unclean.get(key) is None:
            self._unclean[key] = None if reason is None else fold_disposal_failures([reason])

    def _backends_for_instance(
        self, key: SandboxKey, kind: str | None, instance_id: str | None
    ) -> list[SandboxBackend]:
        """Use every recorded serving backend, falling back to engine ownership checks."""
        if instance_id is None:
            return self._backends
        with self._seen_guard:
            known = {
                provider: backend
                for (held, workload, provider), (backend, instances) in self._served.items()
                if held == key and (kind is None or workload == kind) and instance_id in instances
            }
        for target in self._pending_for(key, kind=kind, instance_id=instance_id):
            known[id(target.backend)] = target.backend
        return list(known.values()) or self._backends

    def _unclean_state(self, key: SandboxKey) -> tuple[bool, DisposalFailure | None]:
        with self._unclean_guard:
            return key in self._unclean, self._unclean.get(key)

    def _pending_for(
        self,
        key: SandboxKey,
        backends: Sequence[SandboxBackend] | None = None,
        kind: str | None = None,
        instance_id: str | None = None,
    ) -> list[_PendingDisposal]:
        with self._unclean_guard:
            return [
                target
                for target in self._pending_disposals.get(key, {}).values()
                if (backends is None or any(target.backend is one for one in backends))
                and (kind is None or target.kind == kind)
                and (instance_id is None or target.instance_id == instance_id)
            ]

    def _forget_pending(self, key: SandboxKey, pending: Sequence[_PendingDisposal]) -> None:
        with self._unclean_guard:
            targets = self._pending_disposals.get(key, {})
            for target in pending:
                at = (id(target.backend), target.kind, target.instance_id)
                if targets.get(at) is target:
                    targets.pop(at)
            if not targets:
                self._pending_disposals.pop(key, None)
                self._unclean.pop(key, None)

    @asynccontextmanager
    async def scope(self, scope: str, thread_id: str) -> AsyncGenerator[ScopeDisposal, None]:
        """Serve one conversation, and reclaim its sandboxes when the block ends.

        :meth:`dispose_scope` is the call every host has to remember and one will not — its own
        reason says why that matters: a sandbox nobody reclaims is a sandbox somebody pays for.
        Here it runs however the block ends.

        It cannot mask an application error.  :meth:`dispose_scope` already swallows each
        backend's failure and logs it, so nothing raised on the way out replaces the exception
        on its way past — which is the property that makes putting it in a ``finally`` safe.

        The yielded object carries the count *after* the block, because a host that reports
        what it reclaimed is the one that notices the day the number is zero — and, beside it,
        the reason a sandbox is still there, because a host that deleted a conversation and did
        not is owed more than a number that happens to be lower than usual.
        """
        disposal = ScopeDisposal()
        try:
            yield disposal
        finally:
            purge = await self.dispose_scope(scope, thread_id)
            disposal.disposed = purge.disposed
            disposal.undisposed = purge.undisposed

    async def dispose_scope(self, scope: str, thread_id: str) -> ScopePurge:
        """Purge this conversation on every backend, reporting counts and failures.

        Successful backends retire their pending targets; failed or newer targets keep keys
        refused. Each backend's answer reaches the observer as a ScopeDisposed event.
        """
        total = 0
        with self._unclean_guard:
            pending = {
                key: list(targets.values())
                for key, targets in self._pending_disposals.items()
                if (key.scope, key.thread_id) == (scope, thread_id)
            }
        undisposed: list[DisposalFailure] = []
        for backend in self._backends:
            started = time.monotonic()
            # This backend's own answer and its own count, kept apart from the sweep's.
            answered: DisposalFailure | None = None
            disposed = 0
            try:
                purged = await backend.dispose_scope(scope, thread_id)
            except Exception as exc:  # noqa: BLE001 - purge must never fail
                answered = DisposalFailure("unknown", f"{backend.name} raised: {exc}")
                undisposed.append(answered)
                logger.warning(
                    "sandbox router: backend %s failed to purge thread %s: %s",
                    backend.name,
                    thread_id,
                    exc,
                )
            except BaseException as interrupted:
                # The bound expiring mid-sweep, as in `_dispose_each`: nothing below runs, so
                # this is the interrupted backend's only record.
                self._record_an_interrupted_purge(scope, thread_id, backend, started, interrupted)
                raise
            else:
                disposed = purged.disposed
                total += disposed
                if purged.undisposed is not None:
                    answered = DisposalFailure(
                        purged.undisposed.code, f"{backend.name}: {purged.undisposed.detail}"
                    )
                    undisposed.append(answered)
                    logger.warning(
                        "sandbox router: backend %s did not purge thread %s: %s",
                        backend.name,
                        thread_id,
                        purged.undisposed,
                    )
            if answered is None:
                self._forget_instances(backend, scope=scope, thread_id=thread_id)
                for key, targets in pending.items():
                    self._forget_pending(
                        key, [target for target in targets if target.backend is backend]
                    )
            self._record_purge(scope, thread_id, backend, disposed, answered, started)
        return ScopePurge(total, fold_disposal_failures(undisposed))
