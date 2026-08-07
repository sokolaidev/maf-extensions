"""The Bicep sandbox: ``bicep build`` / ``bicep lint`` as a MAF tool (issue #408).

The first sandbox kind of its own family — a GitHub Copilot agent and an Azure CLI surface
are the obvious next ones, and each arrives as a sibling package here rather than as changes
to this one.

Nothing in this subpackage imports Azure or knows what a sandbox is: it asks a
:class:`~sandbox_router.SandboxRouter` for one and gets back ``write_file`` and ``exec``.
The companion artefacts — the container image and the registry that serves it — live at
``images/bicep-sandbox/`` and ``infra/bicep-sandbox/`` in the host repository.
"""

from __future__ import annotations

from ._paths import safe_workspace_path
from ._sarif import RESTORE_FAILURE_RULES, count_restore_failures, format_diagnostics, parse_sarif
from ._tool import (
    BICEP_KIND,
    BICEP_TOOL_NAMES,
    BICEP_VALIDATE_TOOL_NAME,
    bicep_sandbox_spec,
    make_bicep_tools,
)

__all__ = [
    "BICEP_KIND",
    "BICEP_TOOL_NAMES",
    "BICEP_VALIDATE_TOOL_NAME",
    "RESTORE_FAILURE_RULES",
    "bicep_sandbox_spec",
    "count_restore_failures",
    "format_diagnostics",
    "make_bicep_tools",
    "parse_sarif",
    "safe_workspace_path",
]
