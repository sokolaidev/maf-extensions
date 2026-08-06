"""ACA Sandboxes as a sandbox backend, plus the sandbox kinds that run on it (issue #408).

```
app  ->  sandbox_router  ->  maf_aca_sandboxes  ->  a sandbox kind (bicep today)
```

:class:`AcaSandboxBackend` implements :class:`sandbox_router.SandboxBackend` on
`Azure Container Apps Sandboxes <https://learn.microsoft.com/azure/container-apps/sandboxes-overview>`_:
VM isolation, Deny-default egress with a per-spec allowlist, no ambient identity inside, and
lifecycle policies that reclaim a billable VM after it goes idle.  It declares
:data:`~sandbox_router.Isolation.VM`, which is what lets the router permit it in a deployed
environment.

The kinds live in subpackages — :mod:`maf_aca_sandboxes.bicep` today; a GitHub Copilot agent
and an Azure CLI surface are the obvious next ones, and each arrives as a sibling rather than
as changes to the backend.  A kind is written against the router's protocol, not against this
backend, so it keeps working the day one runs somewhere else.

This package imports no host application.
"""

from __future__ import annotations

from ._backend import AcaSandboxBackend
from ._config import AcaConfig
from ._images import disk_image_base, resolve_disk_image_id

__all__ = [
    "AcaConfig",
    "AcaSandboxBackend",
    "disk_image_base",
    "resolve_disk_image_id",
]
