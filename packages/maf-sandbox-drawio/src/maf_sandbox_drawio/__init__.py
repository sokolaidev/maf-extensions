"""Editable draw.io files from model XML, with host-configured automatic layout."""

import warnings as _warnings

from ._tool import (
    CREATE_DRAWIO_TOOL_NAME,
    DRAWIO_KIND,
    drawio_sandbox_spec,
    make_drawio_tools,
)

__all__ = [
    "CREATE_DRAWIO_TOOL_NAME",
    "DRAWIO_KIND",
    "MafSandboxDrawioExperimentalWarning",
    "drawio_sandbox_spec",
    "make_drawio_tools",
]


class MafSandboxDrawioExperimentalWarning(UserWarning):
    """Warning category for the experimental draw.io kind."""


def _warn_experimental() -> None:
    try:
        _warnings.warn(
            "maf_sandbox_drawio is experimental and may change or be removed without notice.",
            category=MafSandboxDrawioExperimentalWarning,
            stacklevel=2,
        )
    except MafSandboxDrawioExperimentalWarning:
        # An informational notice must not prevent importing under -W error.
        pass


_warn_experimental()
