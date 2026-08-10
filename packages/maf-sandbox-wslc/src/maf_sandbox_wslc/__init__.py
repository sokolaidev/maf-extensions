"""WSL containers as a sandbox backend for Microsoft Agent Framework agents.

```
app  ->  maf_sandbox  ->  maf_sandbox_wslc  ->  the container
```

:class:`WslcSandboxBackend` implements :class:`maf_sandbox.SandboxBackend` on ``wslc``, the
container CLI that ships with WSL 2.9.3 and later: a container on the developer's own
machine, created in about half a second, with no subscription, no daemon and no login.

It declares :data:`~maf_sandbox.Isolation.CONTAINER`, below the router's default
:data:`~maf_sandbox.Isolation.MICROVM` floor — a host opts down explicitly with
``min_isolation=Isolation.CONTAINER``, and with nothing passed construction refuses this
backend.  Egress is :data:`~maf_sandbox.Egress.CLOSED` by default — ``--network none`` on
every container — and becomes
:data:`~maf_sandbox.Egress.ALLOWLIST` when the config names a
:func:`proxy_build_context`-built image, which places each sandbox on its own internal
network behind a filtering proxy.  These are honest downgrades from a VM-isolated backend, and
they are what this package is *for*: the same workload, run locally.

This package is the backend only.  The sandbox kinds that run on it live in sibling packages
and are written against the router's protocol, not against this backend, so they never import
it.

This package imports no host application and no agent framework.
"""

from __future__ import annotations

from ._backend import WslcSandboxBackend
from ._config import WslcSandboxConfig
from ._proxy import build_context as proxy_build_context

__all__ = [
    "MafSandboxWslcExperimentalWarning",
    "WslcSandboxBackend",
    "WslcSandboxConfig",
    "proxy_build_context",
]

# --- Experimental-package notice ---------------------------------------------------------
# This package is early-stage ("Development Status :: 4 - Beta"). Mirrors `agent_framework`'s
# own experimental-feature idiom (see its `_feature_stage` module and `ExperimentalWarning`, a
# `FutureWarning` subclass) but deliberately subclasses `UserWarning` instead: a host that runs
# under `python -W error` (many CI/production launchers do) would have importing this package
# alone raise before any of its own code runs if the category were a `FutureWarning`.
# `UserWarning` keeps the notice informational-by-default while staying a real, catchable,
# filterwarnings-suppressible category — see the try/except below for how `-W error` is
# handled anyway.
#
# Duplicated (not imported from a shared module) in each of the maf-sandbox* packages on
# purpose — a shared warnings module would be a cross-package dependency this split is
# designed to avoid.
import warnings as _warnings


class MafSandboxWslcExperimentalWarning(UserWarning):
    """Warning category for maf-sandbox-wslc's experimental-package notice."""


def _warn_experimental() -> None:
    message = (
        "maf_sandbox_wslc is experimental and may change or be removed in future versions "
        "without notice."
    )
    try:
        _warnings.warn(message, category=MafSandboxWslcExperimentalWarning, stacklevel=2)
    except MafSandboxWslcExperimentalWarning:
        # A host running under `python -W error` (or with a blanket `filterwarnings("error")`
        # active) turns the warning above into an exception at the call site. Importing a
        # package must never fail because of an informational notice, so it is swallowed here
        # — this is the one piece of state a `-W error` host is allowed to change: whether the
        # notice was printed, never whether the import succeeded.
        pass


_warn_experimental()
