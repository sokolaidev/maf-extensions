"""The CodeAct sandbox: ``execute_code`` as a MAF tool.

A sibling of the Bicep kind rather than a variant of it: both are written against
:mod:`maf_sandbox`'s protocol alone, and neither knows which backend answers.  Where that one
runs a fixed compiler over files an agent authored, this one runs a program the model just
wrote, and returns what it printed.

Nothing in this subpackage imports Azure, a backend, or a host application: it asks a
:class:`~maf_sandbox.SandboxRouter` for a sandbox and gets back ``write_file`` and ``exec``.
"""

from __future__ import annotations

from ._tool import (
    CODEACT_KIND,
    EXECUTE_CODE_TOOL_NAME,
    codeact_sandbox_spec,
    make_codeact_tools,
)

__all__ = [
    "CODEACT_KIND",
    "EXECUTE_CODE_TOOL_NAME",
    "MafSandboxCodeactExperimentalWarning",
    "codeact_sandbox_spec",
    "make_codeact_tools",
]

# --- Experimental-package notice ---------------------------------------------------------
# This package is early-stage ("Development Status :: 4 - Beta"). Mirrors `agent_framework`'s
# own experimental-feature idiom (see its `_feature_stage` module and `ExperimentalWarning`, a
# `FutureWarning` subclass) but deliberately subclasses `UserWarning` instead: a host that runs
# under `python -W error` (many CI/production launchers do) would have importing this package
# alone raise before any of its own code runs if the category were a `FutureWarning`.
# `UserWarning` keeps the notice informational-by-default while staying a real, catchable,
# filterwarnings-suppressible category — see the try/except below for how `-W error` is
# handled anyway.
#
# Duplicated (not imported from a shared module) in each of the maf-sandbox* packages on
# purpose — a shared warnings module would be a cross-package dependency this split is
# designed to avoid.
import warnings as _warnings


class MafSandboxCodeactExperimentalWarning(UserWarning):
    """Warning category for maf-sandbox-codeact's experimental-package notice."""


def _warn_experimental() -> None:
    message = (
        "maf_sandbox_codeact is experimental and may change or be removed in future versions "
        "without notice."
    )
    try:
        _warnings.warn(message, category=MafSandboxCodeactExperimentalWarning, stacklevel=2)
    except MafSandboxCodeactExperimentalWarning:
        # A host running under `python -W error` (or with a blanket `filterwarnings("error")`
        # active) turns the warning above into an exception at the call site. Importing a
        # package must never fail because of an informational notice, so it is swallowed here
        # — this is the one piece of state a `-W error` host is allowed to change: whether the
        # notice was printed, never whether the import succeeded.
        pass


_warn_experimental()
