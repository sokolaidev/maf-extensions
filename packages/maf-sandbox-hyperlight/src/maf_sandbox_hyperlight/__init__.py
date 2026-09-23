"""Hyperlight's packaged Python guest with snapshot reset and optional output files."""

import warnings as _warnings

from ._backend import (
    BACKEND_NAME,
    FILE_RUNTIME_INSTRUCTIONS,
    RUNTIME_INSTRUCTIONS,
    HyperlightSandboxBackend,
)
from ._config import HyperlightSandboxConfig
from ._pod_config import HyperlightPodConfig
from ._wire import HyperlightOutputLimitExceeded, HyperlightWorkerError

__all__ = [
    "BACKEND_NAME",
    "RUNTIME_INSTRUCTIONS",
    "FILE_RUNTIME_INSTRUCTIONS",
    "HyperlightSandboxBackend",
    "HyperlightSandboxConfig",
    "HyperlightPodConfig",
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
