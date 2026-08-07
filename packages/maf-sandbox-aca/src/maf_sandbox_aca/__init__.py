"""ACA Sandboxes as a sandbox backend for Microsoft Agent Framework agents (issue #408).

```
app  ->  maf_sandbox  ->  maf_sandbox_aca  ->  the sandbox
```

:class:`AcaSandboxBackend` implements :class:`maf_sandbox.SandboxBackend` on
`Azure Container Apps Sandboxes <https://learn.microsoft.com/azure/container-apps/sandboxes-overview>`_:
VM isolation, Deny-default egress with a per-spec allowlist, no ambient identity inside, and
lifecycle policies that reclaim a billable VM after it goes idle.  It declares
:data:`~maf_sandbox.Isolation.VM`, which is what lets the router permit it in a deployed
environment.

This package is the backend only.  The sandbox kinds that run on it live in sibling
packages under `src/` — :mod:`maf_sandbox_bicep` first; a GitHub Copilot agent and an Azure CLI
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
    "MafSandboxAcaExperimentalWarning",
    "disk_image_base",
    "resolve_disk_image_id",
]

# --- Experimental-package notice (issue #697) -------------------------------------------
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


class MafSandboxAcaExperimentalWarning(UserWarning):
    """Warning category for maf-sandbox-aca's experimental-package notice."""


def _warn_experimental() -> None:
    message = (
        "maf_sandbox_aca is experimental and may change or be removed in future versions "
        "without notice."
    )
    try:
        _warnings.warn(message, category=MafSandboxAcaExperimentalWarning, stacklevel=2)
    except MafSandboxAcaExperimentalWarning:
        # A host running under `python -W error` (or with a blanket
        # `filterwarnings("error")` active) turns the warning above into an exception at
        # the call site. Importing a package must never fail because of an informational
        # notice, so it is swallowed here — this is the one piece of state a `-W error`
        # host is allowed to change: whether the notice was printed, never whether the
        # import succeeded.
        pass


_warn_experimental()
