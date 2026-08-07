"""Sandbox router: one seam between a host application and any sandbox provider.

```
app  ->  SandboxRouter  ->  backend  ->  the sandbox
```

A workload asks for a sandbox and runs a command in it.  A backend decides what actually
boots — an ACA Sandbox (`maf-sandbox-aca`) today, a local Docker container or an
in-process fake later.  Neither knows about the other, which is what lets the same tool run
against all of them unchanged.

The router exists for two things a backend cannot own:

- **Which backend serves a request.** Configuration, not an import, decides.
- **The deployed-isolation rule.** A backend weaker than a VM boundary is refused outright
  when the host reports it is running deployed — see
  :class:`~maf_sandbox._router.SandboxBackendNotPermitted`.  This is the router's one part
  that is a security property rather than a convenience, so it is enforced at construction
  and pinned by tests.

This package imports no backend and no host application.

One module sits deliberately outside that claim and is deliberately not re-exported here:
:mod:`maf_sandbox.maf`, the MAF glue (``make_workspace_context``, ``sandboxed_tool``, and the
purge participant).  It is the only module allowed to import ``agent_framework``, and keeping
it off this ``__init__`` is what lets ``import maf_sandbox`` stay cheap and framework-free for
a backend or a test that only speaks the protocol.  Reach it by name —
``from maf_sandbox.maf import sandboxed_tool``.
"""

from __future__ import annotations

from ._error_detail import error_detail
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
    "MafSandboxExperimentalWarning",
    "NoSandboxBackend",
    "Sandbox",
    "SandboxBackend",
    "SandboxBackendNotPermitted",
    "SandboxKey",
    "SandboxPurger",
    "SandboxRouter",
    "SandboxSpec",
    "WorkspaceContext",
    "error_detail",
]

# --- Experimental-package notice ---------------------------------------------------------
# This package is early-stage (0.1.0, "Development Status :: 4 - Beta"). Mirrors
# `agent_framework`'s own experimental-feature idiom (see its `_feature_stage` module and
# `ExperimentalWarning`, a `FutureWarning` subclass) but deliberately subclasses
# `UserWarning` instead: a host that runs under `python -W error` (many CI/production
# launchers do) would have importing this package alone raise before any of its own code
# runs if the category were a `FutureWarning`. `UserWarning` keeps the notice
# informational-by-default while staying a real, catchable, filterwarnings-suppressible
# category — see the try/except immediately below for how `-W error` is handled anyway.
#
# Duplicated (not imported from a shared module) in each of the three maf-sandbox*
# packages on purpose — a shared warnings module would be a cross-package dependency this
# split is designed to avoid.
import warnings as _warnings


class MafSandboxExperimentalWarning(UserWarning):
    """Warning category for maf-sandbox's experimental-package notice."""


def _warn_experimental() -> None:
    message = (
        "maf_sandbox is experimental and may change or be removed in future versions "
        "without notice."
    )
    try:
        _warnings.warn(message, category=MafSandboxExperimentalWarning, stacklevel=2)
    except MafSandboxExperimentalWarning:
        # A host running under `python -W error` (or with a blanket
        # `filterwarnings("error")` active) turns the warning above into an exception at
        # the call site. Importing a package must never fail because of an informational
        # notice, so it is swallowed here — this is the one piece of state a `-W error`
        # host is allowed to change: whether the notice was printed, never whether the
        # import succeeded.
        pass


_warn_experimental()
