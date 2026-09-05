"""Tests for the observer seam — what a host is told about what a sandbox did.

Two properties carry the whole thing and are pinned from several directions. **Every way out
of an instrumented site is recorded**, refusals and cancellations included, because a record
that only covers the happy path is exactly the blind spot this seam exists to close: an acquire
that was refused and a host-tool call that was cancelled are the two an operator goes looking
for. And **nothing an observer does can reach the call** — it is the host's code on the call's
own task, so its failure is a warning and never a tool result.
"""

from __future__ import annotations

import asyncio
import dataclasses
import inspect
import json
import logging

import pytest

from maf_sandbox import (
    Artifact,
    CallerContext,
    Capability,
    DeclaredOutput,
    DisposalFailure,
    Egress,
    FileStoreProvenance,
    HostToolCalled,
    HostToolRegistry,
    HostToolRun,
    Identity,
    Isolation,
    IsolationScope,
    LandedArtifact,
    LandedOutput,
    ListedFile,
    OutputDisposition,
    OutputsCollected,
    OutputSink,
    SandboxAcquired,
    SandboxCapabilityNotSupported,
    SandboxDisposed,
    SandboxEvent,
    SandboxKey,
    SandboxObserver,
    SandboxRouter,
    SandboxSpec,
    SandboxTransferCapExceeded,
    SandboxUnclean,
    SourceIntegrity,
    StoreFileRead,
    ToolCallEnded,
    TransferLimits,
    collect_outputs,
    sandbox_tool,
)
from maf_sandbox import _router as _router_module
from maf_sandbox._observer import EVENT_METHODS, record
from maf_sandbox.maf import SandboxToolSession, sandboxed_tool
from maf_sandbox.testing import (
    FAKE_BACKEND_DECLARATIONS,
    InMemoryStore,
    InProcessSandbox,
    InProcessSandboxBackend,
)

_KEY = SandboxKey(scope="scope-a", thread_id="thread-1", agent_dir="agent-1")
_SPEC = SandboxSpec(kind="test", egress=Egress.ALLOWLIST, egress_allow=("example.invalid",))
_LOG = logging.getLogger("test_observer")


class _Recorder(SandboxObserver):
    """Every event this seam emits, in order, with a switch for failing on one."""

    def __init__(self, fail: BaseException | None = None) -> None:
        self.events: list[SandboxEvent] = []
        self.fail = fail

    def _seen(self, event: SandboxEvent) -> None:
        self.events.append(event)
        if self.fail is not None:
            raise self.fail

    def sandbox_acquired(self, event: SandboxAcquired) -> None:
        self._seen(event)

    def sandbox_disposed(self, event: SandboxDisposed) -> None:
        self._seen(event)

    def host_tool_called(self, event: HostToolCalled) -> None:
        self._seen(event)

    def store_file_read(self, event: StoreFileRead) -> None:
        self._seen(event)

    def outputs_collected(self, event: OutputsCollected) -> None:
        self._seen(event)

    def tool_call_ended(self, event: ToolCallEnded) -> None:
        self._seen(event)

    def only(self, kind: type[SandboxEvent]) -> list:
        return [event for event in self.events if isinstance(event, kind)]

    def one(self, kind: type[SandboxEvent]):
        found = self.only(kind)
        assert len(found) == 1, f"expected exactly one {kind.__name__}, got {found}"
        return found[0]


def _context(scope: str = "scope-a", thread_id: str | None = "thread-1") -> CallerContext:
    return CallerContext(
        current_scope=lambda: scope,
        current_thread_id=lambda: thread_id,
        list_files=InMemoryStore.list,
    )


def _router(backend=None, **kw) -> SandboxRouter:
    return SandboxRouter(
        [backend if backend is not None else InProcessSandboxBackend()],
        min_isolation=Isolation.NONE,
        **kw,
    )


# ---------------------------------------------------------------------------
# The vocabulary
# ---------------------------------------------------------------------------


class TestTheEventVocabulary:
    """`EVENT_METHODS` is the one list, and every event routes to the method it names."""

    def test_it_names_every_public_method_on_the_observer_and_nothing_else(self):
        public = tuple(
            name
            for name, member in vars(SandboxObserver).items()
            if not name.startswith("_") and inspect.isfunction(member)
        )
        assert set(EVENT_METHODS) == set(public)
        assert len(EVENT_METHODS) == len(public)

    def test_every_event_class_delivers_to_exactly_one_of_them(self):
        reached: list[str] = []

        class _Naming(SandboxObserver):
            pass

        for name in EVENT_METHODS:
            setattr(_Naming, name, lambda self, event, _name=name: reached.append(_name))

        for event in _every_event():
            event.deliver_to(_Naming())

        assert sorted(reached) == sorted(EVENT_METHODS)

    def test_the_base_event_refuses_to_deliver_itself(self):
        """A subclass that forgets `deliver_to` must fail loudly rather than record nothing."""
        with pytest.raises(NotImplementedError):
            SandboxEvent().deliver_to(_Recorder())


def _every_event() -> list[SandboxEvent]:
    """One of each, so the routing and containment tests cannot miss a class."""
    return [
        SandboxAcquired(
            key=_KEY,
            spec=_SPEC,
            isolation_scope=IsolationScope.CONVERSATION,
            backend="in-process",
            isolation=Isolation.NONE,
            declarations=FAKE_BACKEND_DECLARATIONS,
            seconds=0.0,
        ),
        SandboxDisposed(key=_KEY, backend="in-process", landed=True, failure=None, seconds=0.0),
        HostToolCalled(
            run_id="run-1",
            key=_KEY,
            tool="add",
            declared=True,
            source=None,
            sink=None,
            identity=None,
            outcome="delivered",
            refusal=None,
            response_bytes=2,
            calls=1,
            seconds=0.0,
        ),
        StoreFileRead(
            key=_KEY,
            tool="widget_run",
            name="a.txt",
            integrity=None,
            characters=1,
            refused=False,
        ),
        OutputsCollected(
            key=_KEY,
            kind="test",
            declared=0,
            limits=_SPEC.files_out,
            landed=(),
            seconds=0.0,
        ),
        ToolCallEnded(
            tool="widget_run", kind="test", keys=(_KEY,), seconds=0.0, failure=None, unclean=0
        ),
    ]


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


class TestRegistration:
    """Both host-policy objects take an observer, and both refuse an unusable one."""

    def test_the_router_reads_back_what_was_registered(self):
        recorder = _Recorder()
        assert _router(observer=recorder).observer is recorder
        assert _router().observer is None

    def test_the_registry_reads_back_what_was_registered(self):
        recorder = _Recorder()
        assert HostToolRegistry(observer=recorder).observer is recorder
        assert HostToolRegistry().observer is None

    def test_the_session_reads_the_routers(self):
        recorder = _Recorder()
        session = SandboxToolSession(
            _router(observer=recorder),
            _context(),
            "agent-1",
            _SPEC,
            name="widget_run",
            logger=_LOG,
        )
        assert session.observer is recorder

    def test_a_group_of_containable_failures_is_contained(self):
        """A `BaseExceptionGroup` is not an `Exception`, so a tuple naming the leaf types alone
        lets one through — and an observer using a task group raises exactly that shape."""

        class _Grouped(SandboxObserver):
            def sandbox_acquired(self, event: SandboxAcquired) -> None:
                raise BaseExceptionGroup("observer", [asyncio.CancelledError()])

        router = _router(observer=_Grouped())

        assert asyncio.run(router.acquire(_KEY, _SPEC)) is not None

    def test_a_group_carrying_an_exit_still_escapes(self):
        """Unwrapped rather than trusted for being a group: `SystemExit` is the host's control
        flow whether it arrives alone or as a leaf."""

        class _Exits(SandboxObserver):
            def sandbox_acquired(self, event: SandboxAcquired) -> None:
                raise BaseExceptionGroup("observer", [SystemExit(2)])

        router = _router(observer=_Exits())

        with pytest.raises(BaseExceptionGroup):
            asyncio.run(router.acquire(_KEY, _SPEC))

    def test_an_observer_whose_failure_cannot_be_rendered_is_still_contained(self):
        """Containment runs `error_detail` on whatever the observer raised, and rendering an
        exception runs the exception author's own code. One that raises from `__str__` would
        otherwise escape the handler that exists to stop it and replace the call's outcome."""

        class _Unprintable(Exception):
            def __str__(self) -> str:
                # Not an `Exception`: containment that enumerated only that hierarchy would let
                # this walk out of the handler built to stop it.
                raise asyncio.CancelledError

        class _Raises(SandboxObserver):
            def sandbox_acquired(self, event: SandboxAcquired) -> None:
                raise _Unprintable

        router = _router(observer=_Raises())

        # The acquire answers normally: nothing the observer did reached the caller.
        sandbox = asyncio.run(router.acquire(_KEY, _SPEC))
        assert sandbox is not None

    @pytest.mark.parametrize("register", [_router, HostToolRegistry])
    def test_something_that_is_not_an_observer_is_refused_at_registration(self, register):
        class _Duck:
            def sandbox_acquired(self, event):
                pass

        with pytest.raises(TypeError, match="must be a SandboxObserver"):
            register(observer=_Duck())

    @pytest.mark.parametrize("register", [_router, HostToolRegistry])
    def test_an_async_callable_override_is_refused_too(self, register):
        """`inspect.iscoroutinefunction` is false for an instance whose `__call__` is async, so
        a check reading the attribute alone admits an observer whose every event is discarded
        as an unawaited coroutine. `maf._awaits` and the host-tool bracket already read both."""

        class _AsyncCallable:
            async def __call__(self, event: HostToolCalled) -> None:
                pass

        class _Wired(SandboxObserver):
            host_tool_called = _AsyncCallable()  # pyright: ignore[reportAssignmentType]

        with pytest.raises(TypeError, match="host_tool_called"):
            register(observer=_Wired())

    @pytest.mark.parametrize("register", [_router, HostToolRegistry])
    def test_a_coroutine_override_is_refused_at_registration(self, register):
        """Nothing awaits an observer, so an `async def` one would lose every event it saw."""

        class _Async(SandboxObserver):
            async def host_tool_called(self, event: HostToolCalled) -> None:
                pass

        with pytest.raises(TypeError, match="host_tool_called"):
            register(observer=_Async())


# ---------------------------------------------------------------------------
# Containment
# ---------------------------------------------------------------------------


class TestRecordContainsWhateverAnObserverDoes:
    """An observer is the host's code on the call's task. Its failure is a warning."""

    @pytest.mark.parametrize(
        "failure", [RuntimeError("boom"), asyncio.CancelledError(), GeneratorExit()]
    )
    def test_an_observers_failure_never_reaches_the_caller(self, failure, caplog):
        recorder = _Recorder(fail=failure)
        with caplog.at_level(logging.WARNING, logger="test_observer"):
            for event in _every_event():
                record(recorder, event, _LOG)
        assert len(recorder.events) == len(EVENT_METHODS)
        assert len(caplog.records) == len(EVENT_METHODS)
        assert "SandboxAcquired was not recorded" in caplog.text

    @pytest.mark.parametrize("failure", [SystemExit(), KeyboardInterrupt()])
    def test_the_hosts_own_control_flow_still_escapes(self, failure):
        recorder = _Recorder(fail=failure)
        with pytest.raises(type(failure)):
            record(recorder, _every_event()[0], _LOG)

    def test_no_observer_is_a_no_op(self):
        record(None, _every_event()[0], _LOG)


# ---------------------------------------------------------------------------
# Acquire and dispose
# ---------------------------------------------------------------------------


class TestAcquireIsRecorded:
    def test_the_recorded_labels_do_not_move_after_the_event_is_delivered(self):
        """`SandboxSpec` is frozen about its bindings, not about the dict `labels` is. A record
        that shared it would change after delivery — and let an observer write into the
        caller's spec through the event it was handed."""
        recorder = _Recorder()
        spec = SandboxSpec(kind="test", labels={"run": "first"})
        router = _router(observer=recorder)

        asyncio.run(router.acquire(_KEY, spec))
        spec.labels["run"] = "second"
        spec.labels["added"] = "later"

        assert recorder.one(SandboxAcquired).spec.labels == {"run": "first"}

    def test_an_observer_cannot_write_into_the_callers_spec_through_the_event(self):
        recorder = _Recorder()
        spec = SandboxSpec(kind="test", labels={"run": "first"})
        router = _router(observer=recorder)

        asyncio.run(router.acquire(_KEY, spec))
        recorder.one(SandboxAcquired).spec.labels["injected"] = "by the observer"

        assert spec.labels == {"run": "first"}

    def test_a_served_acquire_carries_the_posture_it_was_served_under(self):
        recorder = _Recorder()
        backend = InProcessSandboxBackend()
        router = _router(backend, observer=recorder)

        asyncio.run(router.acquire(_KEY, _SPEC))

        event = recorder.one(SandboxAcquired)
        assert (event.key, event.spec) == (_KEY, _SPEC)
        assert event.backend == "in-process"
        assert event.isolation is Isolation.NONE
        assert event.declarations == FAKE_BACKEND_DECLARATIONS
        assert event.isolation_scope is IsolationScope.CONVERSATION
        assert event.refusal is None
        assert event.seconds >= 0

    def test_the_scope_recorded_is_the_resolved_one_not_the_specs(self):
        """A host floor of CALL is not in the spec, and it is what the sandbox was served at."""
        recorder = _Recorder()
        backend = InProcessSandboxBackend(
            declarations=dataclasses.replace(
                FAKE_BACKEND_DECLARATIONS,
                isolation_scopes=frozenset({IsolationScope.CALL, IsolationScope.CONVERSATION}),
            ),
            sandbox_per_key=True,
        )
        router = _router(backend, observer=recorder, min_isolation_scope=IsolationScope.CALL)

        asyncio.run(router.acquire(dataclasses.replace(_KEY, call_id="call-1"), _SPEC))

        assert recorder.one(SandboxAcquired).isolation_scope is IsolationScope.CALL

    def test_a_refusal_before_a_backend_is_chosen_names_the_class_and_no_backend(self):
        recorder = _Recorder()
        router = _router(observer=recorder)
        spec = dataclasses.replace(_SPEC, requires=frozenset({Capability.SNAPSHOT}))

        with pytest.raises(SandboxCapabilityNotSupported):
            asyncio.run(router.acquire(_KEY, spec))

        event = recorder.one(SandboxAcquired)
        assert event.refusal == "SandboxCapabilityNotSupported"
        assert (event.backend, event.isolation, event.declarations) == (None, None, None)

    def test_a_refused_key_is_recorded_too(self):
        """The unclean ledger refuses before any backend is asked, and that is still an acquire."""
        recorder = _Recorder()
        router = _router(observer=recorder)
        router.mark_unclean(_KEY, DisposalFailure("refused", "still there"))

        with pytest.raises(SandboxUnclean):
            asyncio.run(router.acquire(_KEY, _SPEC))

        assert recorder.one(SandboxAcquired).refusal == "SandboxUnclean"

    def test_a_failure_inside_the_backend_still_names_the_backend_that_was_chosen(self):
        recorder = _Recorder()
        backend = InProcessSandboxBackend(acquire_error=RuntimeError("service unavailable"))
        router = _router(backend, observer=recorder)

        with pytest.raises(RuntimeError):
            asyncio.run(router.acquire(_KEY, _SPEC))

        event = recorder.one(SandboxAcquired)
        assert (event.refusal, event.backend) == ("RuntimeError", "in-process")

    def test_a_broken_declaration_read_does_not_fail_the_acquire_it_records(self):
        """The record is read after the create, and a second read must not replace a sandbox."""
        recorder = _Recorder()
        backend = _DeclarationsThatBreakAfterRouting()
        router = _router(backend, observer=recorder)

        assert asyncio.run(router.acquire(_KEY, _SPEC)) is backend.sandbox

        event = recorder.one(SandboxAcquired)
        assert event.backend == "in-process"
        assert (event.isolation, event.declarations) == (None, None)


class _DeclarationsThatBreakAfterRouting(InProcessSandboxBackend):
    """Answers every read the routing makes and then stops — what a record must survive."""

    def __init__(self) -> None:
        super().__init__()
        self._served = False

    @property
    def declarations(self):
        if self._served:
            raise RuntimeError("declarations are gone")
        return FAKE_BACKEND_DECLARATIONS

    async def acquire(self, key: SandboxKey, spec: SandboxSpec):
        sandbox = await super().acquire(key, spec)
        self._served = True
        return sandbox


class TestDisposalIsRecorded:
    def test_one_event_per_backend_asked(self):
        recorder = _Recorder()
        router = SandboxRouter(
            [InProcessSandboxBackend(name="first"), InProcessSandboxBackend(name="second")],
            min_isolation=Isolation.NONE,
            observer=recorder,
        )

        asyncio.run(router.dispose(_KEY))

        events = recorder.only(SandboxDisposed)
        assert [event.backend for event in events] == ["first", "second"]
        assert all(event.landed and event.failure is None for event in events)

    def test_a_disposal_a_cancel_took_is_still_recorded(self):
        """`CancelledError` is not an `Exception`, so an `except Exception` around the dispose
        drops exactly the record a timed-out disposal would have left — the one an operator
        most wants. The cancel still reaches the caller."""
        recorder = _Recorder()

        class _Cancels(InProcessSandboxBackend):
            async def dispose(self, key: SandboxKey) -> str | None:
                raise asyncio.CancelledError

        router = _router(_Cancels(name="cancels"), observer=recorder)

        with pytest.raises(asyncio.CancelledError):
            asyncio.run(router.dispose(_KEY))

        event = recorder.one(SandboxDisposed)
        assert (event.backend, event.landed) == ("cancels", False)
        assert event.failure is not None
        assert event.failure.code == "unknown"
        # The interruption is named rather than assumed to be a cancel: a disposal taken by an
        # interpreter shutting down is a different fact, and an audit reads this string.
        assert "interrupted by CancelledError" in event.failure.detail

    def test_a_disposal_an_exit_took_names_the_exit_rather_than_a_cancel(self):
        """The catch is wide enough to see an interpreter shutting down, so what took the
        disposal is named. Recording `SystemExit` as a cancel is a wrong answer to the question
        an audit asks the detail string."""
        recorder = _Recorder()

        class _Exits(InProcessSandboxBackend):
            async def dispose(self, key: SandboxKey) -> str | None:
                raise GeneratorExit

        router = _router(_Exits(name="exits"), observer=recorder)

        with pytest.raises(GeneratorExit):
            asyncio.run(router.dispose(_KEY))

        event = recorder.one(SandboxDisposed)
        assert event.failure is not None
        assert "interrupted by GeneratorExit" in event.failure.detail

    def test_a_backend_that_refuses_is_recorded_with_its_code(self):
        recorder = _Recorder()
        failure = DisposalFailure("refused", "the service said no")
        router = _router(InProcessSandboxBackend(dispose_failure=failure), observer=recorder)

        asyncio.run(router.dispose(_KEY))

        event = recorder.one(SandboxDisposed)
        assert event.landed is False
        # Named by the backend that answered, the way the ledger's own entry is.
        assert event.failure == DisposalFailure("refused", "in-process: the service said no")

    def test_each_backends_record_carries_its_own_answer_and_not_the_sweeps(self):
        """A sweep accumulates reasons; a record says what *this* backend answered."""
        recorder = _Recorder()
        router = SandboxRouter(
            [
                InProcessSandboxBackend(
                    name="first", dispose_failure=DisposalFailure("refused", "no")
                ),
                InProcessSandboxBackend(name="second"),
            ],
            min_isolation=Isolation.NONE,
            observer=recorder,
        )

        asyncio.run(router.dispose(_KEY))

        first, second = recorder.only(SandboxDisposed)
        assert (first.backend, first.landed) == ("first", False)
        assert (second.backend, second.landed, second.failure) == ("second", True, None)

    def test_a_backend_that_raises_is_recorded_as_unknown(self):
        recorder = _Recorder()
        router = _router(
            InProcessSandboxBackend(dispose_error=RuntimeError("boom")), observer=recorder
        )

        asyncio.run(router.dispose(_KEY))

        event = recorder.one(SandboxDisposed)
        assert event.landed is False
        assert event.failure is not None and event.failure.code == "unknown"

    def test_the_delete_that_answers_a_refused_acquire_is_recorded(self):
        """A sandbox nothing can reclaim is disposed on the way to the refusal, so it is a
        disposal like any other — and a record covering only the sweep would miss it."""
        recorder = _Recorder()
        router = _router(_UnreclaimableBackend(), observer=recorder)

        with pytest.raises(TypeError, match="reclaim"):
            asyncio.run(router.acquire(_KEY, _SPEC))

        assert recorder.one(SandboxAcquired).refusal == "TypeError"
        assert recorder.one(SandboxDisposed).backend == "in-process"


class _UnreclaimableBackend(InProcessSandboxBackend):
    """Hands back something without `Sandbox.reclaim`, which the router refuses and disposes."""

    async def acquire(self, key: SandboxKey, spec: SandboxSpec):
        return object()


# ---------------------------------------------------------------------------
# Host-tool calls
# ---------------------------------------------------------------------------


@sandbox_tool(source=SourceIntegrity.UNTRUSTED, sink="internal", identity=Identity.APP)
def _fetch(url: str) -> str:
    """A declared tool: an untrusted source and a sink, which is the pair a query asks about."""
    return f"fetched {url}"


def _undeclared(value: int) -> int:
    return value + 1


def _run(registry: HostToolRegistry, **kw) -> HostToolRun:
    return HostToolRun(registry, logger=_LOG, run_id="run-1", **kw)


class TestHostToolCallsAreRecorded:
    def test_a_delivered_call_carries_its_declaration_and_what_it_cost(self, recwarn):
        recorder = _Recorder()
        registry = HostToolRegistry(observer=recorder)
        registry.register(_fetch, name="fetch")

        result = asyncio.run(_run(registry, key=_KEY).call("fetch", {"url": "u"}))

        event = recorder.one(HostToolCalled)
        assert event.outcome == "delivered"
        assert (event.run_id, event.key, event.tool) == ("run-1", _KEY, "fetch")
        assert (event.declared, event.source, event.sink, event.identity) == (
            True,
            SourceIntegrity.UNTRUSTED,
            "internal",
            Identity.APP,
        )
        assert event.refusal is None
        assert result.value_json is not None
        assert event.response_bytes == len(result.value_json.encode("utf-8"))
        assert event.calls == 1

    def test_the_framing_a_transport_declares_is_in_the_recorded_size(self, recwarn):
        recorder = _Recorder()
        registry = HostToolRegistry(observer=recorder)
        registry.register(_fetch, name="fetch")

        result = asyncio.run(_run(registry).call("fetch", {"url": "u"}, framing_bytes=7))

        assert result.value_json is not None
        assert recorder.one(HostToolCalled).response_bytes == (
            len(result.value_json.encode("utf-8")) + 7
        )

    def test_two_overlapping_calls_each_report_only_their_own_bytes(self, recwarn):
        """Calls of one run may overlap, and the run's byte ledger is cumulative across them.

        Differencing that ledger across a call gives the one finishing second the other's bytes
        as well, so the size a query reads is the size of somebody else's response.
        """
        released = asyncio.Event()
        entered = asyncio.Event()

        async def slow(url: str) -> str:
            """Deliver only after a second call has already delivered."""
            entered.set()
            await released.wait()
            return "s" * 40

        def quick(url: str) -> str:
            """Deliver immediately, in the middle of `slow`."""
            return "q" * 5

        recorder = _Recorder()
        registry = HostToolRegistry(observer=recorder)
        registry.register(slow, name="slow")
        registry.register(quick, name="quick")

        async def both() -> None:
            run = _run(registry)
            overlapping = asyncio.create_task(run.call("slow", {"url": "u"}))
            await entered.wait()
            await run.call("quick", {"url": "u"})
            released.set()
            # Asserted rather than discarded: if the held call did not deliver, the sizes below
            # would be comparing against a refusal's zero and would pass for the wrong reason.
            assert (await overlapping).value_json is not None

        asyncio.run(both())

        sizes = {event.tool: event.response_bytes for event in recorder.only(HostToolCalled)}
        assert sizes["quick"] == len(json.dumps("q" * 5).encode("utf-8"))
        assert sizes["slow"] == len(json.dumps("s" * 40).encode("utf-8"))

    def test_an_unstamped_tool_is_recorded_as_declaring_nothing(self, recwarn):
        recorder = _Recorder()
        registry = HostToolRegistry(observer=recorder)
        registry.register(_undeclared, name="bump")

        asyncio.run(_run(registry).call("bump", {"value": 1}))

        event = recorder.one(HostToolCalled)
        assert (event.tool, event.declared) == ("bump", False)
        assert (event.source, event.sink, event.identity) == (None, None, None)

    def test_a_name_that_never_resolved_is_recorded_with_no_tool(self):
        recorder = _Recorder()

        asyncio.run(_run(HostToolRegistry(observer=recorder)).call("nope"))

        event = recorder.one(HostToolCalled)
        assert (event.tool, event.declared, event.outcome) == (None, False, "refused")
        assert event.refusal is not None and "not a registered host tool" in event.refusal
        assert event.response_bytes == 0

    def test_an_exhausted_cap_is_recorded_as_a_refusal_of_the_call_that_spent_it(self, recwarn):
        recorder = _Recorder()
        registry = HostToolRegistry(observer=recorder, max_host_tool_calls_per_run=1)
        registry.register(_fetch, name="fetch")
        run = _run(registry)

        async def twice() -> None:
            await run.call("fetch", {"url": "u"})
            await run.call("fetch", {"url": "u"})

        asyncio.run(twice())

        first, second = recorder.only(HostToolCalled)
        assert (first.outcome, first.calls) == ("delivered", 1)
        assert (second.outcome, second.tool, second.calls) == ("refused", None, 2)

    def test_a_cancelled_call_is_recorded_as_cancelled(self, recwarn):
        recorder = _Recorder()
        registry = HostToolRegistry(observer=recorder)

        @sandbox_tool(source=None, sink=None, identity=None)
        async def vanish() -> str:
            raise asyncio.CancelledError

        registry.register(vanish, name="vanish")

        with pytest.raises(asyncio.CancelledError):
            asyncio.run(_run(registry).call("vanish"))

        event = recorder.one(HostToolCalled)
        assert (event.outcome, event.tool, event.response_bytes) == ("cancelled", "vanish", 0)

    def test_a_transports_own_programming_error_is_not_a_call(self, recwarn):
        """The framing checks raise before the call begins, so nothing is recorded for one."""
        recorder = _Recorder()
        registry = HostToolRegistry(observer=recorder)
        registry.register(_fetch, name="fetch")

        with pytest.raises(ValueError, match="framing_bytes"):
            asyncio.run(_run(registry).call("fetch", {"url": "u"}, framing_bytes=-1))

        assert recorder.events == []

    def test_the_bracketing_observer_and_the_record_are_both_served(self, recwarn):
        """`host_tool_calls_observer` brackets the call; this says what the call was."""
        import contextlib

        bracketed: list[object] = []

        @contextlib.contextmanager
        def watching(run: HostToolRun, name: object):
            bracketed.append(name)
            yield

        recorder = _Recorder()
        registry = HostToolRegistry(observer=recorder, host_tool_calls_observer=watching)
        registry.register(_fetch, name="fetch")

        asyncio.run(_run(registry).call("fetch", {"url": "u"}))

        assert bracketed == ["fetch"]
        assert recorder.one(HostToolCalled).tool == "fetch"


# ---------------------------------------------------------------------------
# Outputs
# ---------------------------------------------------------------------------


class _Sink:
    def __init__(self, fail_on: int | None = None, *, per_call: bool = False) -> None:
        self.delivered: list[Artifact] = []
        self.sink = OutputSink(deliver=self.deliver, per_call=per_call)
        self._fail_on = fail_on

    async def deliver(self, artifact: Artifact) -> LandedArtifact:
        if self._fail_on is not None and len(self.delivered) == self._fail_on:
            raise RuntimeError("the store refused")
        self.delivered.append(artifact)
        return LandedArtifact(name=artifact.name, display=f"[{artifact.name}]")


def _outputs_spec(*outputs: DeclaredOutput, files_out: TransferLimits | None = None) -> SandboxSpec:
    return SandboxSpec(
        kind="diagram",
        work_dir="/w",
        declared_outputs=outputs,
        **({} if files_out is None else {"files_out": files_out}),
    )


class TestOutputsAreRecorded:
    def test_what_landed_is_recorded_with_its_size_and_media_type(self):
        recorder = _Recorder()
        sandbox = InProcessSandbox(seed_files={"/w/a.png": "12345", "/w/b.png": "67"})
        spec = _outputs_spec(
            DeclaredOutput(path="a.png", media_type="image/png"),
            DeclaredOutput(path="b.png", media_type="image/png", name="renamed.png"),
        )
        sink = _Sink()

        asyncio.run(collect_outputs(sandbox, spec, sink=sink.sink, observer=recorder, key=_KEY))

        event = recorder.one(OutputsCollected)
        assert (event.key, event.kind, event.declared) == (_KEY, "diagram", 2)
        assert event.limits == spec.files_out
        assert event.landed == (
            LandedOutput(name="a.png", size_bytes=5, media_type="image/png"),
            LandedOutput(name="renamed.png", size_bytes=2, media_type="image/png"),
        )
        assert event.refusal is None

    def test_the_call_id_a_sink_lands_under_is_recorded_beside_the_key(self):
        """`key` reaches the conversation; `call_id` reaches the folder the artifacts are in."""
        recorder = _Recorder()
        sandbox = InProcessSandbox(seed_files={"/w/a.png": "1"})
        spec = _outputs_spec(DeclaredOutput(path="a.png"))
        sink = _Sink(per_call=True)

        asyncio.run(
            collect_outputs(
                sandbox,
                spec,
                sink=sink.sink,
                call_id="call-7",
                observer=recorder,
                key=_KEY,
            )
        )

        event = recorder.one(OutputsCollected)
        assert (event.key, event.call_id) == (_KEY, "call-7")
        assert [artifact.call_id for artifact in sink.delivered] == ["call-7"]

    def test_a_per_call_sink_with_no_id_records_the_refusal_and_nothing_landed(self):
        recorder = _Recorder()
        sandbox = InProcessSandbox(seed_files={"/w/a.png": "1"})

        with pytest.raises(ValueError, match="per_call"):
            asyncio.run(
                collect_outputs(
                    sandbox,
                    _outputs_spec(DeclaredOutput(path="a.png")),
                    sink=_Sink(per_call=True).sink,
                    observer=recorder,
                    key=_KEY,
                )
            )

        event = recorder.one(OutputsCollected)
        assert (event.refusal, event.landed, event.call_id) == ("ValueError", (), None)

    def test_a_consumed_output_is_counted_and_not_landed(self):
        recorder = _Recorder()
        sandbox = InProcessSandbox(seed_files={"/w/r.sarif": "{}"})
        spec = _outputs_spec(
            DeclaredOutput(path="r.sarif", disposition=OutputDisposition.CONSUME),
        )

        asyncio.run(collect_outputs(sandbox, spec, observer=recorder, key=_KEY))

        event = recorder.one(OutputsCollected)
        assert (event.declared, event.landed) == (1, ())

    def test_a_refusal_records_what_a_sink_had_already_taken(self):
        """A deliver is a push nothing takes back, so the record has to survive the refusal."""
        recorder = _Recorder()
        sandbox = InProcessSandbox(seed_files={"/w/a.png": "1", "/w/b.png": "2"})
        spec = _outputs_spec(DeclaredOutput(path="a.png"), DeclaredOutput(path="b.png"))
        sink = _Sink(fail_on=1)

        with pytest.raises(RuntimeError, match="the store refused"):
            asyncio.run(collect_outputs(sandbox, spec, sink=sink.sink, observer=recorder, key=_KEY))

        event = recorder.one(OutputsCollected)
        assert event.refusal == "RuntimeError"
        assert [landed.name for landed in event.landed] == ["a.png"]

    def test_a_cap_refusal_before_anything_is_read_records_an_empty_collection(self):
        recorder = _Recorder()
        sandbox = InProcessSandbox(seed_files={"/w/a.png": "123456"})
        spec = _outputs_spec(
            DeclaredOutput(path="a.png"),
            files_out=TransferLimits(max_bytes_per_file=2, max_total_bytes=2, max_files=1),
        )

        with pytest.raises(SandboxTransferCapExceeded):
            asyncio.run(
                collect_outputs(sandbox, spec, sink=_Sink().sink, observer=recorder, key=_KEY)
            )

        event = recorder.one(OutputsCollected)
        assert (event.refusal, event.landed) == ("SandboxTransferCapExceeded", ())


# ---------------------------------------------------------------------------
# Store reads
# ---------------------------------------------------------------------------


def _session(recorder: SandboxObserver | None, *, thread_id: str | None = "thread-1"):
    return SandboxToolSession(
        _router(observer=recorder) if recorder is not None else _router(),
        _context(thread_id=thread_id),
        "agent-1",
        _SPEC,
        name="widget_run",
        logger=_LOG,
    )


class TestStoreReadsAreRecorded:
    def test_a_read_records_the_folded_label_and_the_length(self):
        recorder = _Recorder()
        store = InMemoryStore({"a.txt": "hello"}, integrity=SourceIntegrity.TRUSTED)
        session = _session(recorder)

        asyncio.run(session.read_file(store, ListedFile("a.txt", SourceIntegrity.TRUSTED)))

        event = recorder.one(StoreFileRead)
        assert (event.key, event.tool, event.name) == (_KEY, "widget_run", "a.txt")
        assert (event.integrity, event.characters, event.refused) == (
            SourceIntegrity.TRUSTED,
            5,
            False,
        )

    def test_a_read_refused_by_the_provenance_check_is_still_recorded(self):
        """The record's own refusal fires before the store is ever asked, and it is a supported
        configuration — a `trusted` floor no middleware observes — so it is a reachable way out
        of a read that would otherwise leave nothing behind."""
        recorder = _Recorder()
        session = SandboxToolSession(
            _router(observer=recorder),
            _context(),
            "agent-1",
            _SPEC,
            name="widget_run",
            logger=_LOG,
            file_store_provenance=FileStoreProvenance(floor=SourceIntegrity.TRUSTED),
        )

        with pytest.raises(ValueError, match="file_store_provenance_middleware"):
            asyncio.run(
                session.read_file(
                    InMemoryStore({"a.txt": "1"}), ListedFile("a.txt", SourceIntegrity.TRUSTED)
                )
            )

        event = recorder.one(StoreFileRead)
        assert (event.name, event.refused, event.characters) == ("a.txt", True, 0)

    def test_a_read_whose_second_provenance_reading_raises_is_still_recorded(self):
        """The fold reads the record a second time, and a path forgotten while the read was in
        flight can make that one raise where the first did not — after the store has answered,
        and so outside the handler that covers the store."""
        recorder = _Recorder()
        record = FileStoreProvenance()
        session = SandboxToolSession(
            _router(observer=recorder),
            _context(),
            "agent-1",
            _SPEC,
            name="widget_run",
            logger=_LOG,
            file_store_provenance=record,
        )
        readings = iter([(None, 0)])

        def state_of(name: str) -> tuple[SourceIntegrity | None, int]:
            try:
                return next(readings)
            except StopIteration:
                raise ValueError("file_store_provenance_middleware") from None

        record.state_of = state_of  # pyright: ignore[reportAttributeAccessIssue]

        with pytest.raises(ValueError):
            asyncio.run(session.read_file(InMemoryStore({"a.txt": "hi"}), ListedFile("a.txt")))

        event = recorder.one(StoreFileRead)
        assert (event.name, event.refused, event.characters) == ("a.txt", True, 0)

    def test_a_context_getter_that_cancels_does_not_fail_the_read(self):
        """The key is read for the record's sake alone, from the host's own context getters. A
        read that would otherwise have completed must not start failing because an observer is
        registered and one of those getters raised something outside `Exception`."""

        def cancels() -> str:
            raise asyncio.CancelledError

        session = SandboxToolSession(
            _router(observer=_Recorder()),
            CallerContext(
                current_scope=cancels,
                current_thread_id=lambda: "thread-1",
                list_files=InMemoryStore.list,
            ),
            "agent-1",
            _SPEC,
            name="widget_run",
            logger=_LOG,
        )

        answer = asyncio.run(session.read_file(InMemoryStore({"a.txt": "hi"}), ListedFile("a.txt")))
        assert getattr(answer, "text", None) == "hi"

    def test_a_read_a_cancel_took_is_still_recorded(self):
        """A cancel leaves the site the same way a raise does — no text crossed — and it is not
        an `Exception`, so it needs its own catch or the read goes unrecorded."""

        class _Cancels(InMemoryStore):
            async def read(self, name: str) -> str | None:
                raise asyncio.CancelledError

        recorder = _Recorder()
        session = _session(recorder)

        with pytest.raises(asyncio.CancelledError):
            asyncio.run(session.read_file(_Cancels({"a.txt": "x"}), ListedFile("a.txt")))

        event = recorder.one(StoreFileRead)
        assert (event.refused, event.characters, event.integrity) == (True, 0, None)

    def test_a_read_that_failed_is_recorded_as_refused(self):
        class _Broken(InMemoryStore):
            async def read(self, name: str) -> str | None:
                raise RuntimeError("the store is gone")

        recorder = _Recorder()
        session = _session(recorder)

        answer = asyncio.run(session.read_file(_Broken({"a.txt": "x"}), ListedFile("a.txt")))

        assert isinstance(answer, str) and answer.startswith("Error:")
        event = recorder.one(StoreFileRead)
        assert (event.refused, event.characters, event.integrity) == (True, 0, None)

    def test_a_file_that_has_gone_is_recorded_and_is_not_a_refusal(self):
        recorder = _Recorder()
        session = _session(recorder)

        assert asyncio.run(session.read_file(InMemoryStore({}), ListedFile("a.txt"))) is None

        event = recorder.one(StoreFileRead)
        assert (event.refused, event.characters) == (False, 0)

    def test_a_read_with_no_conversation_bound_records_no_key(self):
        recorder = _Recorder()
        session = _session(recorder, thread_id=None)

        asyncio.run(session.read_file(InMemoryStore({"a.txt": "x"}), ListedFile("a.txt")))

        assert recorder.one(StoreFileRead).key is None


# ---------------------------------------------------------------------------
# The call
# ---------------------------------------------------------------------------


def _tool(router: SandboxRouter, body):
    return sandboxed_tool(
        lambda session: body(session),
        router=router,
        context=_context(),
        agent_dir="agent-1",
        spec=_SPEC,
        name="widget_run",
        logger=_LOG,
    )[0]


def _fn(tool):
    return getattr(tool, "func", None) or getattr(tool, "__wrapped__", None) or tool


class TestTheCallIsRecorded:
    def test_a_call_that_acquired_a_sandbox_records_the_key_it_reached(self):
        recorder = _Recorder()
        router = _router(observer=recorder)

        def build(session: SandboxToolSession):
            async def widget_run() -> str:
                """Do a thing."""
                key = session.key()
                assert isinstance(key, SandboxKey)
                await session.acquire(key)
                return "done"

            return widget_run

        assert asyncio.run(_fn(_tool(router, build))()) == "done"

        event = recorder.one(ToolCallEnded)
        assert (event.tool, event.kind, event.keys) == ("widget_run", "test", (_KEY,))
        assert (event.failure, event.unclean) == (None, 0)
        assert event.seconds >= 0

    def test_a_refused_acquire_still_joins_to_the_call_that_asked(self):
        """The refusal gets its own `SandboxAcquired`, and that record is only useful if the
        call names the key too. `acquired` is the cleanup ledger and holds served keys only, so
        the join has to come from what the call *asked* for."""
        recorder = _Recorder()
        backend = InProcessSandboxBackend(
            acquire_error=SandboxCapabilityNotSupported("the backend cannot serve this")
        )
        router = _router(backend, observer=recorder)

        def build(session: SandboxToolSession):
            async def widget_run() -> str:
                """Do a thing."""
                key = session.key()
                assert isinstance(key, SandboxKey)
                await session.acquire(key)
                return "done"

            return widget_run

        asyncio.run(_fn(_tool(router, build))())

        assert recorder.one(SandboxAcquired).refusal == "SandboxCapabilityNotSupported"
        assert recorder.one(ToolCallEnded).keys == (_KEY,)

    def test_a_call_that_reached_two_sandboxes_records_both(self):
        """`acquire` takes a key, so one call can hold two — and naming only the first would
        leave the second's acquire and disposal records with nothing to join to."""
        recorder = _Recorder()
        router = _router(observer=recorder)
        other = SandboxKey(scope=_KEY.scope, thread_id=_KEY.thread_id, agent_dir="agent-2")

        def build(session: SandboxToolSession):
            async def widget_run() -> str:
                """Do a thing."""
                mine = session.key()
                assert isinstance(mine, SandboxKey)
                for key in (mine, other):
                    await session.acquire(key)
                return "done"

            return widget_run

        assert asyncio.run(_fn(_tool(router, build))()) == "done"

        assert recorder.one(ToolCallEnded).keys == (_KEY, other)

    def test_a_synchronous_body_is_recorded_too(self):
        """`sandboxed_tool` supports a body that awaits nothing, through its own wrapper. One
        event per call has to hold there as well, or a whole class of tool is invisible."""
        recorder = _Recorder()
        router = _router(observer=recorder)

        def build(session: SandboxToolSession):
            def widget_run() -> str:
                """Do a thing without awaiting."""
                return "done"

            return widget_run

        assert _fn(_tool(router, build))() == "done"

        event = recorder.one(ToolCallEnded)
        # Empty because `acquire` is a coroutine: a body that awaits nothing holds no sandbox.
        assert (event.tool, event.kind, event.keys) == ("widget_run", "test", ())
        assert (event.failure, event.unclean) == (None, 0)

    def test_a_body_that_returned_is_not_recorded_as_failing_its_label_check(self):
        """The wrapper's own refusal is not the body's failure, and `failure` names the body."""
        recorder = _Recorder()
        router = _router(observer=recorder)

        def build(session: SandboxToolSession):
            async def widget_run() -> list:
                """Return an empty list, which `sandboxed_tool` refuses after the body returns."""
                return []

            return widget_run

        with pytest.raises(ValueError):
            asyncio.run(_fn(_tool(router, build))())

        # The body returned. What raised was this package's own check on what it returned, and
        # attributing that to the body would send an operator to the wrong place.
        assert recorder.one(ToolCallEnded).failure is None

    def test_a_call_that_acquired_nothing_records_no_keys(self):
        recorder = _Recorder()
        router = _router(observer=recorder)

        def build(session: SandboxToolSession):
            async def widget_run() -> str:
                """Do a thing."""
                return "nothing to do"

            return widget_run

        asyncio.run(_fn(_tool(router, build))())

        assert recorder.one(ToolCallEnded).keys == ()

    def test_a_body_that_raised_records_the_class_it_raised(self):
        recorder = _Recorder()
        router = _router(observer=recorder)

        def build(session: SandboxToolSession):
            async def widget_run() -> str:
                """Do a thing."""
                raise KeyError("nope")

            return widget_run

        with pytest.raises(KeyError):
            asyncio.run(_fn(_tool(router, build))())

        assert recorder.one(ToolCallEnded).failure == "KeyError"

    def test_a_cancelled_call_is_still_recorded(self):
        """The one outcome that would otherwise leave no trace anywhere."""
        recorder = _Recorder()
        router = _router(observer=recorder)

        def build(session: SandboxToolSession):
            async def widget_run() -> str:
                """Do a thing."""
                raise asyncio.CancelledError

            return widget_run

        with pytest.raises(asyncio.CancelledError):
            asyncio.run(_fn(_tool(router, build))())

        assert recorder.one(ToolCallEnded).failure == "CancelledError"

    def test_a_transports_note_that_the_sandbox_is_not_clean_is_counted(self):
        from maf_sandbox._reclaim import note_unclean

        recorder = _Recorder()
        router = _router(observer=recorder)

        def build(session: SandboxToolSession):
            async def widget_run() -> str:
                """Do a thing."""
                key = session.key()
                assert isinstance(key, SandboxKey)
                sandbox = await session.acquire(key)
                note_unclean(sandbox, "a stop did not reach the program tree")
                return "done"

            return widget_run

        asyncio.run(_fn(_tool(router, build))())

        assert recorder.one(ToolCallEnded).unclean == 1

    def test_the_call_and_its_disposal_are_recorded_together(self):
        """The lifecycle record is the pair: what the call did, and what was done about it."""
        from maf_sandbox._reclaim import note_unclean

        recorder = _Recorder()
        router = _router(observer=recorder)

        def build(session: SandboxToolSession):
            async def widget_run() -> str:
                """Do a thing."""
                key = session.key()
                assert isinstance(key, SandboxKey)
                sandbox = await session.acquire(key)
                note_unclean(sandbox, "a stop did not reach the program tree")
                return "done"

            return widget_run

        asyncio.run(_fn(_tool(router, build))())

        assert recorder.one(SandboxDisposed).landed is True
        assert recorder.one(ToolCallEnded).unclean == 1


# ---------------------------------------------------------------------------
# The off position
# ---------------------------------------------------------------------------


class TestARouterWithNoObserverPaysNothing:
    """No observer, no event — the claim the default rests on, checked rather than asserted."""

    def test_no_event_is_built_for_an_acquire(self, monkeypatch):
        def _refuse(*args, **kwargs):
            raise AssertionError("an event was built for a router recording nowhere")

        monkeypatch.setattr(_router_module, "SandboxAcquired", _refuse)
        monkeypatch.setattr(_router_module, "SandboxDisposed", _refuse)
        router = _router()

        asyncio.run(router.acquire(_KEY, _SPEC))
        asyncio.run(router.dispose(_KEY))

    def test_no_event_is_built_for_a_host_tool_call(self, recwarn, monkeypatch):
        import maf_sandbox._host_tools as host_tools_module

        def _refuse(*args, **kwargs):
            raise AssertionError("an event was built for a registry recording nowhere")

        monkeypatch.setattr(host_tools_module, "HostToolCalled", _refuse)
        registry = HostToolRegistry()
        registry.register(_fetch, name="fetch")

        result = asyncio.run(_run(registry).call("fetch", {"url": "u"}))
        assert result.value_json == json.dumps("fetched u")

    def test_no_event_is_built_for_a_collection(self, monkeypatch):
        import maf_sandbox._outputs as outputs_module

        def _refuse(*args, **kwargs):
            raise AssertionError("an event was built for a collection recording nowhere")

        monkeypatch.setattr(outputs_module, "OutputsCollected", _refuse)
        sandbox = InProcessSandbox(seed_files={"/w/a.png": "1"})

        landed = asyncio.run(
            collect_outputs(sandbox, _outputs_spec(DeclaredOutput(path="a.png")), sink=_Sink().sink)
        )
        assert [artifact.name for artifact in landed] == ["a.png"]
