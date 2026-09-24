"""Docker Sandboxes as a sandbox backend for Microsoft Agent Framework agents.

```
app  ->  maf_sandbox  ->  maf_sandbox_docker_sbx  ->  the microVM
```

:class:`SbxSandboxBackend` implements :class:`maf_sandbox.SandboxBackend` on ``sbx``, Docker's
standalone sandbox CLI: one microVM with its own kernel per sandbox, on the developer's own
Windows, macOS or Linux machine.

It declares :data:`~maf_sandbox.Isolation.MICROVM`, the router's default floor, and
:data:`~maf_sandbox.Egress.CLOSED` only.  That claim also rests on two host-wide ``sbx``
settings this backend reads at every acquire and never writes: SSH agent forwarding must be
off, and no MCP server may be registered.

This package imports no host application and no agent framework.
"""

from __future__ import annotations

from ._backend import (
    BACKEND_NAME,
    SbxDaemonFault,
    SbxError,
    SbxHostNotConfined,
    SbxLoginRequired,
    SbxSandboxBackend,
)
from ._config import SbxSandboxConfig

__all__ = [
    "BACKEND_NAME",
    "MafSandboxDockerSbxExperimentalWarning",
    "SbxDaemonFault",
    "SbxError",
    "SbxHostNotConfined",
    "SbxLoginRequired",
    "SbxSandboxBackend",
    "SbxSandboxConfig",
]

# --- Experimental-package notice ---------------------------------------------------------
# A `UserWarning` rather than a `FutureWarning`, so `python -W error` does not fail the import;
# duplicated in each maf-sandbox* package rather than shared, to keep packages independent.
import warnings as _warnings


class MafSandboxDockerSbxExperimentalWarning(UserWarning):
    """Warning category for maf-sandbox-docker-sbx's experimental-package notice."""


def _warn_experimental() -> None:
    message = (
        "maf_sandbox_docker_sbx is experimental and may change or be removed in future "
        "versions without notice."
    )
    try:
        _warnings.warn(message, category=MafSandboxDockerSbxExperimentalWarning, stacklevel=2)
    except MafSandboxDockerSbxExperimentalWarning:
        # Under `-W error` the notice raises; an import must never fail because of it.
        pass


_warn_experimental()
