"""The Bicep sandbox: ``bicep build`` / ``bicep lint`` as a MAF tool (issue #408).

The first sandbox kind of its own family — a GitHub Copilot agent and an Azure CLI surface
are the obvious next ones, and each arrives as a sibling package here rather than as changes
to this one.

Nothing in this subpackage imports Azure or knows what a sandbox is: it asks a
:class:`~maf_sandbox.SandboxRouter` for one and gets back ``write_file`` and ``exec``.
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
    "MafSandboxBicepExperimentalWarning",
    "RESTORE_FAILURE_RULES",
    "bicep_sandbox_spec",
    "count_restore_failures",
    "format_diagnostics",
    "make_bicep_tools",
    "parse_sarif",
    "safe_workspace_path",
]

# --- Experimental-package notice (issue #697) -------------------------------------------
# This package is early-stage (0.1.0, "Development Status :: 4 - Beta"). Mirrors
# `agent_framework`'s own experimental-feature idiom (see its `_feature_stage` module and
# `ExperimentalWarning`, a `FutureWarning` subclass) but deliberately subclasses
# `UserWarning` instead: a host that runs under `python -W error` (many CI/production
# launchers do) would have importing this package alone raise before any of its own code
# runs if the category were a `FutureWarning`. `UserWarning` keeps the notice
# informational-by-default while staying a real, catchable, filterwarnings-suppressible
# category — see the try/except immediately below for how `-W error` is handled anyway.
#
# Duplicated (not imported from a shared module) in each of the three maf-sandbox*
# packages on purpose — a shared warnings module would be a cross-package dependency this
# split is designed to avoid.
import warnings as _warnings


class MafSandboxBicepExperimentalWarning(UserWarning):
    """Warning category for maf-sandbox-bicep's experimental-package notice."""


def _warn_experimental() -> None:
    message = (
        "maf_sandbox_bicep is experimental and may change or be removed in future "
        "versions without notice."
    )
    try:
        _warnings.warn(message, category=MafSandboxBicepExperimentalWarning, stacklevel=2)
    except MafSandboxBicepExperimentalWarning:
        # A host running under `python -W error` (or with a blanket
        # `filterwarnings("error")` active) turns the warning above into an exception at
        # the call site. Importing a package must never fail because of an informational
        # notice, so it is swallowed here — this is the one piece of state a `-W error`
        # host is allowed to change: whether the notice was printed, never whether the
        # import succeeded.
        pass


_warn_experimental()
