"""Backend selection: the layer between a host application and any sandbox provider.

``app -> SandboxRouter -> backend -> the sandbox itself``.  The router owns two things no
individual backend can own: **which** backend serves a request, and the rule that a weaker
boundary is never selected in a deployed environment.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

from ._protocol import Egress, Isolation, Sandbox, SandboxBackend, SandboxKey, SandboxSpec

logger = logging.getLogger(__name__)

__all__ = [
    "DEPLOYED_ISOLATION",
    "NoSandboxBackend",
    "SandboxBackendNotPermitted",
    "SandboxEgressNotEnforced",
    "SandboxRouter",
]

#: The only isolation level permitted when the host reports it is running deployed.
#: A hardened container runtime (gVisor, Kata, Firecracker) is deliberately NOT in this set:
#: the security-posture doc's claims are written against a hypervisor boundary, so admitting
#: anything else is a decision to be made there first, not here.
DEPLOYED_ISOLATION = frozenset({Isolation.VM})


class NoSandboxBackend(LookupError):
    """No backend is configured, or the named one is not registered."""


class SandboxBackendNotPermitted(PermissionError):
    """The selected backend's isolation is too weak for a deployed environment.

    Raised rather than degraded on purpose.  Silently falling back to a stronger backend
    would hide a misconfiguration, and silently proceeding with the weaker one would break
    the claim the posture doc makes about every execution surface.
    """


class SandboxEgressNotEnforced(PermissionError):
    """The selected backend cannot confine egress to what the workload's spec allows.

    Raised rather than degraded, like :class:`SandboxBackendNotPermitted`: a backend that
    accepts ``egress_allow`` and ignores it turns the containment a workload was designed
    around into a comment.
    """


class SandboxRouter:
    """Routes a sandbox request to a backend.

    Args:
        backends: The registered backends, in preference order.
        deployed: Whether the host is running deployed. The host decides this — it is the
            same signal it already uses elsewhere — and the router treats it as ground truth.
        selected: Name of the backend to use. ``None`` picks the first registered one, which
            with a single backend is the whole selection story and stays correct when more
            arrive.

    Raises:
        SandboxBackendNotPermitted: at construction, when a deployed host is configured with
            a backend weaker than :data:`DEPLOYED_ISOLATION`. Failing here rather than at
            first use means a misconfigured deployment cannot start with the feature
            apparently enabled and quietly unsafe.
    """

    def __init__(
        self,
        backends: Sequence[SandboxBackend],
        *,
        deployed: bool = False,
        selected: str | None = None,
    ) -> None:
        self._backends = list(backends)
        self._deployed = deployed
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

        if self._deployed and backend.isolation not in DEPLOYED_ISOLATION:
            raise SandboxBackendNotPermitted(
                f"sandbox backend {backend.name!r} has {backend.isolation!r} isolation, "
                f"which is not permitted in a deployed environment "
                f"(permitted: {', '.join(sorted(DEPLOYED_ISOLATION))}). "
                "A shared-kernel boundary sits next to the host's credentials, which is "
                "why it is not accepted here."
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

    def ensure_can_serve(self, spec: SandboxSpec) -> None:
        """Raise unless the selected backend can confine egress to what ``spec`` allows.

        Called for you by :func:`maf_sandbox.maf.sandboxed_tool`, and it is also the whole of
        a host's own wiring test::

            router.ensure_can_serve(bicep_sandbox_spec())

        Confining more than the spec asks is permitted and warned about; confining less is
        refused (see :class:`~maf_sandbox.Egress`).  With no backend configured this returns:
        nothing runs, so nothing reaches anything.

        Raises:
            SandboxEgressNotEnforced: when the backend cannot meet this spec.
        """
        if self._backend is None:
            return
        # Silence is read as enforcing nothing, not excused: a backend written before this
        # property existed cannot have been enforcing an allowlist it never read.
        egress = getattr(self._backend, "egress", Egress.UNRESTRICTED)
        if egress == Egress.ALLOWLIST:
            return
        if egress == Egress.CLOSED:
            if spec.egress_allow:
                logger.warning(
                    "sandbox backend %r cannot allow named hosts, so %s will be unreachable "
                    "from a %r sandbox; expect the workload to report what it could not fetch",
                    self._backend.name,
                    ", ".join(spec.egress_allow),
                    spec.kind,
                )
            return
        raise SandboxEgressNotEnforced(
            f"sandbox backend {self._backend.name!r} declares {egress!r} egress, which "
            f"cannot enforce the {spec.kind!r} workload's allowlist "
            f"({', '.join(spec.egress_allow) or 'no network at all'}). "
            f"A backend must declare one of {Egress.ALLOWLIST!r} or {Egress.CLOSED!r} to "
            "serve a workload at all — everything a spec does not name is meant to be denied."
        )

    async def acquire(self, key: SandboxKey, spec: SandboxSpec) -> Sandbox:
        """Return a running sandbox for ``key``, creating one if needed.

        Raises:
            NoSandboxBackend: when no backend is configured. Callers that check
                :attr:`enabled` before attaching a tool never reach this.
        """
        if self._backend is None:
            raise NoSandboxBackend("no sandbox backend is configured")
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
