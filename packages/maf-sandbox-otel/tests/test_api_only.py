"""The recorder against the OpenTelemetry **API** alone, with no SDK and no provider configured.

This is the environment a host that has not configured telemetry is in, and the one the
published-cores gate builds: the wheel, its declared dependencies, and nothing else. What it
has to prove is that recording still costs nothing and raises nothing — the API's no-op
providers answer every call.

It imports no SDK, deliberately, so it runs in both environments. Reading back what the
recorder actually wrote needs exporters and lives in `test_otel_observer.py`, which is skipped
where there is no SDK to read with.
"""

from __future__ import annotations

from maf_sandbox import (
    DisposalFailure,
    Egress,
    HostToolCalled,
    Identity,
    IsolationScope,
    LandedOutput,
    OutputsCollected,
    SandboxAcquired,
    SandboxDisposed,
    SandboxKey,
    SandboxObserver,
    SandboxSpec,
    SourceIntegrity,
    StoreFileRead,
    ToolCallEnded,
    TransferLimits,
)

from maf_sandbox_otel import NAMESPACE, OpenTelemetrySandboxObserver, hashed_key

KEY = SandboxKey(scope="tenant-a", thread_id="thread-1", agent_dir="agent")
LIMITS = TransferLimits(max_bytes_per_file=8, max_total_bytes=32, max_files=64)


def every_event() -> list[object]:
    """One of each, so a new event type that nobody records here is a failing import."""
    return [
        SandboxAcquired(
            key=KEY,
            spec=SandboxSpec(kind="execute_code", egress=Egress.CLOSED),
            isolation_scope=IsolationScope.CONVERSATION,
            backend="docker",
            isolation=None,
            declarations=None,
            seconds=0.1,
        ),
        SandboxDisposed(
            key=KEY,
            backend="docker",
            landed=False,
            failure=DisposalFailure(code="timeout", detail="no answer"),
            seconds=0.2,
        ),
        HostToolCalled(
            run_id="run-1",
            key=KEY,
            tool="post_comment",
            declared=True,
            source=SourceIntegrity.UNTRUSTED,
            sink="internal",
            identity=Identity.APP,
            outcome="delivered",
            refusal=None,
            response_bytes=16,
            calls=1,
            seconds=0.01,
        ),
        StoreFileRead(
            key=KEY,
            tool="execute_code",
            name="report.csv",
            integrity=SourceIntegrity.TRUSTED,
            characters=10,
            refused=False,
        ),
        OutputsCollected(
            key=KEY,
            kind="codeact",
            declared=1,
            limits=LIMITS,
            landed=(LandedOutput(name="out.png", size_bytes=9, media_type="image/png"),),
            seconds=0.05,
        ),
        ToolCallEnded(
            tool="execute_code", kind="codeact", key=KEY, seconds=1.0, failure=None, unclean=0
        ),
    ]


class TestItRecordsWithoutAnSdk:
    def test_the_recorder_is_an_observer_a_host_can_register(self):
        assert issubclass(OpenTelemetrySandboxObserver, SandboxObserver)

    def test_it_constructs_against_the_global_no_op_providers(self):
        assert OpenTelemetrySandboxObserver() is not None

    def test_every_event_records_and_none_of_them_raises(self):
        observer = OpenTelemetrySandboxObserver()
        for event in every_event():
            event.deliver_to(observer)  # pyright: ignore[reportAttributeAccessIssue]

    def test_the_sensitive_switch_changes_nothing_about_that(self):
        observer = OpenTelemetrySandboxObserver(record_sensitive_data=True)
        for event in every_event():
            event.deliver_to(observer)  # pyright: ignore[reportAttributeAccessIssue]


class TestTheJoinColumn:
    def test_the_hash_is_stable_across_calls(self):
        assert hashed_key(KEY) == hashed_key(KEY)

    def test_two_keys_that_render_alike_still_differ(self):
        """The parts are joined with a separator none of them can hold."""
        first = SandboxKey(scope="a", thread_id="b|c", agent_dir="d")
        second = SandboxKey(scope="a|b", thread_id="c", agent_dir="d")
        assert hashed_key(first) != hashed_key(second)

    def test_the_namespace_every_attribute_hangs_off_is_pinned(self):
        """A rename here silently orphans every dashboard and alert built on it."""
        assert NAMESPACE == "maf_sandbox"
