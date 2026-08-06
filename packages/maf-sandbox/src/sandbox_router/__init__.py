"""Sandbox router: one seam between a host application and any sandbox provider (issue #663).

```
app  ->  SandboxRouter  ->  backend  ->  the sandbox
```

A workload asks for a sandbox and runs a command in it.  A backend decides what actually
boots — an ACA Sandbox (`maf-aca-sandboxes`) today, a local Docker container or an
in-process fake later.  Neither knows about the other, which is what lets the same tool run
against all of them unchanged.

The router exists for two things a backend cannot own:

- **Which backend serves a request.** Configuration, not an import, decides.
- **The deployed-isolation rule.** A backend weaker than a VM boundary is refused outright
  when the host reports it is running deployed — see
  :class:`~sandbox_router._router.SandboxBackendNotPermitted`.  This is the one part of
  issue #663 that is a security property rather than a convenience, so it is enforced at
  construction and pinned by tests.

This package imports no backend and no host application.
"""

from __future__ import annotations

from ._protocol import (
    ExecResult,
    Isolation,
    Sandbox,
    SandboxBackend,
    SandboxKey,
    SandboxSpec,
    WorkspaceContext,
)
from ._purger import SandboxPurger
from ._router import (
    DEPLOYED_ISOLATION,
    NoSandboxBackend,
    SandboxBackendNotPermitted,
    SandboxRouter,
)

__all__ = [
    "DEPLOYED_ISOLATION",
    "ExecResult",
    "Isolation",
    "NoSandboxBackend",
    "Sandbox",
    "SandboxBackend",
    "SandboxBackendNotPermitted",
    "SandboxKey",
    "SandboxPurger",
    "SandboxRouter",
    "SandboxSpec",
    "WorkspaceContext",
]
