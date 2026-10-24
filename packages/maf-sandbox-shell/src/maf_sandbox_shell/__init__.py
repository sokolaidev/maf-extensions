"""Offline shell script validation for Microsoft Agent Framework."""

import warnings as _warnings

from ._spec import (
    SHELL_TOOL_NAMES,
    ShellDialect,
    ShellFlavor,
    ShellPolicy,
    ShellSeverity,
    shell_sandbox_spec,
)
from ._tool import make_shell_tools

__all__ = [
    "MafSandboxShellExperimentalWarning",
    "SHELL_TOOL_NAMES",
    "ShellDialect",
    "ShellFlavor",
    "ShellPolicy",
    "ShellSeverity",
    "make_shell_tools",
    "shell_sandbox_spec",
]


class MafSandboxShellExperimentalWarning(UserWarning):
    """Warning category for the experimental shell workload package."""


def _warn_experimental() -> None:
    try:
        _warnings.warn(
            "maf_sandbox_shell is experimental and may change or be removed in future "
            "versions without notice.",
            category=MafSandboxShellExperimentalWarning,
            stacklevel=2,
        )
    except MafSandboxShellExperimentalWarning:
        # An informational warning must not block imports under -Werror.
        pass


_warn_experimental()
