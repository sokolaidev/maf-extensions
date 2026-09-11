"""What a host records when a cleanup fails — and the one record `maf-sandbox-otel` cannot make.

`maf-sandbox-otel` is an observer on the router: it turns what the *library* did into spans, log
records and metrics. A reclaim that did not happen is not one of those things. The framework
reports it to the **host**, through `ReclaimConfig(on_failure=...)`, because what to do about it
is a host decision — so the record is the host's to make too.

**What the package records when a reclaim fails today, and why it is not enough.** Two spans
come out, and neither says a removal failed:

* `sandbox.call` carries `maf_sandbox.call.unclean`, which counts what a *transport* noted about
  processes it could not prove it stopped. A failed directory removal does not reach it. The
  attribute reads `0`.
* `sandbox.dispose` records the disposal the framework escalated to. It is not even the only one:
  the router cleans an instance it has not served before, inside `acquire` and ahead of the body,
  so a call whose reclaim then fails emits **two** disposal records carrying the same call id,
  both `outcome=gone`, and nothing on either says which was the remedy. Under
  `FailedReclaimPolicy.KEEP` there is one, and it is the adoption — the cleanup that did not
  happen leaves no record here at all.

So from the exported telemetry alone, a call whose cleanup failed and a call that cleaned up
perfectly look the same. The three facts that separate them are exactly the three
`ReclaimFailure` carries and no event does: **which path**, **why**, and **what the framework did
about it**.

**Where each attribute goes, and why they are not all in one namespace.** The join columns are
the package's, taken from its public `Redaction` and its documented `maf_sandbox.call.id` — put
them anywhere else and the record does not group with the call and the disposal it is about. The
three new facts go under this host's own namespace, because `maf_sandbox.*` belongs to the
package: a host writing new names into it is squatting on a namespace a later version may define
differently, and the collision would be silent.

Replace `app` below with your own service's namespace and this file is ready to copy.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from maf_sandbox_otel import NAMESPACE, OpenTelemetrySandboxObserver, Redaction
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import Status, StatusCode

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence

    from maf_sandbox import ReclaimFailure
    from opentelemetry.sdk.trace import ReadableSpan

#: This sample standing in for your own service's namespace. Not `maf_sandbox` — see the module
#: docstring for why a host does not write new names into a namespace it does not own.
APP = "app"

#: The record the package does not make. Named in this host's own namespace for the same reason
#: its attributes are, and emitted through a tracer whose instrumentation scope is this host —
#: so a pipeline can tell it from the package's records by where it came from, not only by name.
RECLAIM_FAILURE_SPAN = f"{APP}.sandbox.reclaim_failure"

#: What the framework did about the failure before this handler was called: `disposed`, `kept`
#: or `failed`. The first thing a host branches on, so it is a low-cardinality attribute rather
#: than a phrase inside the reason.
DISPOSAL = f"{APP}.reclaim.disposal"
#: The path that was not removed. Host-side infrastructure detail, so it crosses on the same
#: switch the package holds its own detail behind.
PATH = f"{APP}.reclaim.path"
#: This stack's own sentence for why. A log line, never something to parse — and it quotes the
#: engine, so it crosses on the same switch.
REASON = f"{APP}.reclaim.reason"

#: The package's own column for which tool call a record came from, documented in its README as
#: the one every record carries. Spelled from the exported `NAMESPACE` rather than hardcoded.
CALL_ID = f"{NAMESPACE}.call.id"

#: Two more of the package's columns, which this sample only ever **reads** — off the records it
#: made, to show what they do and do not say about a failed cleanup. `UNCLEAN` counts processes a
#: transport could not prove it stopped, and reads `0` beside a directory that was not removed.
UNCLEAN = f"{NAMESPACE}.call.unclean"
DISPOSAL_OUTCOME = f"{NAMESPACE}.disposal.outcome"

#: What `ReclaimFailure.path` holds when the call's body never asked for a path of its own.
#: There is no call directory to name, so there is no call id in it either.
_WHOLE_BASE = "."

INSTRUMENTATION_SCOPE = "purge-lifecycle-sample"


@dataclass(frozen=True)
class Telemetry:
    """One tracer provider, wired to both halves, with its exporter kept for reading back."""

    #: Hand to `SandboxRouter(observer=...)`. Its logger and meter providers are left to the
    #: globals, which are no-ops here: this sample reads spans, and asking for three exporters
    #: to read one would say the signals are coupled when they are deliberately not.
    observer: OpenTelemetrySandboxObserver
    #: What the handler writes through, under this host's own instrumentation scope.
    on_reclaim_failure: Callable[[ReclaimFailure], Awaitable[None]]
    #: Where both of them land, so the sample can print what a collector would have received.
    exporter: InMemorySpanExporter


def build_telemetry(*, record_sensitive_data: bool = False) -> Telemetry:
    """Wire the observer and the handler onto one in-memory tracer provider.

    An in-memory exporter because a sample has to *show* what was recorded; a deployment swaps
    it for an OTLP one and changes nothing else. `record_sensitive_data` is the package's own
    switch, and this handler honours it rather than inventing a second one — a deployment that
    decided its pipeline may hold host-identifying strings decided it for all of them.
    """
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    observer = OpenTelemetrySandboxObserver(
        tracer_provider=provider, record_sensitive_data=record_sensitive_data
    )
    tracer = provider.get_tracer(INSTRUMENTATION_SCOPE)
    redaction = Redaction(sensitive=record_sensitive_data)

    async def on_reclaim_failure(failure: ReclaimFailure) -> None:
        """Record one cleanup the framework could not complete.

        **It records before it does anything else that could fail.** `sandboxed_tool` runs this
        inside an `except Exception` that logs and continues, so a handler raising on its way to
        the recording — a closed stream, a value that will not encode — loses the record and
        leaves the run looking clean. Whatever else a host does here goes after this line.

        Nothing is awaited: the OpenTelemetry API takes the span and returns, and this runs on
        the call's own way out, under the reclaim timeout.
        """
        attributes = {
            **redaction.key(failure.key),
            DISPOSAL: failure.disposal,
            **redaction.text(REASON, failure.reason),
            **redaction.text(PATH, failure.path),
        }
        if failure.path != _WHOLE_BASE:
            # The call's directory is named for the call, so this is also what the package
            # stamped on `sandbox.call` and on the disposal it escalated to. The join is the
            # point of the record: on its own it says a path was left, and beside those two it
            # says which call left it and what the engine did next. `check_live_purge_sample.py`
            # asserts the three values match, so a framework that stopped naming the directory
            # after the call would fail this sample rather than quietly stop correlating.
            attributes[CALL_ID] = failure.path

        span = tracer.start_span(RECLAIM_FAILURE_SPAN)
        span.set_attributes(attributes)
        # An error, even where `disposal` says the framework contained it: the containment is
        # what kept the data from the next call, not evidence that nothing went wrong.
        span.set_status(Status(StatusCode.ERROR, failure.disposal))
        span.end()

    return Telemetry(observer=observer, on_reclaim_failure=on_reclaim_failure, exporter=exporter)


def exported(exporter: InMemorySpanExporter, name: str) -> tuple[ReadableSpan, ...]:
    """Every span of ``name`` a collector would have received by now, in the order they ended."""
    return tuple(span for span in exporter.get_finished_spans() if span.name == name)


def for_call(exporter: InMemorySpanExporter, name: str, call_id: str) -> tuple[ReadableSpan, ...]:
    """Every ``name`` span belonging to one call, oldest first.

    Selected on the call id rather than taken as the newest of its name, because "the last
    disposal anyone recorded" would hand back a different call's. Plural rather than one, because
    **a call can produce more than one disposal record and does**: the router cleans an instance
    it has not served before, inside `acquire` and ahead of the body, so a call whose reclaim
    then fails records that adoption and the escalation alike — identically.
    """
    return tuple(
        span
        for span in exporter.get_finished_spans()
        if span.name == name and (span.attributes or {}).get(CALL_ID) == call_id
    )


def outcomes(spans: Sequence[ReadableSpan], name: str) -> str:
    """The distinct values of ``name`` across ``spans``, for a line that reports a set.

    A set rather than a list: what the sample is showing is that these records do not differ,
    so collapsing them is the claim. One value printed beside a count of two says it exactly.
    """
    distinct = sorted({attribute(span, name) for span in spans})
    return ", ".join(distinct) if distinct else "(none)"


def attribute(span: ReadableSpan | None, name: str) -> str:
    """One attribute as text; ``"(no record)"`` for a span that was never made, ``"(absent)"``
    for one that was made without it.

    The two are different answers and the sample prints them as such: no disposal record means
    nothing was disposed, while a disposal record missing its outcome would be a defect.
    """
    if span is None:
        return "(no record)"
    value = (span.attributes or {}).get(name)
    return "(absent)" if value is None else str(value)
