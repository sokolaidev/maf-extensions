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

# Experimental package (Beta): importing it emits a UserWarning rather than a FutureWarning,
# so a host running under `python -W error` can still import it.
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
        # Deliberate: under `-W error` an informational notice must not fail the import.
        pass


_warn_experimental()
