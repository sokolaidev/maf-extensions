"""Tests for the per-call record of what a sandbox was actually served.

Two properties carry it. **The snapshot is the served answer**, so a refusal leaves none and
every field on one describes something that held. And **it carries posture, never payload**: the
spec's ``labels`` and the sandbox key stay out, because this record is written into session
state, which a host persists and may hold to a different classification than the transcript it
sits beside.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
from typing import TYPE_CHECKING, Any

import pytest

from maf_sandbox import (
    CallerContext,
    Capability,
    DeclaredOutput,
    EffectiveState,
    Egress,
    HostToolRegistry,
    Identity,
    Isolation,
    IsolationScope,
    OutputDisposition,
    SandboxAcquired,
    SandboxCapabilityNotSupported,
    SandboxKey,
    SandboxObserver,
    SandboxRouter,
    SandboxSpec,
    SourceIntegrity,
    ToolCallEnded,
    TransferLimits,
    sandbox_tool,
)
from maf_sandbox import _effective_state as _state_module
from maf_sandbox._effective_state import (
    close_effective_state_notes,
    effective_state_is_noted,
    open_effective_state_notes,
)
from maf_sandbox.maf import (
    EFFECTIVE_STATE_KEY,
    SandboxToolSession,
    effective_state_middleware,
    list_no_files,
    sandboxed_tool,
)
from maf_sandbox.testing import FAKE_BACKEND_DECLARATIONS, InProcessSandboxBackend

if TYPE_CHECKING:
    from agent_framework import AgentSession

KEY = SandboxKey(scope="scope-a", thread_id="thread-1", agent_dir="agent")

#: Small enough that a wired host-tool surface folds into the transfer match without the
#: workload's own caps having to grow to meet it.
_TINY = TransferLimits(max_bytes_per_file=1024, max_total_bytes=4096, max_files=4)


@sandbox_tool(source=SourceIntegrity.UNTRUSTED, sink=None, identity=Identity.APP)
def weather(city: str) -> str:
    return "rain"


def _registry() -> HostToolRegistry:
    registry = HostToolRegistry(
        response_limits=TransferLimits(1024, 1024, 1), max_host_tool_calls_per_run=1
    )
    registry.register(weather)
    return registry


def _spec(**overrides: Any) -> SandboxSpec:
    fields: dict[str, Any] = {
        "kind": "codeact",
        "image": "python:3.13",
        "egress": Egress.ALLOWLIST,
        "egress_allow": ("pypi.org",),
        "labels": {"tenant": "contoso", "cost_centre": "cc-42"},
        "requires": frozenset({Capability.EXEC, Capability.HOST_TOOLS}),
        "files_in": _TINY,
        "files_out": _TINY,
        "host_tools": _registry().aggregate(),
    }
    fields.update(overrides)
    return SandboxSpec(**fields)


def _backend() -> InProcessSandboxBackend:
    return InProcessSandboxBackend(
        declarations=dataclasses.replace(
            FAKE_BACKEND_DECLARATIONS,
            capabilities=frozenset({Capability.EXEC, Capability.HOST_TOOLS, Capability.FILES_OUT}),
        )
    )


def _acquired(*, spec: SandboxSpec | None = None, call: str | None = None) -> SandboxAcquired:
    """A served acquire, built directly — for the cases a real router cannot reach."""
    return SandboxAcquired(
        key=KEY,
        spec=spec if spec is not None else _spec(),
        isolation_scope=IsolationScope.CONVERSATION,
        backend="in-process",
        isolation=None,
        declarations=None,
        seconds=0.0,
        call=call,
    )


def _served(spec: SandboxSpec | None = None) -> list[EffectiveState]:
    """Acquire once with notes open, and answer with whatever was noted."""
    router = SandboxRouter([_backend()], min_isolation=Isolation.NONE)
    notes, token = open_effective_state_notes()
    try:
        asyncio.run(router.acquire(KEY, spec or _spec()))
    finally:
        close_effective_state_notes(token)
    return notes


class TestWhatASnapshotHolds:
    """The served answer, field by field."""

    def test_it_names_the_backend_that_answered_and_the_rung_it_declared(self):
        (state,) = _served()
        assert state.backend == "in-process"
        assert state.isolation is Isolation.NONE

    def test_it_carries_the_resolved_scope_rather_than_the_one_the_spec_named(self):
        """The host's floor can raise it, so the spec alone is not the answer."""
        backend = InProcessSandboxBackend(
            declarations=dataclasses.replace(
                FAKE_BACKEND_DECLARATIONS,
                capabilities=frozenset({Capability.EXEC, Capability.HOST_TOOLS}),
                isolation_scopes=frozenset({IsolationScope.CONVERSATION, IsolationScope.CALL}),
            ),
            sandbox_per_key=True,
        )
        router = SandboxRouter(
            [backend], min_isolation=Isolation.NONE, min_isolation_scope=IsolationScope.CALL
        )
        notes, token = open_effective_state_notes()
        try:
            asyncio.run(router.acquire(dataclasses.replace(KEY, call_id="c1"), _spec()))
        finally:
            close_effective_state_notes(token)
        (state,) = notes
        assert state.isolation_scope is IsolationScope.CALL

    def test_it_carries_the_egress_posture_and_the_hosts_that_were_reachable(self):
        (state,) = _served()
        assert state.egress is Egress.ALLOWLIST
        assert state.egress_allow == ("pypi.org",)

    def test_it_names_every_tool_the_sealed_registry_was_carrying(self):
        """Which tools a sandbox was served with — the half no event answered before."""
        (state,) = _served()
        assert state.host_tools == frozenset({"weather"})
        assert state.identities == frozenset({Identity.APP})

    def test_a_workload_with_no_host_tools_names_none(self):
        (state,) = _served(_spec(requires=frozenset({Capability.EXEC}), host_tools=None))
        assert state.host_tools == frozenset()
        assert state.identities == frozenset()

    def test_it_carries_the_declared_outputs_by_path(self):
        spec = _spec(
            requires=frozenset({Capability.EXEC, Capability.HOST_TOOLS, Capability.FILES_OUT}),
            declared_outputs=(
                DeclaredOutput(path="report.md", disposition=OutputDisposition.LAND),
            ),
        )
        (state,) = _served(spec)
        assert state.declared_outputs == ("report.md",)

    def test_it_carries_the_backend_declarations_it_was_matched_against(self):
        (state,) = _served()
        assert state.backend_capabilities == frozenset(
            {Capability.EXEC, Capability.HOST_TOOLS, Capability.FILES_OUT}
        )
        assert state.backend_egress_modes == FAKE_BACKEND_DECLARATIONS.egress_modes

    def test_the_declarations_are_none_together_where_they_could_not_be_read(self):
        """A degraded read of a sandbox that *was* served, which is not a backend declaring none."""
        state = EffectiveState.of(_acquired())
        assert state is not None
        assert state.backend_capabilities is None
        assert state.backend_egress_modes is None
        assert state.backend == "in-process"


class TestPostureNeverPayload:
    """What is kept out, which is the reason this record is safe to persist."""

    def test_the_specs_labels_are_not_in_it(self):
        """Host deployment vocabulary — a tenant, a cost centre — and inclusion is a decision."""
        (state,) = _served()
        rendered = json.dumps(state.as_dict())
        assert "contoso" not in rendered
        assert "cost_centre" not in rendered
        assert "labels" not in rendered

    def test_the_call_id_is_in_it_where_the_key_is_not(self):
        """The session already is the conversation; what it cannot say is *which call*."""
        rendered = EffectiveState.of(_acquired(call="call-7"))
        assert rendered is not None
        assert rendered.as_dict()["call"] == "call-7"

    def test_the_sandbox_key_is_not_in_it(self):
        """The session is already that conversation; a scope and a thread id answer nothing more."""
        (state,) = _served()
        rendered = json.dumps(state.as_dict())
        assert KEY.scope not in rendered
        assert KEY.thread_id not in rendered

    def test_every_field_is_host_configuration_a_declaration_or_the_call_id(self):
        """A field naming something a model chose would put transcript content in a second store.

        `call` is the one identifier admitted: the framework generates it, it names nobody on
        its own, and it is what joins this record to the events the same call emitted.
        """
        assert {field.name for field in dataclasses.fields(EffectiveState)} == {
            "call",
            "kind",
            "backend",
            "isolation",
            "isolation_scope",
            "egress",
            "egress_allow",
            "requires",
            "image",
            "work_dir",
            "declared_outputs",
            "host_tools",
            "identities",
            "files_in",
            "files_out",
            "backend_capabilities",
            "backend_egress_modes",
        }


class TestTheJsonRendering:
    """`as_dict` is the only shape a session store is asked to hold."""

    def test_it_survives_a_round_trip_through_json(self):
        (state,) = _served()
        assert json.loads(json.dumps(state.as_dict())) == state.as_dict()

    def test_every_key_is_present_even_where_the_value_is_unset(self):
        """A fixed shape is what makes a record queryable a month later."""
        rendered = EffectiveState.of(_acquired(spec=SandboxSpec(kind="bicep")))
        assert rendered is not None
        assert rendered.as_dict()["image"] is None
        assert rendered.as_dict()["isolation"] is None
        assert rendered.as_dict()["backend_capabilities"] is None

    def test_sets_are_rendered_sorted_so_two_records_of_one_posture_compare_equal(self):
        (state,) = _served()
        rendered = state.as_dict()
        assert rendered["requires"] == sorted(rendered["requires"])
        assert rendered["backend_capabilities"] == sorted(rendered["backend_capabilities"])

    def test_the_caps_are_rendered_per_direction(self):
        (state,) = _served()
        assert state.as_dict()["files_out"] == {
            "max_bytes_per_file": 1024,
            "max_total_bytes": 4096,
            "max_files": 4,
        }


class TestOnlyAServedAcquireIsRecorded:
    def test_a_refusal_leaves_no_snapshot(self):
        """It already has an exception, a log line and a `SandboxAcquired` of its own."""
        router = SandboxRouter(
            [InProcessSandboxBackend(declarations=FAKE_BACKEND_DECLARATIONS)],
            min_isolation=Isolation.NONE,
        )
        notes, token = open_effective_state_notes()
        try:
            with pytest.raises(SandboxCapabilityNotSupported):
                asyncio.run(router.acquire(KEY, _spec()))
        finally:
            close_effective_state_notes(token)
        assert notes == []

    def test_acquiring_twice_on_one_key_records_one_sandbox(self):
        """Get-or-create hands back the same sandbox; two entries would read as two of them."""
        router = SandboxRouter([_backend()], min_isolation=Isolation.NONE)
        spec = _spec()
        notes, token = open_effective_state_notes()
        try:
            asyncio.run(router.acquire(KEY, spec))
            asyncio.run(router.acquire(KEY, spec))
        finally:
            close_effective_state_notes(token)
        assert len(notes) == 1

    def test_nothing_is_noted_outside_a_call(self):
        """A router driven directly has no call to attribute a snapshot to."""
        assert not effective_state_is_noted()
        router = SandboxRouter([_backend()], min_isolation=Isolation.NONE)
        asyncio.run(router.acquire(KEY, _spec()))
        assert not effective_state_is_noted()

    def test_a_snapshot_that_cannot_be_built_costs_a_warning_and_not_the_sandbox(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ):
        """It reads a backend's own declarations, in a `finally` over an acquire that succeeded."""

        def _explodes(event: SandboxAcquired) -> EffectiveState | None:
            raise RuntimeError("the declarations are not readable")

        monkeypatch.setattr(_state_module.EffectiveState, "of", staticmethod(_explodes))
        router = SandboxRouter([_backend()], min_isolation=Isolation.NONE)
        notes, token = open_effective_state_notes()
        try:
            with caplog.at_level(logging.WARNING, logger="maf_sandbox._router"):
                sandbox = asyncio.run(router.acquire(KEY, _spec()))
        finally:
            close_effective_state_notes(token)
        assert sandbox is not None
        assert notes == []
        assert any("was not recorded" in record.getMessage() for record in caplog.records)


class TestTheAcquirePathStaysFreeWhenNobodyIsListening:
    def test_no_observer_and_no_notes_builds_no_record(self, monkeypatch: pytest.MonkeyPatch):
        """The observer seam's own promise, extended: a host wiring neither pays for neither."""
        built: list[object] = []
        original = _state_module.EffectiveState.of

        def _counted(event: SandboxAcquired) -> EffectiveState | None:
            built.append(event)
            return original(event)

        monkeypatch.setattr(_state_module.EffectiveState, "of", staticmethod(_counted))
        router = SandboxRouter([_backend()], min_isolation=Isolation.NONE)
        asyncio.run(router.acquire(KEY, _spec()))
        assert built == []

    def test_an_observer_alone_gets_its_event_and_builds_no_snapshot(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """An observer reaches the record site on every acquire; it must not pay for both."""
        seen: list[SandboxAcquired] = []
        built: list[SandboxAcquired] = []

        class _Records(SandboxObserver):
            def sandbox_acquired(self, event: SandboxAcquired) -> None:
                seen.append(event)

        def _counted(event: SandboxAcquired) -> EffectiveState | None:
            built.append(event)
            return None

        monkeypatch.setattr(_state_module.EffectiveState, "of", staticmethod(_counted))
        router = SandboxRouter([_backend()], min_isolation=Isolation.NONE, observer=_Records())
        asyncio.run(router.acquire(KEY, _spec()))
        assert [event.backend for event in seen] == ["in-process"]
        assert built == []


class TestTheMiddlewareWritesIntoSessionState:
    """The other half: where a snapshot lands, and what happens when it cannot."""

    def _run(self, session: AgentSession | None, *, tool_name: str = "execute_code") -> None:
        """Run one tool call under the middleware, whose body acquires a sandbox."""
        from agent_framework import FunctionInvocationContext, FunctionTool

        router = SandboxRouter([_backend()], min_isolation=Isolation.NONE)

        async def _body() -> str:
            await router.acquire(KEY, _spec())
            return "ok"

        tool = FunctionTool(name=tool_name, func=_body)
        context = FunctionInvocationContext(function=tool, arguments={}, session=session)

        async def call_next() -> None:
            await tool.invoke(arguments={})

        asyncio.run(effective_state_middleware().process(context, call_next))

    def _session(self) -> AgentSession:
        from agent_framework import AgentSession

        return AgentSession()

    def test_a_served_call_lands_under_the_tool_the_model_called(self):
        session = self._session()
        self._run(session)
        served = session.state[EFFECTIVE_STATE_KEY]
        assert list(served) == ["execute_code"]
        assert served["execute_code"][0]["backend"] == "in-process"

    def test_what_it_writes_is_what_a_session_store_can_hold(self):
        session = self._session()
        self._run(session)
        assert json.loads(json.dumps(session.state)) == session.state

    def test_a_second_call_overwrites_rather_than_appends(self):
        """Session state lives as long as the conversation; a history here would grow forever."""
        session = self._session()
        self._run(session)
        self._run(session)
        assert len(session.state[EFFECTIVE_STATE_KEY]["execute_code"]) == 1

    def test_two_tools_keep_their_own_entries(self):
        session = self._session()
        self._run(session, tool_name="execute_code")
        self._run(session, tool_name="bicep_validate")
        assert set(session.state[EFFECTIVE_STATE_KEY]) == {
            "execute_code",
            "bicep_validate",
        }

    def test_a_call_that_acquired_nothing_leaves_the_previous_answer_standing(self):
        """It is still true: that *is* the last posture this tool was served under."""
        from agent_framework import FunctionInvocationContext, FunctionTool

        session = self._session()
        self._run(session)

        async def _quiet() -> str:
            return "no sandbox needed"

        tool = FunctionTool(name="execute_code", func=_quiet)
        context = FunctionInvocationContext(function=tool, arguments={}, session=session)

        async def call_next() -> None:
            await tool.invoke(arguments={})

        asyncio.run(effective_state_middleware().process(context, call_next))
        assert session.state[EFFECTIVE_STATE_KEY]["execute_code"]

    def test_a_body_that_raised_still_records_what_it_was_served(self):
        """The refusal an operator investigates is exactly the one whose posture they want."""
        from agent_framework import FunctionInvocationContext, FunctionTool

        session = self._session()
        router = SandboxRouter([_backend()], min_isolation=Isolation.NONE)

        async def _fails() -> str:
            await router.acquire(KEY, _spec())
            raise RuntimeError("the body broke after it was served")

        tool = FunctionTool(name="execute_code", func=_fails)
        context = FunctionInvocationContext(function=tool, arguments={}, session=session)

        async def call_next() -> None:
            await tool.invoke(arguments={})

        with pytest.raises(Exception):
            asyncio.run(effective_state_middleware().process(context, call_next))
        assert session.state[EFFECTIVE_STATE_KEY]["execute_code"]

    def test_no_session_says_so_once_rather_than_per_call(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ):
        from maf_sandbox import maf as _maf

        monkeypatch.setattr(_maf, "_warned_about_a_missing_session", False)
        with caplog.at_level(logging.WARNING, logger="maf_sandbox.maf"):
            self._run(None)
            self._run(None)
        said = [record for record in caplog.records if "carries no session" in record.getMessage()]
        assert len(said) == 1

    def test_a_key_something_else_owns_is_left_alone(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ):
        from maf_sandbox import maf as _maf

        monkeypatch.setattr(_maf, "_warned_about_an_occupied_key", False)
        session = self._session()
        session.state[EFFECTIVE_STATE_KEY] = "somebody else's"
        with caplog.at_level(logging.WARNING, logger="maf_sandbox.maf"):
            self._run(session)
        assert session.state[EFFECTIVE_STATE_KEY] == "somebody else's"
        assert any("was overwritten" in record.getMessage() for record in caplog.records)

    def test_an_occupied_key_says_so_once_rather_than_per_call(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ):
        """The condition holds for every call afterwards, so the flag is what bounds the log."""
        from maf_sandbox import maf as _maf

        monkeypatch.setattr(_maf, "_warned_about_an_occupied_key", False)
        session = self._session()
        session.state[EFFECTIVE_STATE_KEY] = "somebody else's"
        with caplog.at_level(logging.WARNING, logger="maf_sandbox.maf"):
            self._run(session)
            self._run(session)
        said = [record for record in caplog.records if "was overwritten" in record.getMessage()]
        assert len(said) == 1


class TestTheSnapshotJoinsToTheCallThatProducedIt:
    """Through `sandboxed_tool`, which is what puts a call id in scope for the router to read."""

    def _one_call(self) -> tuple[EffectiveState, ToolCallEnded]:
        """Run one real sandboxed tool call, and answer with its snapshot and its own record."""
        seen: list[ToolCallEnded] = []

        class _Records(SandboxObserver):
            def tool_call_ended(self, event: ToolCallEnded) -> None:
                seen.append(event)

        router = SandboxRouter([_backend()], min_isolation=Isolation.NONE, observer=_Records())
        context = CallerContext(
            current_scope=lambda: "scope-a",
            current_thread_id=lambda: "thread-1",
            list_files=list_no_files,
        )

        def build(session: SandboxToolSession):
            async def widget_run() -> str:
                """Do a thing."""
                key = session.key()
                assert isinstance(key, SandboxKey)
                await session.acquire(key)
                return "done"

            return widget_run

        (tool,) = sandboxed_tool(
            build,
            router=router,
            context=context,
            agent_dir="agent",
            spec=_spec(),
            name="widget_run",
            logger=logging.getLogger("test_effective_state"),
        )
        body = getattr(tool, "func", None) or getattr(tool, "__wrapped__", None) or tool

        notes, token = open_effective_state_notes()
        try:
            assert asyncio.run(body()) == "done"
        finally:
            close_effective_state_notes(token)
        (state,) = notes
        (ended,) = seen
        return state, ended

    def test_the_snapshot_names_the_call_its_own_record_names(self):
        """One string, so a reader joins the posture to everything else that call did."""
        state, ended = self._one_call()
        assert state.call == ended.call
        assert state.call

    def test_an_acquire_outside_a_call_names_none(self):
        """A router driven directly has no call to attribute the posture to."""
        (state,) = _served()
        assert state.call is None
