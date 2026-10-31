"""Offline antivirus scans with authenticated host-supplied database updates."""

import warnings as _warnings

from ._tool import ANTIVIRUS_KIND, SCAN_FILE_TOOL_NAME, antivirus_sandbox_spec, make_antivirus_tools
from ._updates import (
    DatabaseIdentity,
    DatabaseUpdate,
    DatabaseUpdateProvider,
    UpdateFile,
    directory_update_provider,
)

__all__ = [
    "ANTIVIRUS_KIND",
    "SCAN_FILE_TOOL_NAME",
    "antivirus_sandbox_spec",
    "make_antivirus_tools",
    "DatabaseIdentity",
    "DatabaseUpdate",
    "DatabaseUpdateProvider",
    "UpdateFile",
    "directory_update_provider",
    "MafSandboxAntivirusExperimentalWarning",
]


class MafSandboxAntivirusExperimentalWarning(UserWarning):
    """Warning category for the experimental antivirus kind."""


try:
    _warnings.warn(
        "maf_sandbox_antivirus is experimental and may change or be removed without notice.",
        category=MafSandboxAntivirusExperimentalWarning,
        stacklevel=2,
    )
except MafSandboxAntivirusExperimentalWarning:
    # The pre-1.0 notice must not prevent imports under -W error.
    pass
