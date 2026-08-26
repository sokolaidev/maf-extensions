"""Backend selection: the layer between a host application and any sandbox provider.

``app -> SandboxRouter -> backend -> the sandbox itself``.  The router owns what no
individual backend can own: **which** backend serves a request, and the five rules that
decide whether it may — a minimum-isolation floor, a capability match, the transfer ceilings,
the egress rule, and the host's outright denials (capabilities and identities this posture
refuses whatever the backend could do).
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import math
from collections.abc import AsyncGenerator, Iterable, Sequence
from contextlib import asynccontextmanager
from typing import cast

from ._host_tools_over_exec import fold_host_tool_call_transfer_limits
from ._protocol import (
    DEFAULT_CAPABILITIES,
    DEFAULT_SANDBOX_LIMITS,
    ISOLATION_RANK,
    Capability,
    DisposalCode,
    DisposalFailure,
    Egress,
    Identity,
    Isolation,
    OsFamily,
    Sandbox,
    SandboxBackend,
    SandboxKey,
    SandboxLimits,
    SandboxSpec,
    TransferLimits,
    fold_disposal_failures,
    meets_floor,
)

logger = logging.getLogger(__name__)

__all__ = [
    "NoSandboxBackend",
    "SandboxBackendNotPermitted",
    "SandboxCapabilityDenied",
    "SandboxCapabilityNotSupported",
    "SandboxEgressNotEnforced",
    "SandboxIdentityDenied",
    "SandboxOsFamilyNotSupported",
    "SandboxRouter",
    "SandboxTransferLimitsNotPermitted",
    "SandboxUnclean",
    "ScopeDisposal",
]


@dataclasses.dataclass
class ScopeDisposal:
    """What :meth:`SandboxRouter.scope` reclaimed, filled in once its block has ended.

    Mutable and read afterwards rather than returned, because a context manager's value is
    bound before the work it wraps has happened.  Inside the block it reads zero and means
    nothing.
    """

    disposed: int = 0


#: The rungs, weakest first, rendered once for the refusal messages.
_LADDER = ", ".join(map(str, ISOLATION_RANK))

#: The directions a `SandboxLimits` carries, read off the dataclass so a message naming them
#: cannot drift from the type a backend is being asked for.
_DIRECTION_FIELDS = tuple(field.name for field in dataclasses.fields(SandboxLimits))


class NoSandboxBackend(LookupError):
    """No backend is configured, or the named one is not registered."""


class SandboxBackendNotPermitted(PermissionError):
    """The selected backend's boundary is below the floor the host — or a spec — requires.

    Raised rather than degraded on purpose.  Silently falling back to a stronger backend
    would hide a misconfiguration, and silently proceeding with the weaker one would break
    the boundary every claim about the execution surface rests on.
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

    :attr:`code` is the :data:`DisposalCode` the last disposal reported, or ``None`` when the
    key was closed without one.  **Branch on it, not on the message.**  The sentence is prose
    and may be reworded; putting the code only inside it would hand every host a regex and
    give back the problem a closed set of codes exists to remove.  The backend's own detail is
    not here at all — it can carry an endpoint or a raw response body, and it stays in the log.
    """

    def __init__(self, *args: object, code: DisposalCode | None = None) -> None:
        # `*args`, not a required message: this is an `OSError` subclass, and narrowing the
        # inherited constructor would break `SandboxUnclean()` and the errno forms for anyone
        # who builds one — in a test double, say. The code is keyword-only and additive.
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


class SandboxTransferLimitsNotPermitted(PermissionError):
    """The workload's spec asks to move more data than the selected backend allows.

    A safety claim rather than a functionality one, which is why an undeclared ``limits`` is
    read as :data:`~maf_sandbox.DEFAULT_SANDBOX_LIMITS` and a bigger ask refused, where an
    undeclared ``capabilities`` is read charitably.  Also raised for a ``limits`` this package
    cannot read at all — a declaration nobody can compare against is refused, not guessed at.
    """


def _coded(backend_name: str, reported: DisposalFailure | str) -> DisposalFailure:
    """One backend's answer as a :class:`~maf_sandbox.DisposalFailure`, named by the backend.

    A bare ``str`` is a backend that has not moved to the class yet, and it is read as
    ``"unknown"`` — the honest code for a sentence nothing can classify, and the one that
    keeps such a backend inside the vocabulary rather than outside it.
    """
    if isinstance(reported, DisposalFailure):
        return DisposalFailure(reported.code, f"{backend_name}: {reported.detail}")
    # Anything else — a bool, an exception object, a backend built against a newer protocol —
    # is still an answer, and reading `.code` off it would raise out of a caller that never
    # raises. The protocol widened this return type only this release, which is exactly when a
    # backend is most likely to answer with the wrong shape.
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


def _declared_os_families(backend: SandboxBackend) -> frozenset[OsFamily]:
    """The guest shapes ``backend`` claims it hands out, empty when it claims none.

    Silence is neither a functionality default nor a safety one, which is why this reads
    nothing like :func:`_declared_limits`. It is *absence of an answer*: a backend serving a
    language runtime has no operating system to name, and a backend written before this axis
    existed never considered the question. Both are read as ``frozenset()``, which refuses a
    spec that asks for a family and leaves every spec that does not exactly as it was.

    A declaration that is not a set of :class:`~maf_sandbox.OsFamily` is read as empty rather
    than refused, deliberately, and this is the one place that choice is made: unlike a
    mis-shaped ``limits``, a mis-shaped value here cannot widen anything — the worst it does is
    refuse a workload that would have been served, loudly, with the declaration named.
    """
    declared: object = getattr(backend, "os_families", None)
    if not isinstance(declared, frozenset | set):
        return frozenset()
    # `object` throughout rather than a set type: this is an undeclared attribute off an
    # arbitrary backend, so every element is checked and nothing is assumed about the shape.
    members = cast("Iterable[object]", declared)
    return frozenset(family for family in members if isinstance(family, OsFamily))


def _declared_limits(backend: SandboxBackend) -> SandboxLimits:
    """The ceilings ``backend`` claims, refusing a declaration that is not the right shape.

    Same policy as :func:`_declared_isolation`, for the same reason: a declaration this package
    cannot read is refused rather than guessed at.  The mistake worth naming is the adjacent
    one — :class:`~maf_sandbox.TransferLimits` is a cap for **one** direction and
    :class:`~maf_sandbox.SandboxLimits` is the pair, both exported from one module, and the
    wrong one here used to surface as a bare ``AttributeError`` out of a host's agent factory.
    """
    declared = getattr(backend, "limits", DEFAULT_SANDBOX_LIMITS)
    if isinstance(declared, SandboxLimits):
        return declared
    raise SandboxTransferLimitsNotPermitted(
        f"sandbox backend {backend.name!r} declares limits as {type(declared).__name__}, and "
        f"only {SandboxLimits.__name__} can be read as one — it carries a "
        f"{TransferLimits.__name__} per direction ({', '.join(_DIRECTION_FIELDS)}), where a "
        f"bare {TransferLimits.__name__} is one direction's caps and says nothing about the "
        "other. Declare nothing at all to accept the default ceilings."
    )


class SandboxRouter:
    """Routes a sandbox request to a backend.

    Args:
        backends: The registered backends, in preference order.
        min_isolation: The weakest boundary this host accepts. Defaults to
            :data:`Isolation.MICROVM`.
        selected: Name of the backend to use. ``None`` picks the first registered one, which
            with a single backend is the whole selection story and stays correct when more
            arrive.
        denied_capabilities: Capabilities this host refuses outright, whatever a backend
            declares — a spec *requiring* one is refused at attach. The hard stop for a
            posture: ``denied_capabilities={Capability.HOST_TOOLS}`` closes the
            middleware-bypass channel for every workload this router serves.
        denied_identities: Identities this host refuses host tools to exercise — a
            spec whose ``identities`` carries one is refused at attach.
            ``denied_identities={Identity.USER}`` is how a host forbids model-orchestrated
            user authority in one statement instead of auditing each registration.
        keep_unclean: Opt down from the framework disposing a sandbox it could not clean.
            ``False`` by default, the way ``min_isolation`` defaults to the production
            posture: when a tool call's directory could not be removed, or a program it
            stopped may have left something running, ``sandboxed_tool`` disposes that
            sandbox before the next call can reuse it. ``True`` keeps it warm with the data
            in it, and the host's ``on_reclaim_failure`` is told so. A kind cannot set this:
            it is the host's call to loosen, never a workload's.

    Raises:
        SandboxBackendNotPermitted: at construction, when the selected backend declares a
            rung below ``min_isolation`` or one this package does not recognise. Failing
            here rather than at first use means a misconfigured deployment cannot start with
            the feature apparently enabled and quietly unsafe.
        ValueError: at construction, when ``min_isolation`` is not a rung this package
            recognises — raised by :class:`Isolation` itself rather than surfacing later as a
            bare ``KeyError`` out of a rank comparison, which would only happen once a backend
            was registered and a floor was actually compared against — or when a denied
            capability or identity is not a member this package recognises: a deny list that
            silently never matches would read as protection and provide none.
    """

    def __init__(
        self,
        backends: Sequence[SandboxBackend],
        *,
        min_isolation: Isolation = Isolation.MICROVM,
        selected: str | None = None,
        denied_capabilities: Iterable[Capability] = (),
        denied_identities: Iterable[Identity] = (),
        keep_unclean: bool = False,
    ) -> None:
        self._backends = list(backends)
        self._keep_unclean = bool(keep_unclean)
        # Keys whose sandbox holds data the framework could not remove and could not dispose
        # of. An entry leaves when a disposal lands; a key that keeps failing stays refused.
        # Keyed rather than a set so a refusal can say *why*: the reason is written when the
        # disposal reports one, and stays `None` for a key marked before anything was tried.
        self._unclean: dict[SandboxKey, DisposalFailure | None] = {}
        self._min_isolation = Isolation(str(min_isolation))
        self._selected_name = selected
        self._denied_capabilities = frozenset(
            Capability(str(capability)) for capability in denied_capabilities
        )
        self._denied_identities = frozenset(
            Identity(str(identity)) for identity in denied_identities
        )
        self._backend = self._resolve()

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

    @property
    def backend(self) -> SandboxBackend | None:
        """The selected backend, or ``None`` when none is configured."""
        return self._backend

    @property
    def enabled(self) -> bool:
        """Whether any backend is available. A host should attach no tools when ``False``."""
        return self._backend is not None

    @property
    def keep_unclean(self) -> bool:
        """Whether this host opted down from disposing a sandbox the framework could not clean."""
        return self._keep_unclean

    def _effective_floor(self, spec: SandboxSpec) -> Isolation:
        """The stricter of the host's floor and the spec's — a spec may raise, never lower."""
        if spec.min_isolation is None:
            return self._min_isolation
        return max(self._min_isolation, spec.min_isolation, key=ISOLATION_RANK.__getitem__)

    def _refuse_unless_backend_can_serve(self, spec: SandboxSpec) -> None:
        """Raise unless ``spec`` may be served: denials, floor, capabilities, limits, egress.

        The REFUSING half of the policy, shared by :meth:`ensure_can_serve` and
        :meth:`acquire`. With no backend configured this returns: nothing runs, so nothing
        reaches anything.
        """
        if self._backend is None:
            return

        # The denials first: they are statements about the spec against this host's posture,
        # not about what the backend could do, so no backend property softens them.
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

        floor = self._effective_floor(spec)
        declared = _declared_isolation(self._backend)
        if not meets_floor(declared, floor):
            raise SandboxBackendNotPermitted(
                f"the {spec.kind!r} workload requires at least {str(floor)!r} isolation, and "
                f"sandbox backend {self._backend.name!r} declares {str(declared)!r} "
                f"(ladder, weakest first: {_LADDER}). A spec may raise this host's floor and "
                "never lower it, so the workload is refused here rather than served behind a "
                "boundary it was written not to trust."
            )

        capabilities: frozenset[Capability] = getattr(
            self._backend, "capabilities", DEFAULT_CAPABILITIES
        )
        missing = spec.requires - capabilities
        if missing:
            raise SandboxCapabilityNotSupported(
                f"sandbox backend {self._backend.name!r} does not support "
                f"{', '.join(sorted(missing))}, which the {spec.kind!r} workload requires "
                f"(it declares {', '.join(sorted(capabilities)) or 'nothing'}). Refused "
                "rather than attempted: a workload that reaches for a capability the backend "
                "never implemented fails inside the sandbox, where the reason is hardest to "
                "see."
            )

        # After the capability match and before the ceilings, because it is the same kind of
        # question the capability match asks — can this backend serve this workload at all —
        # and a workload refused for the wrong guest shape was never going to reach a transfer.
        if spec.requires_os_family is not None:
            families = _declared_os_families(self._backend)
            if spec.requires_os_family not in families:
                served = ", ".join(sorted(str(family) for family in families))
                raise SandboxOsFamilyNotSupported(
                    f"sandbox backend {self._backend.name!r} hands out "
                    f"{served or 'no guest whose shape it states'}, and the {spec.kind!r} "
                    f"workload is written for a {str(spec.requires_os_family)!r} guest. Its "
                    "commands, its scripts and the paths it composes assume that shape, so "
                    "running it here would fail inside the sandbox at the first command "
                    "rather than here. Register a backend serving that family, or attach a "
                    "workload written for the one this backend has."
                )

        # Silence is a safety claim here, not a functionality one: an undeclared ceiling is
        # the default ceiling, and a spec asking above it is refused rather than believed.
        limits = _declared_limits(self._backend)
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
                    f"sandbox backend {self._backend.name!r} allows: it asks for {asked}"
                    f"{folded_note} and the backend permits {ceiling}. Refused rather than "
                    "clamped: a workload served a smaller cap than it declared fails part-way "
                    "through a collection, and a partial artifact set is worse than none because "
                    "the model cannot tell what it did not get."
                )

        # Egress is resolved, not matched: the workload runs in exactly one mode, and the
        # backend must be able to enforce it. Refuse, never degrade — no more-open substitute
        # (a silent widening) and no more-isolated one (a quietly different posture). Silence is
        # the empty set: a backend that declares nothing enforces nothing. See
        # docs/sandbox/research/egress-resolution.md.
        modes: frozenset[Egress] = getattr(self._backend, "egress_modes", frozenset())
        if spec.egress not in modes:
            enforced = ", ".join(sorted(modes)) or "nothing"
            raise SandboxEgressNotEnforced(
                f"sandbox backend {self._backend.name!r} cannot enforce the {str(spec.egress)!r} "
                f"egress the {spec.kind!r} workload runs in (it enforces {enforced}). A workload "
                "is served in exactly the mode it declares or refused — never a different one, "
                "because a more open mode silently widens what it reaches and a more isolated "
                "one changes the posture it was built for."
            )

    def ensure_can_serve(self, spec: SandboxSpec) -> None:
        """Raise unless ``spec`` may be served: denials, floor, capabilities, limits, egress.

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
            SandboxBackendNotPermitted: when the spec raises the floor above what the backend
                declares.
            SandboxCapabilityNotSupported: when the backend cannot do what the spec requires.
            SandboxOsFamilyNotSupported: when the spec asks for a guest shape the backend
                does not hand out.
            SandboxTransferLimitsNotPermitted: when the spec's caps exceed the backend's,
                or when the backend declares its ceilings as something other than a
                ``SandboxLimits``.
            SandboxEgressNotEnforced: when the backend cannot enforce the spec's egress mode.
        """
        self._refuse_unless_backend_can_serve(spec)

    async def acquire(self, key: SandboxKey, spec: SandboxSpec) -> Sandbox:
        """Return a running sandbox for ``key``, creating one if needed.

        Runs the same floor, capability, limit and egress checks as :meth:`ensure_can_serve`
        before ever reaching the backend, so a caller that skips :meth:`ensure_can_serve` is
        still refused rather than served behind a boundary or capability set the spec did not
        agree to.

        Raises:
            NoSandboxBackend: when no backend is configured. Callers that check
                :attr:`enabled` before attaching a tool never reach this.
            SandboxUnclean: when a previous call left this key's sandbox unclean and no disposal
                has since landed. An expected outcome for a direct consumer, not a backend
                failure: the refusal persists until :meth:`dispose_unclean` or
                :meth:`dispose_scope` succeeds for the key.
            SandboxCapabilityDenied: when the spec requires a capability this host denies.
            SandboxIdentityDenied: when the spec's ``identities`` carry one this host denies.
            SandboxBackendNotPermitted: when the spec raises the floor above what the backend
                declares.
            SandboxCapabilityNotSupported: when the backend cannot do what the spec requires.
            SandboxOsFamilyNotSupported: when the spec asks for a guest shape the backend
                does not hand out.
            SandboxTransferLimitsNotPermitted: when the spec's caps exceed the backend's,
                or when the backend declares its ceilings as something other than a
                ``SandboxLimits``.
            SandboxEgressNotEnforced: when the backend cannot confine egress to this spec.
            TypeError: when the backend hands back a sandbox without :meth:`Sandbox.reclaim`.
                That sandbox is disposed (this backend, best effort) before the refusal
                reaches the caller: a backend that cannot reclaim can never clean it, and a
                refused acquire must not leave a billable sandbox running.
        """
        if self._backend is None:
            raise NoSandboxBackend("no sandbox backend is configured")
        if key in self._unclean:
            # The *code* only. A backend's detail can carry an endpoint, a subscription id or
            # a raw response body — `error_detail`'s own docstring calls it log-only — and this
            # message reaches any host that calls `acquire` directly, not just `sandboxed_tool`,
            # which sanitizes. The detail is in the router's log beside the code.
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
        self._refuse_unless_backend_can_serve(spec)
        sandbox = await self._backend.acquire(key, spec)
        try:
            _refuse_a_sandbox_that_cannot_be_reclaimed(sandbox)
        except TypeError:
            # The sandbox already exists, and this backend can never clean it — the rule in
            # `docs/sandbox/tool-call.md` § Cleanup. Disposed on this backend alone: its other
            # sandboxes for the key are equally unreclaimable, and no other backend's are
            # touched. Its own failure is logged, never allowed to replace the refusal.
            try:
                reported = await self._backend.dispose(key)
            except Exception as undisposed:  # noqa: BLE001 — the refusal must reach the caller
                reported = str(undisposed)
            if reported is not None:
                logger.warning(
                    "sandbox router: backend %s failed to dispose after a reclaim refusal: %s",
                    self._backend.name,
                    reported,
                )
                # This method's own contract is that a refused acquire leaves nothing billable
                # running. It does here, and now the router knows — so the key is closed rather
                # than served again over a sandbox nothing can reclaim.
                self.mark_unclean(key, _coded(self._backend.name, reported))
            raise
        return sandbox

    async def dispose(self, key: SandboxKey) -> None:
        """Delete every kind's sandbox for ``key``. Best-effort across every registered backend."""
        await self._dispose_each(key)

    async def _dispose_each(self, key: SandboxKey, *, refuse: bool = False) -> bool:
        """Ask every backend to dispose ``key``; ``True`` when none refused.

        A landed disposal clears the key from the unclean set: whatever was in that sandbox
        went with it.  ``refuse`` is what closes the key when one does *not* land, and only
        :meth:`dispose_unclean` passes it: :meth:`dispose` is best-effort and its caller has
        made no claim that the sandbox held anything, so a transient failure there must not
        leave a clean key unservable.  Under ``refuse`` each reason reaches the ledger as its
        backend answers, because this runs inside a bound that can expire mid-loop.

        A backend refuses by *returning* a reason as much as by raising: ``dispose`` never
        raises, so silence is the only thing that may be read as success.
        """
        reasons: list[DisposalFailure] = []
        for backend in self._backends:
            try:
                undisposed = await backend.dispose(key)
            except Exception as exc:  # noqa: BLE001 - disposal must not fail a caller
                # A backend that raises broke its own never-raises contract, so nothing it says
                # can be classified: `unknown` rather than a guess at what went wrong.
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
                # Written before the next backend is awaited, not after the last one answers:
                # `dispose_unclean` bounds this with `asyncio.timeout`, so a later backend that
                # hangs cancels the coroutine and a reason still sitting in this list dies with
                # it — leaving the timeout handler to record `timeout` over a code that outranks
                # it. Recorded over whatever marked the key, so the refusal quotes the latest
                # attempt rather than the sentence that first closed it. No await between the
                # fold and the write.
                self._unclean[key] = fold_disposal_failures(reasons)
        if reasons:
            return False
        self._unclean.pop(key, None)
        return True

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
        refused.  A host that opted down with ``keep_unclean`` gets no ledger write at all — and
        the same bound, which is not part of what that flag loosens.

        Raises:
            ValueError: when ``timeout`` is not a finite positive number of seconds. ``math.inf``
                would leave ``asyncio.timeout`` unable to expire, so the documented bound would
                not hold and a hanging backend would hang the caller. Checked before the key is
                marked, so a rejected call has no lingering effect on the ledger.
        """
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError(f"timeout must be a finite positive number of seconds, not {timeout}")
        # The host opted down from the framework closing the key, and refusing it is the other
        # half of that same act — but not from the bound. This still runs after a tool call's
        # body, and the bound is the only thing between a backend that hangs and a call that
        # never returns, so it wraps both paths and only the ledger writes are conditional.
        refuse = not self._keep_unclean
        if refuse:
            self._unclean.setdefault(key, None)
        try:
            async with asyncio.timeout(timeout):
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
                # Nothing to record under the opt-down: not closing the key is the whole of what
                # the host asked for, and a bound that expired does not change that.
                return False
            expired = DisposalFailure("timeout", f"the disposal did not finish within {timeout}s")
            recorded = self._unclean.get(key)
            # Folded rather than assigned: a previous attempt may have recorded something more
            # actionable, and `unreachable` outranks `timeout` for exactly that reason.
            self._unclean[key] = fold_disposal_failures(
                [expired] if recorded is None else [recorded, expired]
            )
            return False

    def mark_unclean(self, key: SandboxKey, reason: DisposalFailure | str | None = None) -> None:
        """Refuse ``key`` without disposing — for a cleanup cancelled before it could dispose.

        Synchronous, because it is called while a :class:`~asyncio.CancelledError` is propagating
        out of a tool call's cleanup, where awaiting a disposal is not reliable.  The sandbox is
        left refused (:meth:`acquire` raises :class:`SandboxUnclean`) until a later disposal — a
        subsequent :meth:`dispose_unclean`, or :meth:`dispose_scope` — lands.

        The refusal quotes ``reason``'s *code* only.  Its detail is a backend's own sentence and
        stays in the log, so a host must not expect to read it off :class:`SandboxUnclean`.  A
        reason does not overwrite one a disposal already recorded: what a backend reported about
        the sandbox says more than that a cleanup was cut short.
        """
        if self._unclean.get(key) is None:
            if reason is None:
                self._unclean[key] = None
            elif isinstance(reason, DisposalFailure):
                # Folded rather than stored as given: one place decides what a legal code is,
                # and every writer of this ledger goes through it.
                self._unclean[key] = fold_disposal_failures([reason])
            else:
                # The same one-release grace the protocol grants `dispose`: a sentence is read
                # as `unknown` rather than refused, so a caller is never forced to import the
                # class to close a key.
                self._unclean[key] = DisposalFailure("unknown", reason)

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
        what it reclaimed is the one that notices the day the number is zero.
        """
        disposal = ScopeDisposal()
        try:
            yield disposal
        finally:
            disposal.disposed = await self.dispose_scope(scope, thread_id)

    async def dispose_scope(self, scope: str, thread_id: str) -> int:
        """Delete every sandbox for ``(scope, thread_id)``, returning how many.

        Every registered backend is asked, not only the selected one: a conversation may have
        been served while a different backend was configured, and a sandbox nobody reclaims
        is a sandbox somebody pays for.
        """
        total = 0
        landed = True
        for backend in self._backends:
            try:
                total += await backend.dispose_scope(scope, thread_id)
            except Exception as exc:  # noqa: BLE001 - purge must never fail
                landed = False
                logger.warning(
                    "sandbox router: backend %s failed to purge thread %s: %s",
                    backend.name,
                    thread_id,
                    exc,
                )
        if landed:
            # The conversation's sandboxes are gone, so nothing under it holds data any more.
            self._unclean = {
                key: reason
                for key, reason in self._unclean.items()
                if (key.scope, key.thread_id) != (scope, thread_id)
            }
        return total
