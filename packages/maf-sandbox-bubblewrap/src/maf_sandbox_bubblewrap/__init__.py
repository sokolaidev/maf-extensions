"""Linux Bubblewrap sandboxes with closed networking and mandatory cgroup limits."""

import warnings as _warnings

from ._backend import BubblewrapSandboxBackend
from ._config import BubblewrapSandboxConfig

__all__ = ["BubblewrapSandboxBackend", "BubblewrapSandboxConfig"]


class MafSandboxBubblewrapExperimentalWarning(UserWarning):
    """The backend is experimental and requires host qualification."""


try:
    _warnings.warn(
        "maf_sandbox_bubblewrap is experimental and may change without notice.",
        MafSandboxBubblewrapExperimentalWarning,
        stacklevel=2,
    )
except MafSandboxBubblewrapExperimentalWarning:
    pass
