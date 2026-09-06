"""OpenTelemetry records of what a sandbox did — egress posture, host-tool calls, files, disposal.

A host registers :class:`OpenTelemetrySandboxObserver` on its
:class:`~maf_sandbox.SandboxRouter` and its :class:`~maf_sandbox.HostToolRegistry`, and every
event the sandbox suite reports becomes a **log record**; every event that carries a duration
also becomes a **span**, and the countable ones a **metric**.  A store read has no duration of
its own, so it becomes a zero-duration point span instead of an interval one.  Nothing else
changes: the observer seam is a no-op until something is registered on it, and this package is
the only thing in the suite that depends on OpenTelemetry.

It imports :mod:`maf_sandbox` and the OpenTelemetry **API**, and nothing else — no backend, no
agent framework, and no SDK.  Which exporter runs, and whether one runs at all, is the
application's to decide, exactly as it is for the rest of its telemetry.
"""

from ._attributes import (
    NAMESPACE,
    Redaction,
    hashed_conversation,
    hashed_key,
    hashed_scoped_thread,
)
from ._observer import (
    ACQUIRE,
    CALL,
    DISPOSE,
    EGRESS,
    FILES_IN,
    FILES_OUT,
    HOST_TOOL_CALL,
    PURGE,
    OpenTelemetrySandboxObserver,
)

__all__ = [
    "ACQUIRE",
    "EGRESS",
    "CALL",
    "DISPOSE",
    "FILES_IN",
    "FILES_OUT",
    "HOST_TOOL_CALL",
    "NAMESPACE",
    "PURGE",
    "MafSandboxOtelExperimentalWarning",
    "OpenTelemetrySandboxObserver",
    "Redaction",
    "hashed_conversation",
    "hashed_key",
    "hashed_scoped_thread",
]

# Experimental package (Beta): importing it emits a UserWarning rather than a FutureWarning,
# so a host running under `python -W error` can still import it.
import warnings as _warnings


class MafSandboxOtelExperimentalWarning(UserWarning):
    """Warning category for maf-sandbox-otel's experimental-package notice."""


def _warn_experimental() -> None:
    message = (
        "maf_sandbox_otel is experimental and may change or be removed in future versions "
        "without notice."
    )
    try:
        _warnings.warn(message, category=MafSandboxOtelExperimentalWarning, stacklevel=2)
    except MafSandboxOtelExperimentalWarning:
        # Deliberate: under `-W error` an informational notice must not fail the import.
        pass


_warn_experimental()
