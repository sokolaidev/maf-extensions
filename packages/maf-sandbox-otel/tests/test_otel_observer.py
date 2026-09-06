"""What the observer puts on a wire, read back off real in-memory exporters.

The point of these is not that the methods run — the base class already answers every event
with nothing — but that a record carries the facts a security question is asked in, and that
the ones a guest chose stay off it until a host says otherwise.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import threading
from dataclasses import dataclass

import pytest
from maf_sandbox import (
    BackendDeclarations,
    Capability,
    DisposalFailure,
    Egress,
    HostToolAggregate,
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
    ScopeDisposed,
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
    hashed_scoped_thread,
)

KEY = SandboxKey(scope="tenant-a", thread_id="thread-1", agent_dir="agent", call_id="call-9")
#: ``call`` as a keyword where the core under test has it, and nothing where it does not.
#:
#: This suite runs against the workspace core *and* against every published core the wheel's
#: range admits. ``ToolCallEnded.call`` is on no published core yet, so that second set is empty
#: today and the check falls back to the core this checkout builds — but a core cut before the
#: field lands would put one in it that lacks the field. Detected off the class rather than
#: compared by version, so neither ordering needs an edit here.
CALL: dict[str, str] = (
    {"call": "call-4b1e"}
    if "call" in {field.name for field in dataclasses.fields(ToolCallEnded)}
    else {}
)
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


def _surface(
    *,
    identities: frozenset[Identity],
    has_undeclared: bool,
    result_integrity: SourceIntegrity | None,
) -> HostToolAggregate:
    """A sealed registry's answer, built directly: what a registry folds to is core's business."""
    return HostToolAggregate(
        result_integrity=result_integrity,
        outbound_caps=frozenset(),
        identities=identities,
        requires_approval=Identity.USER in identities,
        has_undeclared=has_undeclared,
        response_limits=TransferLimits(1024, 4096, 4),
        max_host_tool_calls_per_run=8,
    )


WITH_HOST_TOOLS = dataclasses.replace(
    SPEC,
    requires=SPEC.requires | {Capability.HOST_TOOLS},
    host_tools=_surface(
        identities=frozenset({Identity.APP, Identity.USER}),
        has_undeclared=True,
        result_integrity=SourceIntegrity.UNTRUSTED,
    ),
)
A_SINK_ONLY_SURFACE = dataclasses.replace(
    SPEC,
    requires=SPEC.requires | {Capability.HOST_TOOLS},
    host_tools=_surface(
        identities=frozenset({Identity.APP}), has_undeclared=False, result_integrity=None
    ),
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

    def log_attributes(self, name: str) -> dict[str, object]:
        """The attributes of the one log record for `name`, which nothing else here reads.

        `attributes()` above reads the *span*, so a value that crosses on the log alone — or
        fails to — is invisible to every assertion written against it.
        """
        records = [r for r in self.logs.get_finished_logs() if r.log_record.event_name == name]
        assert len(records) == 1, f"expected one {name} log record, got {len(records)}"
        return dict(records[0].log_record.attributes or {})

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


def an_acquire(*, refusal: str | None = None, spec: SandboxSpec = SPEC) -> SandboxAcquired:
    return SandboxAcquired(
        key=KEY,
        spec=spec,
        isolation_scope=IsolationScope.CONVERSATION,
        backend=None if refusal else "docker",
        isolation=None if refusal else Isolation.CONTAINER,
        declarations=None if refusal else DECLARATIONS,
        seconds=0.25,
        refusal=refusal,
    )


def a_purge(**overrides: object) -> ScopeDisposed:
    fields: dict[str, object] = {
        "scope": KEY.scope,
        "thread_id": KEY.thread_id,
        "backend": "docker",
        "outcome": "gone",
        "disposed": 2,
        "failure": None,
        "seconds": 0.4,
    }
    fields.update(overrides)
    return ScopeDisposed(**fields)  # pyright: ignore[reportArgumentType]


def a_store_read(**overrides: object) -> StoreFileRead:
    fields: dict[str, object] = {
        "key": KEY,
        "tool": "execute_code",
        "name": "report.csv",
        "integrity": SourceIntegrity.UNTRUSTED,
        "characters": 10,
        "outcome": "read",
    }
    fields.update(overrides)
    return StoreFileRead(**fields)  # pyright: ignore[reportArgumentType]


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

    def test_the_sealed_host_tool_surface_is_recorded(self):
        """Under whose authority the run could act, answerable before any call is made."""
        recorded = build()
        recorded.observer.sandbox_acquired(an_acquire(spec=WITH_HOST_TOOLS))
        attributes = recorded.attributes()
        assert attributes[f"{NAMESPACE}.surface.identities"] == ("app", "user")
        assert attributes[f"{NAMESPACE}.surface.undeclared"] is True
        assert attributes[f"{NAMESPACE}.surface.call_cap"] == 8
        assert attributes[f"{NAMESPACE}.surface.result_integrity"] == "untrusted"

    def test_a_surface_with_no_integrity_opinion_records_none(self):
        """A registry of sink-only tools has no source to fold, which is not `untrusted`."""
        recorded = build()
        recorded.observer.sandbox_acquired(an_acquire(spec=A_SINK_ONLY_SURFACE))
        attributes = recorded.attributes()
        assert f"{NAMESPACE}.surface.result_integrity" not in attributes
        assert attributes[f"{NAMESPACE}.surface.undeclared"] is False

    def test_a_workload_with_no_registry_records_nothing_about_a_surface(self):
        """Absent and empty are different answers, and rendering them alike reports the wrong one."""
        recorded = build()
        recorded.observer.sandbox_acquired(an_acquire())
        attributes = recorded.attributes()
        assert not [name for name in attributes if name.startswith(f"{NAMESPACE}.surface.")]

    def test_the_surface_reaches_the_log_pipeline_too(self):
        recorded = build()
        recorded.observer.sandbox_acquired(an_acquire(spec=WITH_HOST_TOOLS))
        logged = recorded.log_attributes("sandbox.acquire")
        assert logged[f"{NAMESPACE}.surface.identities"] == ("app", "user")


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
                    **CALL,
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
                **CALL,
            )
        )
        span = recorded.only_span()
        assert span.end_time is not None and span.start_time is not None
        assert span.end_time >= span.start_time


class TestEveryEventReachesTheLogPipeline:
    def test_a_duration_bearing_record_carries_its_duration_on_the_log_too(self):
        """The log pipeline is the one that outlives sampling, so it cannot need the span.

        A span keeps its duration in `start_time`/`end_time` whatever the attributes say, so
        dropping `duration` from the log alone stays invisible to every span assertion
        — and takes "how long did the disposal take" with it once the trace is sampled away.
        """
        recorded = build()
        recorded.observer.sandbox_acquired(an_acquire())
        assert recorded.log_attributes("sandbox.acquire")[f"{NAMESPACE}.duration"] == 0.25
        assert recorded.attributes()[f"{NAMESPACE}.duration"] == 0.25

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
        observer.scope_disposed(a_purge())
        observer.tool_call_ended(
            ToolCallEnded(
                tool="execute_code",
                kind="codeact",
                keys=(KEY,),
                seconds=1.5,
                failure=None,
                unclean=0,
                **CALL,
            )
        )
        assert recorded.log_bodies() == [
            "sandbox.acquire",
            "sandbox.host_tool_call",
            "sandbox.files_in",
            "sandbox.files_out",
            "sandbox.dispose",
            "sandbox.purge",
            "sandbox.call",
        ]

    def test_a_store_read_is_a_span_of_no_duration(self):
        """It has no duration of its own, so its span is one instant rather than an interval."""
        recorded = build()
        recorded.observer.store_file_read(a_store_read())
        span = recorded.only_span()
        assert span.name == "sandbox.files_in"
        assert span.start_time == span.end_time
        assert recorded.log_bodies() == ["sandbox.files_in"]

    def test_a_refused_store_read_is_an_error_span_too(self):
        """A refusal produces an error span here just as it does for every other event."""
        recorded = build()
        recorded.observer.store_file_read(a_store_read(outcome="refused"))
        span = recorded.only_span()
        assert span.status.status_code is StatusCode.ERROR

    def test_a_store_read_goes_to_this_packages_provider_not_the_ambient_span(self):
        """The record a host routed away must not also land on the application's trace.

        This is the event with no duration, and hanging it off `get_current_span()` would put
        it outside the provider the constructor was given — losing it for a host that routed
        these records to a security pipeline, and, under `record_sensitive_data`, writing the
        store file name onto the application's span instead. Both providers are read here, so
        the assertion fails whichever way the record goes astray.
        """
        recorded = build(sensitive=True)
        application = InMemorySpanExporter()
        application_provider = TracerProvider()
        application_provider.add_span_processor(SimpleSpanProcessor(application))

        with application_provider.get_tracer("app").start_as_current_span("execute_tool"):
            recorded.observer.store_file_read(a_store_read())

        ambient = next(s for s in application.get_finished_spans() if s.name == "execute_tool")
        assert [event.name for event in ambient.events] == []
        assert "report.csv" not in str(dict(ambient.attributes or {}))
        assert recorded.span_names() == ["sandbox.files_in"]
        assert recorded.attributes()[f"{NAMESPACE}.store.file"] == "report.csv"


class TestContentStaysOffTheWireUntilAHostAsks:
    def test_the_store_file_name_is_redacted_on_the_log_as_well_as_the_span(self):
        """Both signals, because a pipeline may keep only one of them.

        The rest of this class reads span attributes. A log record carries the same dictionary
        and reaches a security pipeline when a sampler has thrown the span away, so a redaction
        that held on one and not the other would leak with the whole suite green.
        """
        default = build()
        default.observer.store_file_read(a_store_read())
        assert f"{NAMESPACE}.store.file" not in default.attributes()
        assert f"{NAMESPACE}.store.file" not in default.log_attributes("sandbox.files_in")

        asked = build(sensitive=True)
        asked.observer.store_file_read(a_store_read())
        assert asked.attributes()[f"{NAMESPACE}.store.file"] == "report.csv"
        assert asked.log_attributes("sandbox.files_in")[f"{NAMESPACE}.store.file"] == "report.csv"

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
                **CALL,
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
                **CALL,
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

    def test_a_lone_surrogate_in_a_key_still_hashes(self):
        """A key part is an unvalidated host string, and `json` yields this for `"\\ud800"`.

        Plain UTF-8 refuses to encode one. Core contains an observer's failure, so raising here
        would drop the whole record while the call reported success — a telemetry package
        failing in the one direction nobody would notice. Two different surrogates still have to
        give two different names, or the fix would trade a loud failure for a silent join.
        """
        lone = json.loads('"\\ud800"')
        assert lone == "\ud800"
        first = SandboxKey(scope="tenant-a", thread_id=lone, agent_dir="agent")
        second = SandboxKey(scope="tenant-a", thread_id="\udfff", agent_dir="agent")
        assert len(hashed_key(first)) == 64
        assert hashed_key(first) != hashed_key(second)
        assert hashed_conversation(first) != hashed_conversation(second)

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
                **CALL,
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
                **CALL,
            )
        )
        assert recorded.counter(f"{NAMESPACE}.call.duration") == 2.0


class _BrokenTracer:
    """A tracer whose every span-open raises, standing in for a synchronous exporter that is."""

    def start_span(self, *args: object, **kwargs: object) -> None:
        raise RuntimeError("exporter is down")


class _BrokenTracerProvider:
    def get_tracer(self, *args: object, **kwargs: object) -> _BrokenTracer:
        return _BrokenTracer()


class _BrokenLogger:
    def emit(self, *args: object, **kwargs: object) -> None:
        raise RuntimeError("log pipeline is down")


class _BrokenLoggerProvider:
    def get_logger(self, *args: object, **kwargs: object) -> _BrokenLogger:
        return _BrokenLogger()


class _BrokenCounter:
    def add(self, *args: object, **kwargs: object) -> None:
        raise RuntimeError("meter is down")


class _BrokenMeter:
    def create_counter(self, *args: object, **kwargs: object) -> _BrokenCounter:
        return _BrokenCounter()

    def create_histogram(self, *args: object, **kwargs: object) -> _BrokenCounter:
        return _BrokenCounter()


class _BrokenMeterProvider:
    def get_meter(self, *args: object, **kwargs: object) -> _BrokenMeter:
        return _BrokenMeter()


class TestASignalsFailureDoesNotCostASibling:
    def test_a_broken_tracer_still_lets_the_log_and_the_counter_through(self):
        """Tracing is one of three independent providers; its failure must not skip the rest."""
        logs = InMemoryLogRecordExporter()
        logger_provider = LoggerProvider()
        logger_provider.add_log_record_processor(SimpleLogRecordProcessor(logs))
        metrics = InMemoryMetricReader()
        recorded = Recorded(
            observer=OpenTelemetrySandboxObserver(
                tracer_provider=_BrokenTracerProvider(),  # pyright: ignore[reportArgumentType]
                logger_provider=logger_provider,
                meter_provider=MeterProvider(metric_readers=[metrics]),
            ),
            spans=InMemorySpanExporter(),
            logs=logs,
            metrics=metrics,
            tracer_provider=TracerProvider(),
        )
        recorded.observer.sandbox_acquired(an_acquire())
        assert recorded.log_bodies() == ["sandbox.acquire"]
        assert recorded.counter(f"{NAMESPACE}.sandbox.acquires") == 1

    def test_a_broken_logger_still_lets_the_span_and_the_counter_through(self):
        """The log pipeline is one of three independent providers; its failure must not skip the rest."""
        spans = InMemorySpanExporter()
        tracer_provider = TracerProvider()
        tracer_provider.add_span_processor(SimpleSpanProcessor(spans))
        metrics = InMemoryMetricReader()
        recorded = Recorded(
            observer=OpenTelemetrySandboxObserver(
                tracer_provider=tracer_provider,
                logger_provider=_BrokenLoggerProvider(),  # pyright: ignore[reportArgumentType]
                meter_provider=MeterProvider(metric_readers=[metrics]),
            ),
            spans=spans,
            logs=InMemoryLogRecordExporter(),
            metrics=metrics,
            tracer_provider=tracer_provider,
        )
        recorded.observer.sandbox_acquired(an_acquire())
        assert recorded.only_span().name == "sandbox.acquire"
        assert recorded.counter(f"{NAMESPACE}.sandbox.acquires") == 1

    def test_a_broken_meter_still_lets_the_span_and_the_log_through(self):
        """The meter is one of three independent providers; its failure must not skip the rest."""
        spans = InMemorySpanExporter()
        tracer_provider = TracerProvider()
        tracer_provider.add_span_processor(SimpleSpanProcessor(spans))
        logs = InMemoryLogRecordExporter()
        logger_provider = LoggerProvider()
        logger_provider.add_log_record_processor(SimpleLogRecordProcessor(logs))
        recorded = Recorded(
            observer=OpenTelemetrySandboxObserver(
                tracer_provider=tracer_provider,
                logger_provider=logger_provider,
                meter_provider=_BrokenMeterProvider(),  # pyright: ignore[reportArgumentType]
            ),
            spans=spans,
            logs=logs,
            metrics=InMemoryMetricReader(),
            tracer_provider=tracer_provider,
        )
        recorded.observer.sandbox_acquired(an_acquire())
        assert recorded.only_span().name == "sandbox.acquire"
        assert recorded.log_bodies() == ["sandbox.acquire"]


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


class TestAScopePurgeRecordsTheConversationItCleanedUp:
    """The routine cleanup, and the one record with no key: a purge is answered with a count."""

    def test_it_joins_on_the_conversation_the_keyed_records_carry(self):
        """The whole value of the record is that it joins — a purge that hashed to something of
        its own would say a conversation was cleaned up and match none of its own sandboxes."""
        recorded = build()
        recorded.observer.scope_disposed(a_purge())
        attributes = recorded.attributes()
        assert attributes[f"{NAMESPACE}.sandbox.conversation"] == hashed_conversation(KEY)
        assert hashed_scoped_thread(KEY.scope, KEY.thread_id) == hashed_conversation(KEY)
        # No key, and none invented: a backend answers with a count, not with what it removed.
        assert f"{NAMESPACE}.sandbox.key" not in attributes

    def test_the_scope_and_thread_wait_for_the_switch_the_way_a_keys_parts_do(self):
        default = build()
        default.observer.scope_disposed(a_purge())
        assert f"{NAMESPACE}.sandbox.scope" not in default.attributes()

        asked = build(sensitive=True)
        asked.observer.scope_disposed(a_purge())
        attributes = asked.attributes()
        assert attributes[f"{NAMESPACE}.sandbox.scope"] == "tenant-a"
        assert attributes[f"{NAMESPACE}.sandbox.thread_id"] == "thread-1"

    def test_what_the_backend_removed_is_on_the_record_and_in_a_counter(self):
        recorded = build()
        recorded.observer.scope_disposed(a_purge(backend="docker", disposed=2))
        recorded.observer.scope_disposed(a_purge(backend="acas", disposed=3))
        assert recorded.span_names() == ["sandbox.purge"] * 2
        assert recorded.counter(f"{NAMESPACE}.scope.purges") == 2
        assert recorded.counter(f"{NAMESPACE}.scope.purged_sandboxes") == 5

    def test_a_partial_purge_names_its_code_and_still_reports_what_went(self):
        recorded = build()
        recorded.observer.scope_disposed(
            a_purge(
                outcome="may_remain",
                disposed=1,
                failure=DisposalFailure(code="timeout", detail="the control plane did not answer"),
            )
        )
        span = recorded.only_span()
        assert span.status.status_code is StatusCode.ERROR
        attributes = dict(span.attributes or {})
        assert attributes[f"{NAMESPACE}.purge.disposed"] == 1
        assert attributes[f"{NAMESPACE}.disposal.outcome"] == "may_remain"
        assert attributes[f"{NAMESPACE}.disposal.code"] == "timeout"

    def test_a_purge_that_never_answered_adds_nothing_to_the_sandboxes_removed(self):
        """Its nought is the absence of an answer, so a sum of that counter stays a count of
        sandboxes a backend reported removing rather than a guess about the ones it did not."""
        recorded = build()
        recorded.observer.scope_disposed(
            a_purge(
                outcome="unknown",
                disposed=0,
                failure=DisposalFailure(
                    code="unknown", detail="docker: the purge was interrupted by CancelledError"
                ),
            )
        )
        assert recorded.counter(f"{NAMESPACE}.scope.purges") == 1
        assert recorded.counter(f"{NAMESPACE}.scope.purged_sandboxes") == 0

    def test_the_backends_own_sentence_waits_for_the_switch(self):
        failure = DisposalFailure(code="unreachable", detail="https://an-endpoint.example refused")
        default = build()
        default.observer.scope_disposed(a_purge(outcome="may_remain", failure=failure))
        assert f"{NAMESPACE}.disposal.detail" not in default.attributes()

        asked = build(sensitive=True)
        asked.observer.scope_disposed(a_purge(outcome="may_remain", failure=failure))
        assert "an-endpoint.example" in str(asked.attributes()[f"{NAMESPACE}.disposal.detail"])
