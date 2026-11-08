"""Guarded document extraction and Markdown export in disposable sandboxes."""

import warnings as _warnings

from ._export import ExportOptions, document_export_sandbox_spec, export_pdf
from ._tool import (
    DOCUMENT_KIND,
    DOCUMENT_READ_TOOL_NAME,
    document_sandbox_spec,
    make_document_tools,
)

__all__ = [
    "DOCUMENT_KIND",
    "DOCUMENT_READ_TOOL_NAME",
    "ExportOptions",
    "MafSandboxDocumentExperimentalWarning",
    "document_sandbox_spec",
    "document_export_sandbox_spec",
    "export_pdf",
    "make_document_tools",
]


class MafSandboxDocumentExperimentalWarning(UserWarning):
    """Warning category for the experimental document kind."""


try:
    _warnings.warn(
        "maf_sandbox_document is experimental and may change or be removed without notice.",
        category=MafSandboxDocumentExperimentalWarning,
        stacklevel=2,
    )
except MafSandboxDocumentExperimentalWarning:
    # The pre-1.0 notice must not prevent imports under -W error.
    pass
