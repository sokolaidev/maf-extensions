"""MXC durable session and coordinator runtime support."""

import warnings as _warnings


class MafSandboxMxcExperimentalWarning(UserWarning):
    """Warning category for maf-sandbox-mxc's experimental-package notice."""


def _warn_experimental() -> None:
    try:
        _warnings.warn(
            "maf_sandbox_mxc is experimental and may change or be removed without notice.",
            category=MafSandboxMxcExperimentalWarning,
            stacklevel=2,
        )
    except MafSandboxMxcExperimentalWarning:
        # An informational notice must not fail import under -W error.
        pass


_warn_experimental()
