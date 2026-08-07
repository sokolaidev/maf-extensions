"""ACA Sandboxes as a sandbox backend for Microsoft Agent Framework agents (issue #408).

```
app  ->  sandbox_router  ->  maf_aca_sandboxes  ->  the sandbox
```

:class:`AcaSandboxBackend` implements :class:`sandbox_router.SandboxBackend` on
`Azure Container Apps Sandboxes <https://learn.microsoft.com/azure/container-apps/sandboxes-overview>`_:
VM isolation, Deny-default egress with a per-spec allowlist, no ambient identity inside, and
lifecycle policies that reclaim a billable VM after it goes idle.  It declares
:data:`~sandbox_router.Isolation.VM`, which is what lets the router permit it in a deployed
environment.

This package is the backend only.  The sandbox kinds that run on it live in sibling
packages under `src/` — :mod:`sandbox_bicep` first; a GitHub Copilot agent and an Azure CLI
surface are the obvious next ones.  A kind is written against the router's protocol, not
against this backend, so it keeps working the day one runs somewhere else, and it never
imports this package directly.

This package imports no host application.
"""

from __future__ import annotations

from ._backend import AcaSandboxBackend
from ._config import AcaSandboxConfig
from ._images import disk_image_base, resolve_disk_image_id

__all__ = [
    "AcaSandboxBackend",
    "AcaSandboxConfig",
    "disk_image_base",
    "resolve_disk_image_id",
]
