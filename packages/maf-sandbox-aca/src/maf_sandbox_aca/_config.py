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

from dataclasses import dataclass

__all__ = ["AcaSandboxConfig"]


@dataclass(frozen=True)
class AcaSandboxConfig:
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
