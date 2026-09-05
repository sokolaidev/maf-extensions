"""OpenTelemetry records of what a sandbox did — egress posture, host-tool calls, files, disposal.

A host registers :class:`OpenTelemetrySandboxObserver` on its
:class:`~maf_sandbox.SandboxRouter` and its :class:`~maf_sandbox.HostToolRegistry`, and every
event the sandbox suite reports becomes a log record, a span and, where it is countable, a
metric.  Nothing else changes: the observer seam is a no-op until something is registered on
it, and this package is the only thing in the suite that depends on OpenTelemetry.

It imports :mod:`maf_sandbox` and the OpenTelemetry **API**, and nothing else — no backend, no
agent framework, and no SDK.  Which exporter runs, and whether one runs at all, is the
application's to decide, exactly as it is for the rest of its telemetry.
"""

from ._attributes import NAMESPACE, Redaction, hashed_key
from ._observer import (
    ACQUIRE,
    CALL,
    DISPOSE,
    FILES_IN,
    FILES_OUT,
    HOST_TOOL_CALL,
    OpenTelemetrySandboxObserver,
)

__all__ = [
    "ACQUIRE",
    "CALL",
    "DISPOSE",
    "FILES_IN",
    "FILES_OUT",
    "HOST_TOOL_CALL",
    "NAMESPACE",
    "OpenTelemetrySandboxObserver",
    "Redaction",
    "hashed_key",
]
