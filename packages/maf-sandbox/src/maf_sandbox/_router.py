"""Backend selection: the layer between a host application and any sandbox provider.

``app -> SandboxRouter -> backend -> the sandbox itself``.  The router owns what no
individual backend can own: **which** backend serves a request, and the rules that decide
whether it may — a minimum-isolation floor, a capability match, the guest's shape, the
transfer ceilings, the egress rule, the scope one sandbox may serve, and the host's outright
denials (capabilities and identities this posture refuses whatever the backend could do).
"""

from __future__ import annotations

import asyncio
import dataclasses
import inspect
import logging
import math
import weakref
from collections.abc import AsyncGenerator, Iterable, Sequence
from contextlib import asynccontextmanager
from enum import StrEnum
from typing import cast

from ._host_tools_over_exec import fold_host_tool_call_transfer_limits
from ._protocol import (
    DEFAULT_BACKEND_DECLARATIONS,
    ISOLATION_RANK,
    ISOLATION_SCOPE_RANK,
    BackendDeclarations,
    Capability,
    DisposalCode,
    DisposalFailure,
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

    The **declaration** one covers a backend this package cannot read, and it is raised at two
    times. At construction, and again per spec: a backend still carrying one of the attributes
    :class:`~maf_sandbox.BackendDeclarations` replaced, or a ``declarations`` that is not one —
    both are properties of the backend alone, so the earliest moment is construction. Per spec
    only: a ``capabilities`` or ``egress_modes`` that is not a set, which is read where the
    match consumes it.
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


def _refuse_a_sandbox_that_cannot_be_reclaimed(sandbox: Sandbox) -> None:
    """Refuse a sandbox missing :meth:`Sandbox.reclaim`, naming the member rather than leaking.

    No capability gates ``reclaim``, so no other check notices a backend without it — every
    call would leak its directory instead. A :class:`TypeError` because an absent protocol
    member is exactly that: read by a person and fixed in code, never caught to recover.
    """
    if not callable(getattr(sandbox, "reclaim", None)):
        raise TypeError(
            f"{type(sandbox).__name__} does not implement `Sandbox.reclaim`, which every backend "
            "serves and no capability gates. Add it — a directory this stack created under the "
            "working directory, removed recursively, where a missing directory is success — and "
            "`maf_sandbox.conformance.assert_reclaim_conformance` proves the implementation."
        )


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


def _declarations(backend: SandboxBackend) -> BackendDeclarations:
    """The one object every optional declaration is read from: one ``getattr``, five fields.

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

    :data:`FIXED` is the default and is what this package has always done.  Routing stays
    opt-in for a billing reason rather than a stylistic one, and the reason is narrower than
    it first looks: routing can only ever *serve* a spec that is refused today.  A router with
    no ``selected`` pin routes to the first registered backend exactly as it resolves to it,
    so nothing that runs today moves anywhere — what changes is that a refusal becomes a
    running sandbox, and on a remote backend a running sandbox has a price.  A host takes that
    trade deliberately.
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
    """

    def __init__(
        self,
        backends: Sequence[SandboxBackend],
        *,
        min_isolation: Isolation = Isolation.MICROVM,
        min_isolation_scope: IsolationScope = IsolationScope.CONVERSATION,
        selected: str | None = None,
        selection: Selection = Selection.FIXED,
        denied_capabilities: Iterable[Capability] = (),
        denied_identities: Iterable[Identity] = (),
        reclaim: ReclaimConfig = DEFAULT_RECLAIM_CONFIG,
    ) -> None:
        self._backends = list(backends)
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

        Every one of them, deliberately, rather than the subset above the floor. Two reasons,
        and the second is the load-bearing one. The floor is the *first* check
        :meth:`_refuse_unless_this_backend_can_serve` runs, so a below-floor backend refuses per
        spec with its rung and the floor named — which the host reads **only when no later
        candidate serves**, since a successful route discards the refusals it passed over; the
        warning below is what names it on every other route. And a backend this list dropped
        would still be in ``self._backends``, so the filtering would buy nothing: disposal
        sweeps that, not this.

        What is checked here is what cannot wait, and routing is what makes it urgent.

        Declarations are read for **all** of them — the object *and* every field's shape — so a
        half-migrated or mis-shaped backend fails at startup rather than the first time a spec
        happens to route as far as it. Under :data:`Selection.FIXED` only the selected backend
        is ever read, and that asymmetry is the point rather than an oversight: there, a
        mis-shaped field surfaces at the first check because there is nowhere to route past it
        to. Here there is. :meth:`_refusal_serving` catches :data:`ATTACH_REFUSALS`, and
        ``_declared_set`` and ``_declared_limits`` raise members of it for a field this package
        cannot read — so without this an unreadable declaration on the first candidate would be
        indistinguishable from an honest refusal, and the *second* backend would quietly serve.
        A declaration nobody can read is refused rather than routed past.

        And the floor is judged across the whole registration: a deployment where nothing
        clears it can serve no workload at all, which is the misconfiguration ``__init__``
        exists to catch.
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
            _declared_isolation_scopes(backend, declared)
            _declared_limits(backend, declared)
        below = [(backend, rung) for backend, rung in rungs if not meets_floor(rung, floor)]
        if below and len(below) != len(rungs):
            # Warned rather than raised, because this arrangement is the one PER_SPEC exists to
            # serve and refusing it would take the feature away. Warned rather than left silent,
            # because the alternative is the thing the floor's own refusal is written against: a
            # host registers a backend below its floor, routing quietly passes over it in favour
            # of one that clears it, and nothing ever says so. The per-spec refusal names it only
            # when *nothing* can serve, which is exactly the case this one is not.
            #
            # It does not advise unregistering, and must not: `dispose` and `dispose_scope` ask
            # every registered backend precisely so a host that changed which one serves does
            # not strand what the previous one still holds. Below the floor is a statement about
            # what may be *served*, never about what must be reclaimed.
            #
            # It promises nothing about what *will* serve either. No spec exists yet, so an
            # above-floor backend may still refuse this host's every workload on capabilities,
            # egress, limits or scope — "considered" is the whole of what clearing the floor
            # earns, and a warning claiming an outcome would be one more thing to disbelieve.
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

        A pure function of the spec, the registered backends and their declarations, and of
        nothing else — no load, health, latency or cost is consulted. Callers may rely on that:
        asking twice **with the same spec** cannot name two backends. It says nothing about two
        different specs, which may route apart under one key and are meant to.
        :class:`Selection` carries why.
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
        denied_capabilities = spec.requires & self._denied_capabilities
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
        missing = spec.requires - capabilities
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
        if spec.requires & self._denied_capabilities:
            return None
        if spec.identities & self._denied_identities:
            return None
        return self._route(spec)[0]

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
        self, key: SandboxKey, backend: SandboxBackend
    ) -> None:
        """Dispose what this acquire just created, then raise the refusal it walked into.

        Under the key's disposal lock, so it cannot overlap the disposal whose mark sent it
        here: two deletes for one key at once are what that lock exists to prevent, and this
        one would otherwise be the exception.  On the backend that just served the create,
        directly, rather than through :meth:`dispose` — which would take the same lock again,
        clear the ledger entry this refusal quotes, and under
        :data:`Selection.PER_SPEC` sweep backends that never saw this key.
        """
        async with self._disposal_lock(key):
            try:
                undisposed = await backend.dispose(key)
            except Exception as failed:  # noqa: BLE001 — the refusal must reach the caller
                undisposed = str(failed)
        reported = self._unclean.get(key)
        if undisposed is None:
            outcome = "The sandbox just created has been disposed"
        else:
            logger.warning(
                "sandbox router: backend %s failed to dispose a sandbox refused mid-create: %s",
                backend.name,
                undisposed,
            )
            # Said out loud, not only logged: an operator who reads "disposed" stops looking,
            # and what is still running is billable.
            outcome = "The sandbox just created could not be disposed either"
        raise SandboxUnclean(
            f"the sandbox for {key.scope}/{key.thread_id}/{key.agent_dir} was refused while "
            f"this acquire was creating it — a disposal for the key ran and did not land. "
            f"{outcome}; the key stays refused until a disposal lands.",
            code=reported.code if reported is not None else None,
        )

    async def acquire(self, key: SandboxKey, spec: SandboxSpec) -> Sandbox:
        """Return a running sandbox for ``key``, creating one if needed.

        Runs the same floor, capability, limit and egress checks as :meth:`ensure_can_serve`
        before ever reaching the backend, so a caller that skips :meth:`ensure_can_serve` is
        still refused rather than served behind a boundary or capability set the spec did not
        agree to.  Under :data:`Selection.PER_SPEC` those checks are also what *chooses* the
        backend, and everything after the create — the reclaim refusal's disposal, the
        mid-create disposal — is aimed at the one that served rather than at all of them.

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
            TypeError: when the backend hands back a sandbox without :meth:`Sandbox.reclaim`.
                That sandbox is disposed (this backend, best effort) before the refusal
                reaches the caller: a backend that cannot reclaim can never clean it, and a
                refused acquire must not leave a billable sandbox running.
        """
        if not self._candidates:
            raise NoSandboxBackend("no sandbox backend is configured")
        if key in self._unclean:
            # The code only: a detail can carry an endpoint or a raw response body, and this
            # message reaches hosts that do not sanitize. The detail is in the log beside it.
            reported = self._unclean[key]
            because = f" ({reported.code})" if reported is not None else ""
            raise SandboxUnclean(
                f"the sandbox for {key.scope}/{key.thread_id}/{key.agent_dir} was left unclean — "
                "a tool call's data could not be removed, or a program it started may still be "
                f"running — and disposing it did not land{because}. It is refused until a "
                "disposal lands — dispose(key) or dispose_scope(scope, thread_id) — rather than "
                "served unclean.",
                code=reported.code if reported is not None else None,
            )
        served = self._refuse_unless_backend_can_serve(spec)
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
        sandbox = await served.acquire(key, spec)
        if key in self._unclean:
            # Read again after the create: the check above is only as fresh as the moment
            # before the await, and a disposal that begins during it closes the key without
            # this call ever seeing the mark. One that began earlier is caught above, since a
            # disposal marks the key before its own first await.
            await self._refuse_a_key_closed_during_the_create(key, served)
        try:
            _refuse_a_sandbox_that_cannot_be_reclaimed(sandbox)
        except TypeError:
            # The sandbox already exists, and this backend can never clean it — the rule in
            # `docs/sandbox/tool-call.md` § Cleanup. Disposed on this backend alone: its other
            # sandboxes for the key are equally unreclaimable, and no other backend's are
            # touched. Its own failure is logged, never allowed to replace the refusal.
            try:
                reported = await served.dispose(key)
            except Exception as undisposed:  # noqa: BLE001 — the refusal must reach the caller
                reported = str(undisposed)
            if reported is not None:
                logger.warning(
                    "sandbox router: backend %s failed to dispose after a reclaim refusal: %s",
                    served.name,
                    reported,
                )
                # A refused acquire owes nothing billable left running. This one does, so
                # the key is closed rather than served over a sandbox nothing can reclaim.
                self.mark_unclean(key, _coded(served.name, reported))
            raise
        return sandbox

    async def dispose(self, key: SandboxKey) -> None:
        """Delete every kind's sandbox for ``key``. Best-effort across every registered backend."""
        async with self._disposal_lock(key):
            await self._dispose_each(key)

    async def _dispose_each(
        self,
        key: SandboxKey,
        *,
        refuse: bool = False,
        backends: Sequence[SandboxBackend] | None = None,
    ) -> bool:
        """Ask every backend to dispose ``key``; ``True`` when none refused.

        ``backends`` narrows the sweep, and only :meth:`dispose_call` passes it: every other
        caller asks all of them, because a key may have been served by a backend this router no
        longer selects.

        A landed disposal clears the key from the unclean set: whatever was in that sandbox
        went with it.  ``refuse`` closes the key when one does *not* land, and only
        :meth:`dispose_unclean` passes it: :meth:`dispose` is best-effort, so a transient
        failure there must not leave a clean key unservable.  Under ``refuse`` each reason
        reaches the ledger as its backend answers, because the bound can expire mid-loop.

        A backend refuses by *returning* a reason as much as by raising: ``dispose`` never
        raises, so silence is the only thing that may be read as success.
        """
        reasons: list[DisposalFailure] = []
        for backend in self._backends if backends is None else backends:
            try:
                undisposed = await backend.dispose(key)
            except Exception as exc:  # noqa: BLE001 - disposal must not fail a caller
                # Nothing a backend says while breaking never-raises can be classified.
                reasons.append(DisposalFailure("unknown", f"{backend.name} raised: {exc}"))
                logger.warning(
                    "sandbox router: backend %s failed to dispose: %s", backend.name, exc
                )
            else:
                if undisposed is not None:
                    reasons.append(_coded(backend.name, undisposed))
                    logger.warning(
                        "sandbox router: backend %s did not dispose %s/%s/%s: %s",
                        backend.name,
                        key.scope,
                        key.thread_id,
                        key.agent_dir,
                        undisposed,
                    )
            if refuse and reasons:
                # As each backend answers, not after the last: the bound can expire mid-loop
                # and a reason still in this list would die with the cancelled coroutine,
                # leaving the handler to record `timeout` over a code that outranks it.
                # Recorded over whatever marked the key. No await between the fold and write.
                self._unclean[key] = fold_disposal_failures(reasons)
        if reasons:
            return False
        self._unclean.pop(key, None)
        return True

    async def dispose_call(
        self, key: SandboxKey, *, timeout: float, spec: SandboxSpec | None = None
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

        ``spec`` is what names the backend to ask under :data:`Selection.PER_SPEC`, by routing
        it again rather than by remembering where the sandbox went.  A ``key -> backend`` map
        would be the shape :meth:`_may_be_refused` already refuses for the unclean ledger — an
        unbounded map on a host that mints a key per call — and it would answer nothing on a
        replica that did not create the sandbox.  Omitting it there leaves nothing to route on,
        so every backend **declaring** :data:`~maf_sandbox.IsolationScope.CALL` is asked and no
        others: slower than routing, never wrong, and the shipped caller has the spec and
        passes it.  The exclusion is not an optimisation — a conversation-scoped backend's
        ``dispose`` sweeps by scope, thread and agent, so asking one would delete a sandbox
        this call never owned.

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
        serving, sweep = self._serving_for_call(spec)
        if serving is not None:
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
        """Which backend served a call's sandbox, and which backends to ask for the delete.

        Routed through :meth:`backend_for` rather than :meth:`_route`, because the host's own
        denials are part of the question. ``_route`` does not consult them — they are raised
        once, ahead of it — so a spec this host denies would still pick a backend here, and
        that backend's ``dispose`` takes **every kind** under the call key. A denied spec never
        created anything, so the honest answer is nobody.

        The two returns differ only where there is no backend to name: none to ask is a landed
        delete, and a per-spec router called without a spec has nothing to route on and asks
        each backend that could be holding a call's sandbox at all.

        **Only those**, and the filter is the same rule the scope guard above enforces for a
        named backend. A backend serving one sandbox per conversation has none of this call's
        to delete, and its ``dispose`` sweeps by scope, thread and agent — so asking it would
        delete the conversation's sandbox out from under every later call.
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

    async def dispose_unclean(self, key: SandboxKey, *, timeout: float) -> bool:
        """Dispose a sandbox the framework could not clean, and refuse the key until one lands.

        What ``sandboxed_tool`` calls from its ``finally`` over a removal that failed or a stop
        that did not reach everything. Bounded by ``timeout`` because it runs after the body
        has returned and adds to the call's latency. ``False`` when any backend refused or the
        bound passed — and from then on :meth:`acquire` raises :class:`SandboxUnclean` for the
        key until a disposal lands.

        The key is refused **before** the first disposal await, not after it lands: calls
        sharing a key are not serialized, so a disposal that hangs must already have the key
        refused — otherwise a concurrent :meth:`acquire` passes its ledger check and is handed
        the dirty sandbox. :meth:`_dispose_each` discards the key on a landed disposal, so a
        success clears it while a failure, the bound passing, or a cancellation leaves it
        refused.  ``FailedReclaimPolicy.KEEP`` suppresses the ledger writes, not the bound.

        Raises:
            ValueError: when ``key`` names a call, which :meth:`dispose_call` serves and this
                cannot protect; or when ``timeout`` is not a finite positive number of seconds.
                ``math.inf``
                would leave ``asyncio.timeout`` unable to expire, so the documented bound would
                not hold and a hanging backend would hang the caller. Checked before the key is
                marked, so a rejected call has no lingering effect on the ledger.
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
        if refuse:
            self._unclean.setdefault(key, None)
        try:
            async with asyncio.timeout(timeout):
                # Inside the bound: waiting on another disposal for this key is still the
                # caller's time, and a bound covering only part of the wait is not the bound
                # this docstring promises.
                async with self._disposal_lock(key):
                    return await self._dispose_each(key, refuse=refuse)
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
            if key not in self._unclean:
                # The bound can now expire waiting for another disposal's lock, and that
                # disposal may have landed and taken the key with it. Absent is not the same as
                # marked-with-no-reason, which is what `get` would flatten it to: recording a
                # timeout over it refuses a key whose sandbox is gone.
                return False
            expired = DisposalFailure("timeout", f"the disposal did not finish within {timeout}s")
            recorded = self._unclean.get(key)
            # Folded, not assigned: an earlier attempt may have recorded something better.
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

    def mark_unclean(self, key: SandboxKey, reason: DisposalFailure | None = None) -> None:
        """Refuse ``key`` without disposing — for a cleanup cancelled before it could dispose.

        Synchronous, because it is called while a :class:`~asyncio.CancelledError` is propagating
        out of a tool call's cleanup, where awaiting a disposal is not reliable.  The sandbox is
        left refused (:meth:`acquire` raises :class:`SandboxUnclean`) until a later disposal — a
        subsequent :meth:`dispose_unclean`, or :meth:`dispose_scope` — lands.

        The refusal carries ``reason``'s *code* only; the detail stays in the log.  A reason
        does not overwrite one a disposal already recorded: what a backend said about the
        sandbox says more than that a cleanup was cut short.

        A key naming a call is not written at all — :meth:`_may_be_refused` carries why.
        """
        if not self._may_be_refused(key):
            return
        if self._unclean.get(key) is None:
            # Folded, not stored as given: one place decides what a legal code is.
            self._unclean[key] = None if reason is None else fold_disposal_failures([reason])

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
        """Delete every sandbox for ``(scope, thread_id)``, returning how many, and what stayed.

        Every registered backend is asked, not only the selected one: a conversation may have
        been served while a different backend was configured, and a sandbox nobody reclaims
        is a sandbox somebody pays for.

        A backend refuses by returning a reason as much as by raising, the same reading
        :meth:`_dispose_each` takes and for the same reason. Only a purge that landed reopens
        the conversation's refused keys: the one that did not is precisely the one whose
        sandboxes still hold the data those keys were refused over.
        """
        total = 0
        undisposed: list[DisposalFailure] = []
        for backend in self._backends:
            try:
                purged = await backend.dispose_scope(scope, thread_id)
            except Exception as exc:  # noqa: BLE001 - purge must never fail
                undisposed.append(DisposalFailure("unknown", f"{backend.name} raised: {exc}"))
                logger.warning(
                    "sandbox router: backend %s failed to purge thread %s: %s",
                    backend.name,
                    thread_id,
                    exc,
                )
            else:
                total += purged.disposed
                if purged.undisposed is not None:
                    undisposed.append(
                        DisposalFailure(
                            purged.undisposed.code, f"{backend.name}: {purged.undisposed.detail}"
                        )
                    )
                    logger.warning(
                        "sandbox router: backend %s did not purge thread %s: %s",
                        backend.name,
                        thread_id,
                        purged.undisposed,
                    )
        if not undisposed:
            # The conversation's sandboxes are gone, so nothing under it holds data any more.
            self._unclean = {
                key: reason
                for key, reason in self._unclean.items()
                if (key.scope, key.thread_id) != (scope, thread_id)
            }
        return ScopePurge(total, fold_disposal_failures(undisposed))
