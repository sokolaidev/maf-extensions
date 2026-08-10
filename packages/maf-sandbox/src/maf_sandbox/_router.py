"""Backend selection: the layer between a host application and any sandbox provider.

``app -> SandboxRouter -> backend -> the sandbox itself``.  The router owns what no
individual backend can own: **which** backend serves a request, and the three rules that
decide whether it may — a minimum-isolation floor, a capability match, and the egress rule.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

from ._protocol import (
    DEFAULT_CAPABILITIES,
    ISOLATION_RANK,
    Capability,
    Egress,
    Isolation,
    Sandbox,
    SandboxBackend,
    SandboxKey,
    SandboxSpec,
    meets_floor,
)

logger = logging.getLogger(__name__)

__all__ = [
    "NoSandboxBackend",
    "SandboxBackendNotPermitted",
    "SandboxCapabilityNotSupported",
    "SandboxEgressNotEnforced",
    "SandboxRouter",
]


#: The rungs, weakest first, rendered once for the refusal messages.
_LADDER = ", ".join(map(str, ISOLATION_RANK))


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


class SandboxEgressNotEnforced(PermissionError):
    """The selected backend cannot confine egress to what the workload's spec allows.

    Raised rather than degraded, like :class:`SandboxBackendNotPermitted`: a backend that
    accepts ``egress_allow`` and ignores it turns the containment a workload was designed
    around into a comment.
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


class SandboxRouter:
    """Routes a sandbox request to a backend.

    Args:
        backends: The registered backends, in preference order.
        min_isolation: The weakest boundary this host accepts. Defaults to
            :data:`Isolation.MICROVM`.
        selected: Name of the backend to use. ``None`` picks the first registered one, which
            with a single backend is the whole selection story and stays correct when more
            arrive.

    Raises:
        SandboxBackendNotPermitted: at construction, when the selected backend declares a
            rung below ``min_isolation`` or one this package does not recognise. Failing
            here rather than at first use means a misconfigured deployment cannot start with
            the feature apparently enabled and quietly unsafe.
        ValueError: at construction, when ``min_isolation`` is not a rung this package
            recognises — raised by :class:`Isolation` itself rather than surfacing later as a
            bare ``KeyError`` out of a rank comparison, which would only happen once a backend
            was registered and a floor was actually compared against.
    """

    def __init__(
        self,
        backends: Sequence[SandboxBackend],
        *,
        min_isolation: Isolation = Isolation.MICROVM,
        selected: str | None = None,
    ) -> None:
        self._backends = list(backends)
        self._min_isolation = Isolation(str(min_isolation))
        self._selected_name = selected
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
        """Raise unless the selected backend can serve ``spec``: floor, capabilities, egress.

        The REFUSING half of the policy, shared by :meth:`ensure_can_serve` and
        :meth:`acquire`. It never logs: the closed-egress-vs-allowlist-spec WARNING is
        :meth:`ensure_can_serve`'s alone, because :meth:`acquire` is called every iteration of
        a warm fix-round loop and must not repeat it. With no backend configured this returns:
        nothing runs, so nothing reaches anything.
        """
        if self._backend is None:
            return

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

        # Silence is read as enforcing nothing, not excused: a backend written before this
        # property existed cannot have been enforcing an allowlist it never read.
        egress = getattr(self._backend, "egress", Egress.UNRESTRICTED)
        if egress in (Egress.ALLOWLIST, Egress.CLOSED):
            return
        raise SandboxEgressNotEnforced(
            f"sandbox backend {self._backend.name!r} declares {str(egress)!r} egress, which "
            f"cannot enforce the {spec.kind!r} workload's allowlist "
            f"({', '.join(spec.egress_allow) or 'no network at all'}). "
            f"A backend must declare one of {str(Egress.ALLOWLIST)!r} or "
            f"{str(Egress.CLOSED)!r} to serve a workload at all — everything a spec does not "
            "name is meant to be denied."
        )

    def ensure_can_serve(self, spec: SandboxSpec) -> None:
        """Raise unless the selected backend can serve ``spec``: floor, capabilities, egress.

        Called for you by :func:`maf_sandbox.maf.sandboxed_tool`, and it is also the whole of
        a host's own wiring test::

            router.ensure_can_serve(bicep_sandbox_spec())

        Confining more egress than the spec asks is permitted and warned about; confining
        less is refused (see :class:`~maf_sandbox.Egress`).  With no backend configured this
        returns: nothing runs, so nothing reaches anything.

        Raises:
            SandboxBackendNotPermitted: when the spec raises the floor above what the backend
                declares.
            SandboxCapabilityNotSupported: when the backend cannot do what the spec requires.
            SandboxEgressNotEnforced: when the backend cannot confine egress to this spec.
        """
        self._refuse_unless_backend_can_serve(spec)
        if self._backend is None:
            return

        egress = getattr(self._backend, "egress", Egress.UNRESTRICTED)
        if egress == Egress.CLOSED and spec.egress_allow:
            logger.warning(
                "sandbox backend %r cannot allow named hosts, so %s will be unreachable "
                "from a %r sandbox; expect the workload to report what it could not fetch",
                self._backend.name,
                ", ".join(spec.egress_allow),
                spec.kind,
            )

    async def acquire(self, key: SandboxKey, spec: SandboxSpec) -> Sandbox:
        """Return a running sandbox for ``key``, creating one if needed.

        Runs the same floor, capability and egress checks as :meth:`ensure_can_serve` — minus
        its WARNING, which stays there — before ever reaching the backend, so a caller that
        skips :meth:`ensure_can_serve` is still refused rather than served behind a boundary
        or capability set the spec did not agree to.

        Raises:
            NoSandboxBackend: when no backend is configured. Callers that check
                :attr:`enabled` before attaching a tool never reach this.
            SandboxBackendNotPermitted: when the spec raises the floor above what the backend
                declares.
            SandboxCapabilityNotSupported: when the backend cannot do what the spec requires.
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
