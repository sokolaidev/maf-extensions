"""A cooperative operator console for maf-sandbox applications."""

import warnings as _warnings

from ._app import SandboxConsole
from ._client import CompositeControl, ControlEndpointError, HttpControl, discover_controls
from ._control import HyperlightControl, MemoryControl, SandboxControl
from ._models import (
    DisposalResult,
    DisposalStatus,
    PurgeResult,
    PurgeStatus,
    SandboxRecord,
    SandboxState,
)
from ._server import EndpointManifest, SandboxControlServer, runtime_directory

__all__ = [
    "CompositeControl",
    "ControlEndpointError",
    "DisposalResult",
    "DisposalStatus",
    "EndpointManifest",
    "HttpControl",
    "HyperlightControl",
    "MafSandboxTuiExperimentalWarning",
    "MemoryControl",
    "PurgeResult",
    "PurgeStatus",
    "SandboxConsole",
    "SandboxControl",
    "SandboxControlServer",
    "SandboxRecord",
    "SandboxState",
    "discover_controls",
    "runtime_directory",
]


class MafSandboxTuiExperimentalWarning(UserWarning):
    """Warning category for maf-sandbox-tui's experimental-package notice."""


def _warn_experimental() -> None:
    try:
        _warnings.warn(
            "maf_sandbox_tui is experimental and may change or be removed in future versions "
            "without notice.",
            category=MafSandboxTuiExperimentalWarning,
            stacklevel=2,
        )
    except MafSandboxTuiExperimentalWarning:
        pass


_warn_experimental()
