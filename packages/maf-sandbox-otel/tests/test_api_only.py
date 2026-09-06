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

import pytest
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
    ScopeDisposed,
    SourceIntegrity,
    StoreFileRead,
    ToolCallEnded,
    TransferLimits,
)

# `EVENT_METHODS` is core's authoritative list of observer handlers and is not re-exported
# from `maf_sandbox` yet. Read from the private module rather than kept as a second copy
# here, because a copy that drifts is exactly the failure this test exists to catch.
from maf_sandbox._observer import EVENT_METHODS

from maf_sandbox_otel import (
    NAMESPACE,
    OpenTelemetrySandboxObserver,
    hashed_conversation,
    hashed_key,
)

KEY = SandboxKey(scope="tenant-a", thread_id="thread-1", agent_dir="agent")
LIMITS = TransferLimits(max_bytes_per_file=8, max_total_bytes=32, max_files=64)


def every_event() -> list[object]:
    """One of each, constructed with the fields core declares.

    This list catches a *changed* event — a renamed or retyped field fails to construct. It
    cannot catch an **added** one, because nothing here would mention it:
    `TestTheObserverCoversEveryEvent` is what does that.
    """
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
            outcome="may_remain",
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
            outcome="read",
        ),
        OutputsCollected(
            key=KEY,
            kind="codeact",
            declared=1,
            limits=LIMITS,
            landed=(LandedOutput(name="out.png", size_bytes=9, media_type="image/png"),),
            seconds=0.05,
        ),
        ScopeDisposed(
            scope="tenant-a",
            thread_id="thread-1",
            backend="docker",
            outcome="gone",
            disposed=2,
            failure=None,
            seconds=0.3,
        ),
        ToolCallEnded(
            tool="execute_code", kind="codeact", keys=(KEY,), seconds=1.0, failure=None, unclean=0
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

    def test_it_overrides_every_handler_core_declares(self):
        """`SandboxObserver` is a base class with no-op methods rather than a Protocol, so core
        can add an event without breaking an existing observer. That is the right trade for a
        host's own observer and the wrong one here: this package would inherit the no-op and
        silently drop the new event, with nothing failing — not the import, not the type check,
        not `every_event`, which cannot mention what it does not know about.

        Read off `EVENT_METHODS`, which is core's own list and what
        `refuse_an_unusable_observer` validates against, rather than a second copy that could
        disagree with it.
        """
        missing = [
            method
            for method in EVENT_METHODS
            if getattr(type(OpenTelemetrySandboxObserver()), method)
            is getattr(SandboxObserver, method)
        ]
        assert not missing, f"core declares handlers this observer leaves as no-ops: {missing}"

    def test_the_sensitive_switch_changes_nothing_about_that(self):
        observer = OpenTelemetrySandboxObserver(record_sensitive_data=True)
        for event in every_event():
            event.deliver_to(observer)  # pyright: ignore[reportAttributeAccessIssue]


class TestTheJoinColumn:
    def test_the_hash_is_stable_across_calls(self):
        assert hashed_key(KEY) == hashed_key(KEY)

    @pytest.mark.parametrize(
        "boundary", ["\x1f", "|", ":", ""], ids=["unit-sep", "pipe", "colon", "empty"]
    )
    def test_two_keys_that_could_render_alike_still_differ(self, boundary):
        """`SandboxKey` constrains none of its parts, so there is no character a part cannot
        hold — including whichever one an encoding reserves. Delimiter-like content in either
        field must therefore never merge two distinct keys into one name."""
        first = SandboxKey(scope="a", thread_id=f"b{boundary}c", agent_dir="d")
        second = SandboxKey(scope=f"a{boundary}b", thread_id="c", agent_dir="d")
        assert hashed_key(first) != hashed_key(second)

    def test_the_whole_digest_is_the_name(self):
        """A truncation is a collision budget spent against a key count this package does not
        choose. At 64 bits and a billion keys the birthday probability is a few percent, and a
        collision here does not blur a statistic — it merges two conversations' records under
        one join key, which is the reading the package exists to prevent. SHA-256 is 64 hex
        characters, and an OpenTelemetry string attribute has no length limit to trade off."""
        assert len(hashed_key(KEY)) == 64
        assert len(hashed_conversation(KEY)) == 64

    def test_the_namespace_every_attribute_hangs_off_is_pinned(self):
        """A rename here silently orphans every dashboard and alert built on it."""
        assert NAMESPACE == "maf_sandbox"
