"""Configuration for the ACA Sandboxes backend.

A plain frozen dataclass rather than a settings model: a host already has its own
configuration system, and requiring a particular one would be exactly the coupling this
package avoids.  The host maps its settings onto this.

Note what is *not* here.  The image's ``repository:tag``, the egress allowlist and the work
directory are properties of a sandbox **kind**, not of the backend, so they travel in a
:class:`~maf_sandbox.SandboxSpec` — which is what lets a second kind (a Copilot agent, an
Azure CLI surface) arrive without touching this file.  The *registry* is the other way
round: one registry serves the sandbox group and the group serves every kind, so it lives
here and a kind never learns where its image is stored.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import cast

from ._credentials import AcasCredentialResolver

__all__ = ["AcasSandboxConfig"]


@dataclass(frozen=True)
class AcasSandboxConfig:
    """Which sandbox group to talk to, and how long its sandboxes may linger.

    ``endpoint`` is the group's data-plane endpoint
    (``https://management.<region>.azuredevcompute.io``).  It has no default because a
    config without one cannot be used — a host with nothing configured should not build one
    at all, and its router then reports :attr:`~maf_sandbox.SandboxRouter.enabled` false.

    ``registry`` is the login server (FQDN) that holds the sandbox images, e.g.
    ``myregistry.azurecr.io``.  A kind's spec carries only ``repository:tag`` and this
    is what qualifies it, so moving to a different registry is one setting rather than one
    per kind.  A spec that already names a registry is left alone.

    The two lifecycle bounds govern how long a billable sandbox survives: suspension after idle,
    then deletion after being stopped.  They are backend-level because they describe the
    sandbox rather than the work it does.
    """

    endpoint: str
    subscription_id: str = ""
    resource_group: str = ""
    sandbox_group: str = ""
    registry: str = ""
    auto_suspend_seconds: int = 60
    auto_delete_seconds: int = 600
    #: A read that never returns must not hold the caller's turn open. The service reports a
    #: FIFO identically to an empty regular file — same mode, both type flags false — so a
    #: guest can put one where a declared output belongs and the read blocks forever.
    read_timeout_seconds: float = 120.0
    #: Maximum returned bytes per exec stream; capture retains one extra byte to detect overflow.
    exec_output_limit_bytes: int = 1 << 20
    #: Resolve from trusted host context on acquire, from durable target scope on disposal.
    credential_resolver: AcasCredentialResolver | None = None
    max_clients_per_loop: int = 32
    client_wait_seconds: float = 30.0
    client_close_seconds: float = 30.0
    #: Fresh management read, including authentication and SDK retries, on every acquire.
    identity_check_seconds: float = 15.0

    def __post_init__(self) -> None:
        if self.credential_resolver is not None and not callable(self.credential_resolver):
            raise ValueError("credential_resolver must be callable")
        if type(self.max_clients_per_loop) is not int or self.max_clients_per_loop < 1:
            raise ValueError("max_clients_per_loop must be a positive integer")
        for name in ("client_wait_seconds", "client_close_seconds", "identity_check_seconds"):
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value <= 0
            ):
                raise ValueError(f"{name} must be finite and positive")
        if (
            isinstance(self.exec_output_limit_bytes, bool)
            or not isinstance(cast(object, self.exec_output_limit_bytes), int)
            or self.exec_output_limit_bytes < 1
        ):
            raise ValueError("exec_output_limit_bytes must be a positive integer")
