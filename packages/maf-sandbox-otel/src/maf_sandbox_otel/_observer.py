"""A :class:`~maf_sandbox.SandboxObserver` that records to OpenTelemetry.

Three signals come out of every event, because three different readers ask three different
questions.  A **log record** is emitted for all of them and is the one a security pipeline
keeps: it does not depend on anything else being instrumented, and it survives a trace sampler
that threw the span away.  A **span** is emitted for every event, carrying a duration where the
event has one, so a call's shape is visible beside the agent framework's own.  All three go
through the providers the constructor was given, so a host that routed these records somewhere
of its own gets all of them and the application's trace gets none.  And a handful of
**counters** answer the aggregate questions — how many sandboxes were served or refused, how
many host-tool calls and under what outcome, how many bytes a sink took — without anybody
reading a record at all.
Not *tunnels*: what a guest actually reached is not on this seam at all, and a counter named
for it would invite reading allowed egress as observed egress.

**Spans are written after the fact.**  An observer is told what happened once it has happened,
so each span is created with an explicit start time and ended immediately.  Its parent is
whatever span is current where the event is delivered — the framework's ``execute_tool`` span,
including for the one record that arrives on a worker thread, which the suite pins rather than
assumes.  That is what puts these records inside the call that caused them.

**And the events of one call are siblings rather than children of** ``sandbox.call``.  Every
event arrives after the work it describes, and the call's own event arrives last of all, so
there is no moment at which this package could open a parent for the others to nest under.
Buffering them until the call ended would create one — at the price of per-call state that a
cancellation or a lost final event would leak.  A flat set of spans under ``execute_tool``
answers the same questions and cannot leak; the ``sandbox.call`` span carries the total the
caller actually waited for.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Mapping

from maf_sandbox import (
    HostToolCalled,
    OutputsCollected,
    SandboxAcquired,
    SandboxDisposed,
    SandboxObserver,
    StoreFileRead,
    ToolCallEnded,
)
from opentelemetry._logs import Logger, LoggerProvider, SeverityNumber, get_logger_provider
from opentelemetry.metrics import Counter, Histogram, MeterProvider, get_meter_provider
from opentelemetry.trace import (
    Status,
    StatusCode,
    Tracer,
    TracerProvider,
    get_tracer_provider,
)
from opentelemetry.util.types import AttributeValue

from ._attributes import (
    BACKEND,
    BACKEND_CAPABILITIES,
    BACKEND_EGRESS_MODES,
    CALL_ID,
    CAPABILITIES,
    DISPOSAL_CODE,
    DISPOSAL_DETAIL,
    DISPOSAL_OUTCOME,
    DURATION,
    EGRESS_ALLOW,
    EGRESS_ALLOW_COUNT,
    EGRESS_MODE,
    FAILURE,
    HOST_TOOL_CALLS,
    HOST_TOOL_DECLARED,
    HOST_TOOL_IDENTITY,
    HOST_TOOL_NAME,
    HOST_TOOL_OUTCOME,
    HOST_TOOL_REFUSAL,
    HOST_TOOL_RESPONSE_BYTES,
    HOST_TOOL_RUN_ID,
    HOST_TOOL_SINK,
    HOST_TOOL_SOURCE,
    IMAGE,
    INSTRUMENTATION_SCOPE,
    ISOLATION,
    ISOLATION_SCOPE,
    KIND,
    NAMESPACE,
    OUTPUTS_DECLARED,
    OUTPUTS_LANDED,
    OUTPUTS_LANDED_BYTES,
    OUTPUTS_MAX_BYTES_PER_FILE,
    OUTPUTS_MAX_FILES,
    OUTPUTS_MAX_TOTAL_BYTES,
    OUTPUTS_NAMES,
    REFUSAL,
    STORE_CHARACTERS,
    STORE_FILE,
    STORE_INTEGRITY,
    STORE_OUTCOME,
    TOOL,
    UNCLEAN,
    Redaction,
    instrumentation_version,
    sorted_values,
    without_none,
)

ACQUIRE = "sandbox.acquire"
DISPOSE = "sandbox.dispose"
HOST_TOOL_CALL = "sandbox.host_tool_call"
FILES_IN = "sandbox.files_in"
FILES_OUT = "sandbox.files_out"
CALL = "sandbox.call"

_NANOSECONDS = 1_000_000_000
_logger = logging.getLogger(__name__)


class OpenTelemetrySandboxObserver(SandboxObserver):
    """Records what a sandbox did to OpenTelemetry, as spans, log records and counters.

    Register it on both host-policy objects — they are separate, and a host that wires only one
    records only that half::

        observer = OpenTelemetrySandboxObserver()
        router = SandboxRouter([backend], observer=observer)
        registry = HostToolRegistry(observer=observer)

    Each provider argument defaults to the global one, which puts these records beside the
    application's own traces.  Passing one sends them somewhere else instead — a pipeline with
    its own exporter, sampling and retention, which is usually what a security record wants: a
    SIEM does not want the application's trace sampling applied to it, and the application's
    trace store does not want a year of egress records.  The three are independent, so a
    deployment can split the logs out and leave the spans where they were.

    ``record_sensitive_data`` follows the agent framework's switch of the same name and is off
    by default.  With it off, every posture, count, size and label crosses and no guest-chosen
    name or sentence does; keys are hashed.  :mod:`maf_sandbox_otel._attributes` states the
    rule and its limits.
    """

    def __init__(
        self,
        *,
        tracer_provider: TracerProvider | None = None,
        logger_provider: LoggerProvider | None = None,
        meter_provider: MeterProvider | None = None,
        record_sensitive_data: bool = False,
    ) -> None:
        # The version rides with the scope on all three, so an operator can tell records from
        # two releases of this package apart — a 0.x telemetry schema will move.
        version = instrumentation_version()
        self._tracer: Tracer = (tracer_provider or get_tracer_provider()).get_tracer(
            INSTRUMENTATION_SCOPE, version
        )
        self._logger: Logger = (logger_provider or get_logger_provider()).get_logger(
            INSTRUMENTATION_SCOPE, version
        )
        meter = (meter_provider or get_meter_provider()).get_meter(INSTRUMENTATION_SCOPE, version)
        self._redaction = Redaction(sensitive=record_sensitive_data)

        self._acquires: Counter = meter.create_counter(
            f"{NAMESPACE}.sandbox.acquires", description="Sandboxes served or refused."
        )
        self._disposals: Counter = meter.create_counter(
            f"{NAMESPACE}.sandbox.disposals", description="Disposals answered by a backend."
        )
        self._host_tool_calls: Counter = meter.create_counter(
            f"{NAMESPACE}.host_tool.calls",
            description="Calls a guest made back into the host.",
        )
        self._host_tool_bytes: Counter = meter.create_counter(
            f"{NAMESPACE}.host_tool.response_bytes",
            unit="By",
            description="Bytes delivered inward as host-tool responses.",
        )
        self._store_reads: Counter = meter.create_counter(
            f"{NAMESPACE}.store.file_reads", description="Files read out of the host's store."
        )
        self._landed_files: Counter = meter.create_counter(
            f"{NAMESPACE}.outputs.landed_files", description="Artifacts a sink accepted."
        )
        self._landed_bytes: Counter = meter.create_counter(
            f"{NAMESPACE}.outputs.landed_bytes",
            unit="By",
            description="Bytes a sink accepted.",
        )
        self._call_duration: Histogram = meter.create_histogram(
            f"{NAMESPACE}.call.duration",
            unit="s",
            description="One sandboxed tool call, body and reclaim together.",
        )

    def sandbox_acquired(self, event: SandboxAcquired) -> None:
        """Record the posture a key was served under, or the refusal that stopped it."""
        spec = event.spec
        recorded: dict[str, AttributeValue] = {
            **self._redaction.key(event.key),
            **without_none(
                {
                    KIND: spec.kind,
                    BACKEND: event.backend,
                    IMAGE: spec.image,
                    ISOLATION: None if event.isolation is None else str(event.isolation),
                    ISOLATION_SCOPE: str(event.isolation_scope),
                    REFUSAL: event.refusal,
                }
            ),
            EGRESS_MODE: str(spec.egress),
            EGRESS_ALLOW: sorted_values(spec.egress_allow),
            EGRESS_ALLOW_COUNT: len(spec.egress_allow),
            CAPABILITIES: sorted_values(spec.requires),
        }
        if event.declarations is not None:
            recorded[BACKEND_CAPABILITIES] = sorted_values(event.declarations.capabilities)
            recorded[BACKEND_EGRESS_MODES] = sorted_values(event.declarations.egress_modes)

        self._emit(ACQUIRE, recorded, event.seconds, event.refusal)
        self._isolate(
            lambda: self._acquires.add(
                1,
                {
                    KIND: spec.kind,
                    BACKEND: event.backend or "",
                    EGRESS_MODE: str(spec.egress),
                    REFUSAL: event.refusal or "",
                },
            )
        )

    def sandbox_disposed(self, event: SandboxDisposed) -> None:
        """Record one backend's answer to one disposal."""
        failure = event.failure
        recorded: dict[str, AttributeValue] = {
            **self._redaction.key(event.key),
            BACKEND: event.backend,
            DISPOSAL_OUTCOME: event.outcome,
            **without_none({DISPOSAL_CODE: None if failure is None else str(failure.code)}),
            # The backend's own sentence, which is a log line rather than something to parse. It
            # names infrastructure — an endpoint, a container id — so it crosses with the rest of
            # the host's own vocabulary rather than by default.
            **self._redaction.text(DISPOSAL_DETAIL, None if failure is None else failure.detail),
        }
        self._emit(DISPOSE, recorded, event.seconds, None if failure is None else str(failure.code))
        self._isolate(
            lambda: self._disposals.add(
                1,
                {
                    BACKEND: event.backend,
                    DISPOSAL_OUTCOME: event.outcome,
                    DISPOSAL_CODE: "" if failure is None else str(failure.code),
                },
            )
        )

    def host_tool_called(self, event: HostToolCalled) -> None:
        """Record one call a guest program made back into the host."""
        recorded: dict[str, AttributeValue] = {
            **self._redaction.key(event.key),
            HOST_TOOL_RUN_ID: event.run_id,
            HOST_TOOL_DECLARED: event.declared,
            HOST_TOOL_OUTCOME: event.outcome,
            HOST_TOOL_RESPONSE_BYTES: event.response_bytes,
            HOST_TOOL_CALLS: event.calls,
            **without_none(
                {
                    HOST_TOOL_NAME: event.tool,
                    HOST_TOOL_SOURCE: None if event.source is None else str(event.source),
                    HOST_TOOL_SINK: event.sink,
                    HOST_TOOL_IDENTITY: (None if event.identity is None else str(event.identity)),
                }
            ),
            # Sanitized for a transcript, and still not purely the host's: the refusals that fire
            # before a name resolves quote a bounded copy of what the guest asked for.
            **self._redaction.text(HOST_TOOL_REFUSAL, event.refusal),
        }
        failed = event.outcome if event.outcome != "delivered" else None
        self._emit(HOST_TOOL_CALL, recorded, event.seconds, failed)

        # `tool` is a name the host registered, so it is bounded and safe to key a metric on; a
        # guest's own spelling never reaches one. An empty name is not a bogus ask, though: the
        # cap refusal fires before the name is read, so a refused call to a perfectly good tool
        # records none either, and `outcome` is what separates the two.
        measured: dict[str, AttributeValue] = {
            HOST_TOOL_NAME: event.tool or "",
            HOST_TOOL_OUTCOME: event.outcome,
            HOST_TOOL_DECLARED: event.declared,
            HOST_TOOL_IDENTITY: "" if event.identity is None else str(event.identity),
            HOST_TOOL_SINK: "" if event.sink is None else str(event.sink),
        }
        self._isolate(lambda: self._host_tool_calls.add(1, measured))
        if event.response_bytes:
            self._isolate(lambda: self._host_tool_bytes.add(event.response_bytes, measured))

    def store_file_read(self, event: StoreFileRead) -> None:
        """Record one file a call read out of the host's store, and what it was worth."""
        recorded: dict[str, AttributeValue] = {
            **self._redaction.key(event.key),
            TOOL: event.tool,
            STORE_CHARACTERS: event.characters,
            STORE_OUTCOME: event.outcome,
            **without_none(
                {STORE_INTEGRITY: None if event.integrity is None else str(event.integrity)}
            ),
            **self._redaction.text(STORE_FILE, event.name),
        }
        # The one event with no duration of its own, so its span is a single instant.
        refused = event.outcome == "refused"
        self._isolate(lambda: self._log(FILES_IN, recorded, failed=refused))
        self._isolate(
            lambda: self._point_span(FILES_IN, recorded, failure=event.outcome if refused else None)
        )
        self._isolate(
            lambda: self._store_reads.add(
                1,
                {
                    TOOL: event.tool,
                    STORE_INTEGRITY: "" if event.integrity is None else str(event.integrity),
                    STORE_OUTCOME: event.outcome,
                },
            )
        )

    def outputs_collected(self, event: OutputsCollected) -> None:
        """Record what a collection declared, what landed, and under what caps."""
        landed_bytes = sum(output.size_bytes for output in event.landed)
        recorded: dict[str, AttributeValue] = {
            **self._redaction.key(event.key),
            KIND: event.kind,
            OUTPUTS_DECLARED: event.declared,
            OUTPUTS_LANDED: len(event.landed),
            OUTPUTS_LANDED_BYTES: landed_bytes,
            OUTPUTS_MAX_FILES: event.limits.max_files,
            OUTPUTS_MAX_BYTES_PER_FILE: event.limits.max_bytes_per_file,
            OUTPUTS_MAX_TOTAL_BYTES: event.limits.max_total_bytes,
            # The key reaches the conversation and the call id reaches the folder a `per_call`
            # sink landed in, so a record of a landing wants both halves. It is the collection's
            # own rather than the key's, which carries one only for a per-call workload.
            **without_none({REFUSAL: event.refusal, CALL_ID: event.call_id}),
            # An artifact name is written by the model, and the suite measures it as a channel
            # of its own — a few hundred bytes of chosen text per call.
            **self._redaction.texts(OUTPUTS_NAMES, (output.name for output in event.landed)),
        }
        self._emit(FILES_OUT, recorded, event.seconds, event.refusal)
        measured: dict[str, AttributeValue] = {KIND: event.kind}
        if event.landed:
            self._isolate(lambda: self._landed_files.add(len(event.landed), measured))
            self._isolate(lambda: self._landed_bytes.add(landed_bytes, measured))

    def tool_call_ended(self, event: ToolCallEnded) -> None:
        """Record one sandboxed tool call, body and reclaim together."""
        recorded: dict[str, AttributeValue] = {
            # Every key the call asked for, served or refused — so this span joins to the
            # refused acquire's record as well as the served one. One key is the ordinary case
            # and is recorded the same way the other events record theirs.
            **self._redaction.keys(event.keys),
            TOOL: event.tool,
            KIND: event.kind,
            UNCLEAN: event.unclean,
            **without_none({FAILURE: event.failure}),
        }
        self._emit(CALL, recorded, event.seconds, event.failure)
        self._isolate(
            lambda: self._call_duration.record(
                event.seconds,
                {TOOL: event.tool, KIND: event.kind, FAILURE: event.failure or ""},
            )
        )

    def _emit(
        self,
        name: str,
        attributes: Mapping[str, AttributeValue],
        seconds: float,
        failure: str | None,
    ) -> None:
        """One span and one log record for an event that took time."""
        # The duration goes on both, not just the span. A log-only pipeline is the one that
        # survives trace sampling, and "how long did the disposal take" is a question it has to
        # be able to answer on its own.
        recorded = {**attributes, DURATION: seconds}
        self._isolate(lambda: self._span(name, recorded, seconds, failure))
        self._isolate(lambda: self._log(name, recorded, failed=failure is not None))

    def _isolate(self, write: Callable[[], None]) -> None:
        """Attempt one signal's write on its own, so its failure does not cost a sibling's.

        An event's span, log record and counters go through providers a host is free to route
        apart — a security pipeline's logger, the application's own tracer — and that
        independence is defeated if a failure in one costs the others their write.
        ``SystemExit`` and ``KeyboardInterrupt`` are the host's own control flow rather than a
        provider's failure, and are not caught here.
        """
        try:
            write()
        except Exception:  # noqa: BLE001 - a sibling signal must still be attempted
            _logger.warning("maf_sandbox_otel: a telemetry write failed", exc_info=True)

    def _span(
        self,
        name: str,
        attributes: Mapping[str, AttributeValue],
        seconds: float,
        failure: str | None,
    ) -> None:
        end = time.time_ns()
        # A negative duration would put the start after the end; a backend reads that as a
        # broken span rather than a fast one.
        start = end - max(int(seconds * _NANOSECONDS), 0)
        span = self._tracer.start_span(name, start_time=start, attributes=dict(attributes))
        if failure is not None:
            span.set_status(Status(StatusCode.ERROR, failure))
        span.end(end_time=end)

    def _log(self, name: str, attributes: Mapping[str, AttributeValue], *, failed: bool) -> None:
        self._logger.emit(
            body=name,
            event_name=name,
            attributes=dict(attributes),
            severity_number=SeverityNumber.WARN if failed else SeverityNumber.INFO,
            severity_text="WARN" if failed else "INFO",
        )

    def _point_span(
        self,
        name: str,
        attributes: Mapping[str, AttributeValue],
        *,
        failure: str | None = None,
    ) -> None:
        """A span for an event with no duration of its own: it starts and ends at one instant.

        Through this package's own tracer, like every other signal.  Hanging it off whatever
        span happened to be current would put it outside the provider a host chose for these
        records — so a host that routed them to a security pipeline would lose this one, and
        under ``record_sensitive_data`` the store file name would land on the application's
        trace instead, which is the opposite of what routing them away asked for.
        """
        at = time.time_ns()
        span = self._tracer.start_span(name, start_time=at, attributes=dict(attributes))
        if failure is not None:
            span.set_status(Status(StatusCode.ERROR, failure))
        span.end(end_time=at)
