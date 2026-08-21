"""Backend selection: the layer between a host application and any sandbox provider.

``app -> SandboxRouter -> backend -> the sandbox itself``.  The router owns what no
individual backend can own: **which** backend serves a request, and the five rules that
decide whether it may — a minimum-isolation floor, a capability match, the transfer ceilings,
the egress rule, and the host's outright denials (capabilities and identities this posture
refuses whatever the backend could do).
"""

from __future__ import annotations

import dataclasses
import logging
from collections.abc import AsyncGenerator, Iterable, Sequence
from contextlib import asynccontextmanager

from ._protocol import (
    DEFAULT_CAPABILITIES,
    DEFAULT_SANDBOX_LIMITS,
    ISOLATION_RANK,
    Capability,
    Egress,
    Identity,
    Isolation,
    Sandbox,
    SandboxBackend,
    SandboxKey,
    SandboxLimits,
    SandboxSpec,
    TransferLimits,
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
    "SandboxRouter",
    "SandboxTransferLimitsNotPermitted",
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


class SandboxCapabilityDenied(PermissionError):
    """The workload requires a capability this host's router denies outright.

    The posture counterpart of :class:`SandboxCapabilityNotSupported`: not "the backend
    cannot", but "this host will not", whatever the backend declares.  A hard stop rather
    than awareness — the deny list exists for hosts whose policy about a capability
    (``HOST_TOOLS`` above all) is a refusal, not a classification.
    """


class SandboxIdentityDenied(PermissionError):
    """The workload's dispatched tools exercise an identity this host's router denies.

    Same posture as :class:`SandboxCapabilityDenied`, on the identity axis: a host that
    forbids model-orchestrated user authority states ``denied_identities={Identity.USER}``
    once, and a spec whose registry-derived ``identities`` carries it is refused at attach —
    before anything runs, where every other posture question is answered.
    """


class SandboxEgressNotEnforced(PermissionError):
    """The selected backend cannot enforce the egress mode the workload runs in.

    Refuse, never degrade: the router will not substitute a mode for the one the spec declares
    — a more open one silently widens what the workload reaches, a more isolated one hands it a
    posture it was not built for. So a backend that cannot deliver the asked mode turns the
    workload away rather than serving it behind a different boundary. See
    ``docs/design/egress-resolution.md``.
    """


class SandboxTransferLimitsNotPermitted(PermissionError):
    """The workload's spec asks to move more data than the selected backend allows.

    A safety claim rather than a functionality one, which is why an undeclared ``limits`` is
    read as :data:`~maf_sandbox.DEFAULT_SANDBOX_LIMITS` and a bigger ask refused, where an
    undeclared ``capabilities`` is read charitably.  Also raised for a ``limits`` this package
    cannot read at all — a declaration nobody can compare against is refused, not guessed at.
    """


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
        denied_identities: Identities this host refuses dispatched tools to exercise — a
            spec whose ``identities`` carries one is refused at attach.
            ``denied_identities={Identity.USER}`` is how a host forbids model-orchestrated
            user authority in one statement instead of auditing each registration.

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
    ) -> None:
        self._backends = list(backends)
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
                f"the {spec.kind!r} workload's dispatched tools exercise "
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

        # Silence is a safety claim here, not a functionality one: an undeclared ceiling is
        # the default ceiling, and a spec asking above it is refused rather than believed.
        limits = _declared_limits(self._backend)
        for direction, asked, ceiling in (
            (Capability.FILES_IN, spec.files_in, limits.files_in),
            (Capability.FILES_OUT, spec.files_out, limits.files_out),
        ):
            if not asked.within(ceiling):
                raise SandboxTransferLimitsNotPermitted(
                    f"the {spec.kind!r} workload declares {str(direction)} limits above what "
                    f"sandbox backend {self._backend.name!r} allows: it asks for {asked} and "
                    f"the backend permits {ceiling}. Refused rather than clamped: a workload "
                    "served a smaller cap than it declared fails part-way through a "
                    "collection, and a partial artifact set is worse than none because the "
                    "model cannot tell what it did not get."
                )

        # Egress is resolved, not matched: the workload runs in exactly one mode, and the
        # backend must be able to enforce it. Refuse, never degrade — no more-open substitute
        # (a silent widening) and no more-isolated one (a quietly different posture). Silence is
        # the empty set: a backend that declares nothing enforces nothing. See
        # docs/design/egress-resolution.md.
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
        and ``docs/design/egress-resolution.md``).  With no backend configured this returns:
        nothing runs, so nothing reaches anything.

        Raises:
            SandboxCapabilityDenied: when the spec requires a capability this host denies.
            SandboxIdentityDenied: when the spec's ``identities`` carry one this host denies.
            SandboxBackendNotPermitted: when the spec raises the floor above what the backend
                declares.
            SandboxCapabilityNotSupported: when the backend cannot do what the spec requires.
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
            SandboxCapabilityDenied: when the spec requires a capability this host denies.
            SandboxIdentityDenied: when the spec's ``identities`` carry one this host denies.
            SandboxBackendNotPermitted: when the spec raises the floor above what the backend
                declares.
            SandboxCapabilityNotSupported: when the backend cannot do what the spec requires.
            SandboxTransferLimitsNotPermitted: when the spec's caps exceed the backend's,
                or when the backend declares its ceilings as something other than a
                ``SandboxLimits``.
            SandboxEgressNotEnforced: when the backend cannot confine egress to this spec.
        """
        if self._backend is None:
            raise NoSandboxBackend("no sandbox backend is configured")
        self._refuse_unless_backend_can_serve(spec)
        return await self._backend.acquire(key, spec)

    async def dispose(self, key: SandboxKey) -> None:
        """Delete every kind's sandbox for ``key``. Best-effort across every registered backend."""
        for backend in self._backends:
            try:
                await backend.dispose(key)
            except Exception as exc:  # noqa: BLE001 - disposal must not fail a caller
                logger.warning(
                    "sandbox router: backend %s failed to dispose: %s", backend.name, exc
                )

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
        for backend in self._backends:
            try:
                total += await backend.dispose_scope(scope, thread_id)
            except Exception as exc:  # noqa: BLE001 - purge must never fail
                logger.warning(
                    "sandbox router: backend %s failed to purge thread %s: %s",
                    backend.name,
                    thread_id,
                    exc,
                )
        return total
