"""What the observer puts on a wire, read back off real in-memory exporters.

The point of these is not that the methods run — the base class already answers every event
with nothing — but that a record carries the facts a security question is asked in, and that
the ones a guest chose stay off it until a host says otherwise.
"""

from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass

import pytest
from maf_sandbox import (
    BackendDeclarations,
    Capability,
    DisposalFailure,
    Egress,
    HostToolCalled,
    Identity,
    Isolation,
    IsolationScope,
    LandedOutput,
    OutputsCollected,
    SandboxAcquired,
    SandboxDisposed,
    SandboxKey,
    SandboxSpec,
    SourceIntegrity,
    StoreFileRead,
    ToolCallEnded,
    TransferLimits,
)
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter, SimpleLogRecordProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import (
    HistogramDataPoint,
    InMemoryMetricReader,
    NumberDataPoint,
)
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from maf_sandbox_otel import (
    NAMESPACE,
    OpenTelemetrySandboxObserver,
    hashed_conversation,
    hashed_key,
)

KEY = SandboxKey(scope="tenant-a", thread_id="thread-1", agent_dir="agent", call_id="call-9")
SPEC = SandboxSpec(
    kind="execute_code",
    image="python:3.12",
    egress=Egress.ALLOWLIST,
    egress_allow=("pypi.org", "files.pythonhosted.org"),
    requires=frozenset({Capability.EXEC, Capability.FILES_IN}),
)
DECLARATIONS = BackendDeclarations(
    capabilities=frozenset({Capability.EXEC}),
    egress_modes=frozenset({Egress.ALLOWLIST, Egress.CLOSED}),
)


@dataclass
class Recorded:
    """The three wires, read back."""

    observer: OpenTelemetrySandboxObserver
    spans: InMemorySpanExporter
    logs: InMemoryLogRecordExporter
    metrics: InMemoryMetricReader
    tracer_provider: TracerProvider

    def span_names(self) -> list[str]:
        return [span.name for span in self.spans.get_finished_spans()]

    def only_span(self):
        finished = self.spans.get_finished_spans()
        assert len(finished) == 1, f"expected one span, got {[s.name for s in finished]}"
        return finished[0]

    def attributes(self) -> dict[str, object]:
        return dict(self.only_span().attributes or {})

    def log_bodies(self) -> list[object]:
        return [record.log_record.body for record in self.logs.get_finished_logs()]

    def counter(self, name: str) -> float:
        total = 0.0
        data = self.metrics.get_metrics_data()
        for resource in data.resource_metrics if data else []:
            for scope in resource.scope_metrics:
                for metric in scope.metrics:
                    if metric.name != name:
                        continue
                    for point in metric.data.data_points:
                        if isinstance(point, NumberDataPoint):
                            total += point.value
                        elif isinstance(point, HistogramDataPoint):
                            total += point.sum
        return total


def build(*, sensitive: bool = False) -> Recorded:
    spans = InMemorySpanExporter()
    tracer_provider = TracerProvider()
    tracer_provider.add_span_processor(SimpleSpanProcessor(spans))
    logs = InMemoryLogRecordExporter()
    logger_provider = LoggerProvider()
    logger_provider.add_log_record_processor(SimpleLogRecordProcessor(logs))
    metrics = InMemoryMetricReader()
    return Recorded(
        observer=OpenTelemetrySandboxObserver(
            tracer_provider=tracer_provider,
            logger_provider=logger_provider,
            meter_provider=MeterProvider(metric_readers=[metrics]),
            record_sensitive_data=sensitive,
        ),
        spans=spans,
        logs=logs,
        metrics=metrics,
        tracer_provider=tracer_provider,
    )


def an_acquire(*, refusal: str | None = None) -> SandboxAcquired:
    return SandboxAcquired(
        key=KEY,
        spec=SPEC,
        isolation_scope=IsolationScope.CONVERSATION,
        backend=None if refusal else "docker",
        isolation=None if refusal else Isolation.CONTAINER,
        declarations=None if refusal else DECLARATIONS,
        seconds=0.25,
        refusal=refusal,
    )


def a_host_tool_call(**overrides: object) -> HostToolCalled:
    fields: dict[str, object] = {
        "run_id": "run-1",
        "key": KEY,
        "tool": "post_comment",
        "declared": True,
        "source": SourceIntegrity.UNTRUSTED,
        "sink": "internal",
        "identity": Identity.APP,
        "outcome": "delivered",
        "refusal": None,
        "response_bytes": 512,
        "calls": 3,
        "seconds": 0.01,
    }
    fields.update(overrides)
    return HostToolCalled(**fields)  # pyright: ignore[reportArgumentType]


class TestTheAcquireRecordCarriesThePosture:
    def test_the_egress_mode_and_its_allowlist_are_recorded(self):
        recorded = build()
        recorded.observer.sandbox_acquired(an_acquire())
        attributes = recorded.attributes()
        assert attributes[f"{NAMESPACE}.egress.mode"] == "allowlist"
        assert attributes[f"{NAMESPACE}.egress.allow"] == ("files.pythonhosted.org", "pypi.org")
        assert attributes[f"{NAMESPACE}.egress.allow_count"] == 2

    def test_the_backend_and_its_declarations_are_recorded(self):
        recorded = build()
        recorded.observer.sandbox_acquired(an_acquire())
        attributes = recorded.attributes()
        assert attributes[f"{NAMESPACE}.sandbox.backend"] == "docker"
        assert attributes[f"{NAMESPACE}.sandbox.isolation"] == "container"
        assert attributes[f"{NAMESPACE}.backend.egress_modes"] == ("allowlist", "closed")

    def test_a_refusal_is_an_error_span_naming_the_class(self):
        recorded = build()
        recorded.observer.sandbox_acquired(an_acquire(refusal="SandboxEgressNotEnforced"))
        span = recorded.only_span()
        assert span.status.status_code is StatusCode.ERROR
        assert (span.attributes or {})[f"{NAMESPACE}.refusal"] == "SandboxEgressNotEnforced"

    def test_a_refusal_before_a_backend_was_chosen_records_no_backend(self):
        recorded = build()
        recorded.observer.sandbox_acquired(an_acquire(refusal="NoSandboxBackend"))
        assert f"{NAMESPACE}.sandbox.backend" not in recorded.attributes()


class TestTheSpanIsWrittenAfterTheFactAndStillNests:
    def test_the_span_carries_the_events_own_duration(self):
        recorded = build()
        recorded.observer.sandbox_acquired(an_acquire())
        span = recorded.only_span()
        assert span.end_time is not None and span.start_time is not None
        assert span.end_time - span.start_time == pytest.approx(0.25e9, rel=0.01)

    def test_it_parents_to_whatever_span_is_current(self):
        recorded = build()
        tracer = recorded.tracer_provider.get_tracer("test")
        with tracer.start_as_current_span("execute_tool bicep_validate") as parent:
            recorded.observer.sandbox_acquired(an_acquire())
            expected = parent.get_span_context().span_id
        emitted = next(
            s for s in recorded.spans.get_finished_spans() if s.name == "sandbox.acquire"
        )
        assert emitted.parent is not None and emitted.parent.span_id == expected

    def test_a_record_from_a_worker_thread_still_nests_under_the_calls_span(self):
        """A tool body that awaits nothing is served on a worker thread, and its record is
        delivered there — so this is the one event whose parent could have been lost.

        The framework's dispatch is reproduced rather than described: `AIFunction._invoke_function`
        runs `asyncio.to_thread(self.__call__, ...)` inside the `execute_tool` span, and
        `to_thread` copies the context, which is what carries the current span across. The
        assertion that the body really ran off the loop thread is what stops this passing
        vacuously if that dispatch ever becomes an inline call.
        """
        recorded = build()
        tracer = recorded.tracer_provider.get_tracer("test")
        ran_on: dict[str, threading.Thread] = {}

        def a_body_that_awaits_nothing() -> None:
            ran_on["body"] = threading.current_thread()
            recorded.observer.tool_call_ended(
                ToolCallEnded(
                    tool="widget_run",
                    kind="test",
                    keys=(),
                    seconds=0.1,
                    failure=None,
                    unclean=0,
                )
            )

        async def as_the_framework_runs_it() -> int:
            ran_on["loop"] = threading.current_thread()
            with tracer.start_as_current_span("execute_tool widget_run") as parent:
                await asyncio.to_thread(a_body_that_awaits_nothing)
                return parent.get_span_context().span_id

        expected = asyncio.run(as_the_framework_runs_it())

        assert ran_on["body"] is not ran_on["loop"]
        emitted = next(s for s in recorded.spans.get_finished_spans() if s.name == "sandbox.call")
        assert emitted.parent is not None and emitted.parent.span_id == expected

    def test_a_zero_duration_event_does_not_invert_the_span(self):
        recorded = build()
        recorded.observer.tool_call_ended(
            ToolCallEnded(
                tool="execute_code",
                kind="codeact",
                keys=(KEY,),
                seconds=0.0,
                failure=None,
                unclean=0,
            )
        )
        span = recorded.only_span()
        assert span.end_time is not None and span.start_time is not None
        assert span.end_time >= span.start_time


class TestEveryEventReachesTheLogPipeline:
    def test_each_event_emits_exactly_one_log_record(self):
        recorded = build()
        observer = recorded.observer
        observer.sandbox_acquired(an_acquire())
        observer.host_tool_called(a_host_tool_call())
        observer.store_file_read(
            StoreFileRead(
                key=KEY,
                tool="execute_code",
                name="report.csv",
                integrity=SourceIntegrity.TRUSTED,
                characters=120,
                outcome="read",
            )
        )
        observer.outputs_collected(
            OutputsCollected(
                key=KEY,
                kind="codeact",
                declared=2,
                limits=TransferLimits(max_bytes_per_file=8, max_total_bytes=32, max_files=64),
                landed=(LandedOutput(name="out.png", size_bytes=2048, media_type="image/png"),),
                seconds=0.05,
            )
        )
        observer.sandbox_disposed(
            SandboxDisposed(key=KEY, backend="docker", outcome="gone", failure=None, seconds=0.1)
        )
        observer.tool_call_ended(
            ToolCallEnded(
                tool="execute_code",
                kind="codeact",
                keys=(KEY,),
                seconds=1.5,
                failure=None,
                unclean=0,
            )
        )
        assert recorded.log_bodies() == [
            "sandbox.acquire",
            "sandbox.host_tool_call",
            "sandbox.files_in",
            "sandbox.files_out",
            "sandbox.dispose",
            "sandbox.call",
        ]

    def test_a_store_read_draws_no_span_of_its_own(self):
        """It has no duration, so it is a fact on the call's span rather than a span."""
        recorded = build()
        recorded.observer.store_file_read(
            StoreFileRead(
                key=KEY,
                tool="execute_code",
                name="report.csv",
                integrity=None,
                characters=10,
                outcome="read",
            )
        )
        assert recorded.span_names() == []
        assert recorded.log_bodies() == ["sandbox.files_in"]

    def test_a_store_read_lands_on_the_call_span_when_one_is_recording(self):
        recorded = build()
        tracer = recorded.tracer_provider.get_tracer("test")
        with tracer.start_as_current_span("execute_tool execute_code"):
            recorded.observer.store_file_read(
                StoreFileRead(
                    key=KEY,
                    tool="execute_code",
                    name="report.csv",
                    integrity=SourceIntegrity.UNTRUSTED,
                    characters=10,
                    outcome="read",
                )
            )
        parent = recorded.only_span()
        assert [event.name for event in parent.events] == ["sandbox.files_in"]


class TestContentStaysOffTheWireUntilAHostAsks:
    def test_a_model_chosen_artifact_name_is_omitted_by_default(self):
        recorded = build()
        recorded.observer.outputs_collected(
            OutputsCollected(
                key=KEY,
                kind="codeact",
                declared=1,
                limits=TransferLimits(max_bytes_per_file=8, max_total_bytes=32, max_files=64),
                landed=(
                    LandedOutput(name="PWNED_INSTRUCTIONS.png", size_bytes=9, media_type=None),
                ),
                seconds=0.01,
            )
        )
        attributes = recorded.attributes()
        assert f"{NAMESPACE}.outputs.names" not in attributes
        assert attributes[f"{NAMESPACE}.outputs.landed"] == 1
        assert attributes[f"{NAMESPACE}.outputs.landed_bytes"] == 9

    def test_a_landing_records_the_call_its_folder_is_named_for(self):
        """`per_call=True` lands a call's artifacts in a folder named by this id."""
        recorded = build()
        recorded.observer.outputs_collected(
            OutputsCollected(
                key=KEY,
                kind="codeact",
                declared=1,
                limits=TransferLimits(max_bytes_per_file=8, max_total_bytes=32, max_files=64),
                landed=(LandedOutput(name="out.png", size_bytes=9, media_type=None),),
                seconds=0.01,
                call_id="call-77",
            )
        )
        assert recorded.attributes()[f"{NAMESPACE}.sandbox.call_id"] == "call-77"

    def test_it_crosses_when_the_host_asked(self):
        recorded = build(sensitive=True)
        recorded.observer.outputs_collected(
            OutputsCollected(
                key=KEY,
                kind="codeact",
                declared=1,
                limits=TransferLimits(max_bytes_per_file=8, max_total_bytes=32, max_files=64),
                landed=(LandedOutput(name="out.png", size_bytes=9, media_type=None),),
                seconds=0.01,
            )
        )
        assert recorded.attributes()[f"{NAMESPACE}.outputs.names"] == ("out.png",)

    def test_a_host_tool_refusal_sentence_is_omitted_by_default(self):
        recorded = build()
        recorded.observer.host_tool_called(
            a_host_tool_call(outcome="refused", refusal="no tool named 'wget' is registered")
        )
        attributes = recorded.attributes()
        assert f"{NAMESPACE}.host_tool.refusal" not in attributes
        assert attributes[f"{NAMESPACE}.host_tool.outcome"] == "refused"

    def test_the_key_is_hashed_by_default_and_its_parts_withheld(self):
        recorded = build()
        recorded.observer.sandbox_acquired(an_acquire())
        attributes = recorded.attributes()
        assert attributes[f"{NAMESPACE}.sandbox.key"] == hashed_key(KEY)
        assert "tenant-a" not in str(attributes)
        assert f"{NAMESPACE}.sandbox.thread_id" not in attributes

    def test_the_call_id_is_not_held_back_with_the_rest_of_the_key(self):
        """It names nobody, and it is what joins a record to the folder a sink landed in."""
        recorded = build()
        recorded.observer.sandbox_acquired(an_acquire())
        assert recorded.attributes()[f"{NAMESPACE}.sandbox.call_id"] == "call-9"

    def test_the_parts_join_the_hash_when_the_host_asked(self):
        recorded = build(sensitive=True)
        recorded.observer.sandbox_acquired(an_acquire())
        attributes = recorded.attributes()
        assert attributes[f"{NAMESPACE}.sandbox.scope"] == "tenant-a"
        assert attributes[f"{NAMESPACE}.sandbox.key"] == hashed_key(KEY)


class TestTheCasesARecorderGetsWrong:
    def test_a_cancelled_call_is_an_error_span_rather_than_a_missing_one(self):
        """The event is emitted from a `finally`, so a cancelled call still produces one."""
        recorded = build()
        recorded.observer.tool_call_ended(
            ToolCallEnded(
                tool="execute_code",
                kind="codeact",
                keys=(KEY,),
                seconds=4.0,
                failure="CancelledError",
                unclean=1,
            )
        )
        span = recorded.only_span()
        assert span.status.status_code is StatusCode.ERROR
        attributes = dict(span.attributes or {})
        assert attributes[f"{NAMESPACE}.call.failure"] == "CancelledError"
        assert attributes[f"{NAMESPACE}.call.unclean"] == 1

    def test_a_call_that_asked_for_two_sandboxes_records_both_keys(self):
        """One key renders like every other event's so the ordinary call stays queryable the
        same way; two render as a list, since naming one would hide the other."""
        other = SandboxKey(scope=KEY.scope, thread_id=KEY.thread_id, agent_dir="agent-2")
        recorded = build()
        recorded.observer.tool_call_ended(
            ToolCallEnded(
                tool="execute_code",
                kind="codeact",
                keys=(KEY, other),
                seconds=1.0,
                failure=None,
                unclean=0,
            )
        )
        assert recorded.attributes()[f"{NAMESPACE}.sandbox.key"] == (
            hashed_key(KEY),
            hashed_key(other),
        )

    def test_a_refusals_status_description_is_the_class_name_and_holds_no_message(self):
        """`refusal` is the exception's class name only — there is no message to reach for."""
        recorded = build()
        recorded.observer.sandbox_acquired(an_acquire(refusal="SandboxEgressNotEnforced"))
        assert recorded.only_span().status.description == "SandboxEgressNotEnforced"

    def test_an_unnamed_tool_is_not_read_as_a_bogus_ask(self):
        """The cap refusal fires before the name is read, so a good tool records none either."""
        recorded = build()
        recorded.observer.host_tool_called(
            a_host_tool_call(tool=None, outcome="refused", refusal="this run has spent its calls")
        )
        attributes = recorded.attributes()
        assert f"{NAMESPACE}.host_tool.name" not in attributes
        assert attributes[f"{NAMESPACE}.host_tool.outcome"] == "refused"
        assert recorded.counter(f"{NAMESPACE}.host_tool.calls") == 1


class TestTheHashIsAJoinColumn:
    def test_it_is_stable(self):
        assert hashed_key(KEY) == hashed_key(KEY)

    @pytest.mark.parametrize("boundary", ["\x1f", "|", ":"], ids=["unit-sep", "pipe", "colon"])
    def test_two_keys_that_could_render_alike_still_differ(self, boundary):
        """Including the encoding's own characters: nothing stops a scope holding one."""
        first = SandboxKey(scope="a", thread_id=f"b{boundary}c", agent_dir="d", call_id="")
        second = SandboxKey(scope=f"a{boundary}b", thread_id="c", agent_dir="d", call_id="")
        assert hashed_key(first) != hashed_key(second)

    def test_a_conversation_groups_across_calls_where_the_key_does_not(self):
        """A per-call workload puts a fresh `call_id` in every key, so the key's own hash
        differs per call — and grouping a conversation's records is the query this exists for,
        with the scope and thread redacted by default."""
        first = SandboxKey(scope="t", thread_id="th", agent_dir="a", call_id="call-1")
        second = SandboxKey(scope="t", thread_id="th", agent_dir="a", call_id="call-2")
        assert hashed_key(first) != hashed_key(second)
        assert hashed_conversation(first) == hashed_conversation(second)

    def test_a_multi_key_call_keeps_every_part_the_single_key_case_keeps(self):
        """The list branch is where a redaction guarantee is easiest to drop silently."""
        other = SandboxKey(scope="t2", thread_id="th2", agent_dir="a2", call_id="call-2")
        recorded = build(sensitive=True)
        recorded.observer.tool_call_ended(
            ToolCallEnded(
                tool="execute_code",
                kind="codeact",
                keys=(KEY, other),
                seconds=1.0,
                failure=None,
                unclean=0,
            )
        )
        attributes = recorded.attributes()
        assert attributes[f"{NAMESPACE}.sandbox.call_id"] == ("call-9", "call-2")
        assert attributes[f"{NAMESPACE}.sandbox.scope"] == ("tenant-a", "t2")
        assert attributes[f"{NAMESPACE}.sandbox.conversation"] == (
            hashed_conversation(KEY),
            hashed_conversation(other),
        )


class TestTheCountersAnswerTheAggregateQuestions:
    def test_bytes_delivered_inward_are_counted(self):
        recorded = build()
        recorded.observer.host_tool_called(a_host_tool_call(response_bytes=512))
        recorded.observer.host_tool_called(a_host_tool_call(response_bytes=88))
        assert recorded.counter(f"{NAMESPACE}.host_tool.response_bytes") == 600
        assert recorded.counter(f"{NAMESPACE}.host_tool.calls") == 2

    def test_a_refused_acquire_is_counted_beside_a_served_one(self):
        recorded = build()
        recorded.observer.sandbox_acquired(an_acquire())
        recorded.observer.sandbox_acquired(an_acquire(refusal="SandboxBackendNotPermitted"))
        assert recorded.counter(f"{NAMESPACE}.sandbox.acquires") == 2

    def test_the_call_duration_is_recorded(self):
        recorded = build()
        recorded.observer.tool_call_ended(
            ToolCallEnded(
                tool="execute_code",
                kind="codeact",
                keys=(KEY,),
                seconds=2.0,
                failure=None,
                unclean=0,
            )
        )
        assert recorded.counter(f"{NAMESPACE}.call.duration") == 2.0


class TestADisposalRecordsItsOutcome:
    def test_a_failure_names_its_code_and_marks_the_span(self):
        recorded = build()
        recorded.observer.sandbox_disposed(
            SandboxDisposed(
                key=KEY,
                backend="acas",
                outcome="may_remain",
                failure=DisposalFailure(code="timeout", detail="the control plane did not answer"),
                seconds=30.0,
            )
        )
        span = recorded.only_span()
        assert span.status.status_code is StatusCode.ERROR
        attributes = dict(span.attributes or {})
        assert attributes[f"{NAMESPACE}.disposal.code"] == "timeout"
        assert attributes[f"{NAMESPACE}.disposal.outcome"] == "may_remain"

    def test_a_disposal_fans_out_and_each_backend_is_its_own_record(self):
        """One `dispose(key)` reaches every registered backend, and each answers for itself."""
        recorded = build()
        for backend in ("docker", "acas", "wslc"):
            recorded.observer.sandbox_disposed(
                SandboxDisposed(key=KEY, backend=backend, outcome="gone", failure=None, seconds=0.1)
            )
        assert recorded.span_names() == ["sandbox.dispose"] * 3
        assert recorded.counter(f"{NAMESPACE}.sandbox.disposals") == 3

    def test_the_backends_own_sentence_is_host_vocabulary_and_waits_for_the_switch(self):
        failure = DisposalFailure(code="unreachable", detail="https://an-endpoint.example refused")
        default = build()
        default.observer.sandbox_disposed(
            SandboxDisposed(
                key=KEY, backend="acas", outcome="may_remain", failure=failure, seconds=1.0
            )
        )
        assert f"{NAMESPACE}.disposal.detail" not in default.attributes()

        asked = build(sensitive=True)
        asked.observer.sandbox_disposed(
            SandboxDisposed(
                key=KEY, backend="acas", outcome="may_remain", failure=failure, seconds=1.0
            )
        )
        assert "an-endpoint.example" in str(asked.attributes()[f"{NAMESPACE}.disposal.detail"])
