"""A maf-sandbox router as a Deep Agents sandbox backend.

```
deepagents  ->  maf_sandbox_deepagents  ->  maf_sandbox (router)  ->  a backend  ->  the sandbox
```

:class:`MafSandbox` implements Deep Agents' ``BaseSandbox`` over a
:class:`~maf_sandbox.SandboxRouter`: the agent gets Deep Agents' own ``execute`` and file tools,
and the host keeps the router's decisions — the isolation floor, the egress mode, the
capability match, keying from the request context, and purge by conversation.

This package imports ``maf_sandbox`` and ``deepagents``, and no backend and no agent framework.
"""

from ._sandbox import (
    DEEPAGENTS_KIND,
    DEFAULT_EXEC_TIMEOUT_SECONDS,
    DEFAULT_WORK_DIR,
    REQUIRED_CAPABILITIES,
    SANDBOX_UNAVAILABLE,
    STORAGE_BASE,
    MafSandbox,
    deepagents_spec,
)

__all__ = [
    "DEEPAGENTS_KIND",
    "DEFAULT_EXEC_TIMEOUT_SECONDS",
    "DEFAULT_WORK_DIR",
    "REQUIRED_CAPABILITIES",
    "SANDBOX_UNAVAILABLE",
    "STORAGE_BASE",
    "MafSandbox",
    "MafSandboxDeepagentsExperimentalWarning",
    "deepagents_spec",
]

# Experimental package (Beta): importing it emits a UserWarning rather than a FutureWarning,
# so a host running under `python -W error` can still import it.
import warnings as _warnings


class MafSandboxDeepagentsExperimentalWarning(UserWarning):
    """Warning category for maf-sandbox-deepagents's experimental-package notice."""


def _warn_experimental() -> None:
    message = (
        "maf_sandbox_deepagents is experimental and may change or be removed in future versions "
        "without notice."
    )
    try:
        _warnings.warn(message, category=MafSandboxDeepagentsExperimentalWarning, stacklevel=2)
    except MafSandboxDeepagentsExperimentalWarning:
        # Deliberate: under `-W error` an informational notice must not fail the import.
        pass


_warn_experimental()
