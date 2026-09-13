"""Hyperlight's packaged Python guest as a runtime-only maf-sandbox backend."""

import warnings as _warnings

from ._backend import (
    BACKEND_NAME,
    RUNTIME_INSTRUCTIONS,
    HyperlightSandboxBackend,
    HyperlightSandboxInfo,
)
from ._config import HyperlightSandboxConfig
from ._wire import HyperlightOutputLimitExceeded, HyperlightWorkerError

__all__ = [
    "BACKEND_NAME",
    "RUNTIME_INSTRUCTIONS",
    "HyperlightSandboxBackend",
    "HyperlightSandboxConfig",
    "HyperlightSandboxInfo",
    "HyperlightOutputLimitExceeded",
    "HyperlightWorkerError",
    "MafSandboxHyperlightExperimentalWarning",
]


class MafSandboxHyperlightExperimentalWarning(UserWarning):
    """Warning category for maf-sandbox-hyperlight's experimental-package notice."""


def _warn_experimental() -> None:
    try:
        _warnings.warn(
            "maf_sandbox_hyperlight is experimental and may change or be removed in future "
            "versions without notice.",
            category=MafSandboxHyperlightExperimentalWarning,
            stacklevel=2,
        )
    except MafSandboxHyperlightExperimentalWarning:
        # An informational notice must not prevent importing under -W error.
        pass


_warn_experimental()
