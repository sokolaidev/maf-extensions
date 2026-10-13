"""Experimental supervised MCP workload service with a fixed Bicep composition."""

import warnings as _warnings


class MafSandboxMcpExperimentalWarning(UserWarning):
    """Warning category for maf-sandbox-mcp's experimental-package notice."""


def _warn_experimental() -> None:
    try:
        _warnings.warn(
            "maf_sandbox_mcp is experimental and may change or be removed without notice.",
            category=MafSandboxMcpExperimentalWarning,
            stacklevel=2,
        )
    except MafSandboxMcpExperimentalWarning:
        # An informational notice must not fail import under -W error.
        pass


_warn_experimental()
