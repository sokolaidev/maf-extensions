"""Tests for the MAF-glue module.

Everything here is about the three decisions :mod:`maf_sandbox.maf` exists to make once
instead of once per workload: where a sandbox key comes from, what a provider failure is
allowed to tell the model, and what a sandbox tool declares about its own information flow.
Each of those is a property a workload would otherwise re-derive — and the failure mode of
getting one wrong is silent in all three cases, which is why they are pinned here rather
than left to the kinds' own suites.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging

import pytest

from maf_sandbox import (
    DEFAULT_CAPABILITIES,
    Artifact,
    CallerContext,
    Capability,
    DeclaredOutput,
    Egress,
    Isolation,
    LandedArtifact,
    NoSandboxBackend,
    OutputDisposition,
    OutputSink,
    SandboxBackendNotPermitted,
    SandboxEgressNotEnforced,
    SandboxKey,
    SandboxOutputSinkRequired,
    SandboxRouter,
    SandboxSpec,
)
from maf_sandbox.maf import (
    SandboxPurger,
    SandboxToolSession,
    list_all_files,
    list_no_files,
    make_caller_context,
    sandbox_tool_declarations,
    sandboxed_tool,
)
from maf_sandbox.testing import InMemoryStore, InProcessSandbox, InProcessSandboxBackend

_SPEC = SandboxSpec(kind="test", egress_allow=("example.invalid",), work_dir="/work")
_NO_EGRESS_SPEC = SandboxSpec(kind="test", work_dir="/work")
_KEY = SandboxKey(scope="scope-a", thread_id="thread-1", agent_dir="agent-1")

#: A spec that declares outputs must require the capability that reads them back — the pull
#: surface is what `FILES_OUT` names, and `sandboxed_tool` refuses the pair without it.
_PULLS = DEFAULT_CAPABILITIES | {Capability.FILES_OUT}

#: A closed-egress workload whose product is an artifact: the shape the cap's old condition
#: read as carrying nothing out of the conversation.
_LANDING_SPEC = SandboxSpec(
    kind="test",
    work_dir="/work",
    requires=_PULLS,
    declared_outputs=(DeclaredOutput(path="out.png", media_type="image/png"),),
)
_CONSUME_SPEC = SandboxSpec(
    kind="test",
    work_dir="/work",
    requires=_PULLS,
    declared_outputs=(DeclaredOutput(path="report.sarif", disposition=OutputDisposition.CONSUME),),
)

#: The CodeAct shape: it knows it lands artifacts and cannot say what they will be called, so
#: `declared_outputs` alone would answer every attach-time question wrongly.
_CALL_TIME_SPEC = SandboxSpec(
    kind="test", work_dir="/work", requires=_PULLS, outputs_named_at_call_time=True
)


async def _deliver(artifact: Artifact) -> LandedArtifact:
    return LandedArtifact(name=artifact.name, display=artifact.name)


_SINK = OutputSink(deliver=_deliver)


def _router(*backends, **kwargs):
    """Every fake here declares `process` isolation, so these routers opt below the floor."""
    return SandboxRouter(list(backends), min_isolation=Isolation.PROCESS, **kwargs)


def _pulling_backend():
    """A backend with the pull surface, for the specs that declare outputs.

    The fake defaults to what every `Sandbox` already owes, so a spec requiring `FILES_OUT`
    would be refused by the capability match before any of the sink rules below were reached.
    """
    return InProcessSandboxBackend(capabilities=_PULLS)


def _context(scope="scope-a", thread_id="thread-1", lister=None):
    return CallerContext(
        current_scope=lambda: scope,
        current_thread_id=lambda: thread_id,
        list_files=lister or InMemoryStore.list,
    )


def _session(
    backend=None,
    *,
    context=None,
    spec=_SPEC,
    name="workload_tool",
    logger=None,
):
    return SandboxToolSession(
        _router(backend if backend is not None else InProcessSandboxBackend()),
        context if context is not None else _context(),
        "agent-1",
        spec,
        name=name,
        logger=logger if logger is not None else logging.getLogger("test_workload"),
    )


# ---------------------------------------------------------------------------
# make_caller_context
# ---------------------------------------------------------------------------


class TestMakeCallerContext:
    def test_it_wires_each_callable_to_the_field_that_reads_it(self):
        store = InMemoryStore({"a.txt": "1"})
        context = make_caller_context(InMemoryStore.list, lambda: "scope-a", lambda: "thread-1")

        assert context.current_scope() == "scope-a"
        assert context.current_thread_id() == "thread-1"
        assert asyncio.run(context.list_files(store)) == ["a.txt"]

    def test_the_getters_are_read_per_call_not_captured_at_build_time(self):
        """The key must follow the host's request context, not the moment the tool was built.

        A host that builds one agent and serves many conversations with it would otherwise
        stamp every sandbox with whichever conversation happened to be current when the tool
        list was assembled — which is exactly the cross-conversation reach the key prevents.
        """
        current = {"scope": "scope-a", "thread": "thread-1"}
        context = make_caller_context(
            InMemoryStore.list,
            lambda: current["scope"],
            lambda: current["thread"],
        )
        assert (context.current_scope(), context.current_thread_id()) == ("scope-a", "thread-1")

        current["scope"], current["thread"] = "scope-b", "thread-2"
        assert (context.current_scope(), context.current_thread_id()) == ("scope-b", "thread-2")

    def test_building_the_context_calls_none_of_them(self):
        """Which is what lets a host import its heavy modules lazily inside those callables."""
        calls: list[str] = []

        async def _lister(store):
            calls.append("list")
            return []

        make_caller_context(
            _lister,
            lambda: calls.append("scope") or "s",  # type: ignore[func-returns-value]
            lambda: calls.append("thread") or "t",  # type: ignore[func-returns-value]
        )
        assert calls == []

    def test_a_none_thread_survives_the_round_trip(self):
        context = make_caller_context(InMemoryStore.list, lambda: "scope-a", lambda: None)
        assert context.current_thread_id() is None


# ---------------------------------------------------------------------------
# sandbox_tool_declarations — what a tool tells an information-flow policy
# ---------------------------------------------------------------------------


class TestSandboxToolDeclarations:
    """A declaration is not inert metadata: a policy engine reads these keys per call.

    The default therefore has to be the smallest true statement — trusted output, nothing
    said about confidentiality — because the alternative is a library silently activating a
    policy leg in a host that never asked for it.
    """

    def test_the_default_is_exactly_source_integrity_trusted(self):
        assert sandbox_tool_declarations(_SPEC) == {"source_integrity": "trusted"}

    def test_the_default_says_nothing_about_confidentiality(self):
        declarations = sandbox_tool_declarations(_SPEC)
        assert "confidentiality" not in declarations
        assert "max_allowed_confidentiality" not in declarations

    def test_no_integrity_claim_is_expressible(self):
        """A workload whose sandbox fetches arbitrary content must be able to decline."""
        assert sandbox_tool_declarations(_SPEC, source_integrity=None) == {}

    def test_an_outbound_cap_is_written_only_when_asked_for(self):
        assert sandbox_tool_declarations(_SPEC, outbound_max_confidentiality="private") == {
            "source_integrity": "trusted",
            "max_allowed_confidentiality": "private",
        }

    def test_a_sandbox_with_no_egress_gets_no_cap_even_when_asked(self):
        """Nothing can leave, so a cap would gate calls for a flow that does not exist."""
        assert sandbox_tool_declarations(
            _NO_EGRESS_SPEC, outbound_max_confidentiality="private"
        ) == {"source_integrity": "trusted"}

    def test_a_landing_spec_and_a_sink_write_the_cap_with_egress_shut(self):
        """The sink is the flow: guest bytes reach host state with the network still shut."""
        assert sandbox_tool_declarations(
            _LANDING_SPEC, outbound_max_confidentiality="private", output_sink=_SINK
        ) == {"source_integrity": "trusted", "max_allowed_confidentiality": "private"}

    def test_a_sink_writes_nothing_the_host_did_not_ask_for(self):
        """Attaching a sink is not itself a request to activate the confidentiality leg."""
        assert sandbox_tool_declarations(_LANDING_SPEC, output_sink=_SINK) == {
            "source_integrity": "trusted"
        }

    @pytest.mark.parametrize("spec", [_NO_EGRESS_SPEC, _CONSUME_SPEC])
    def test_a_sink_with_nothing_to_send_down_it_earns_no_cap(self, spec: SandboxSpec):
        """One sink is ordinarily handed to every sandbox tool a host builds, so its presence
        says nothing about *this* workload. A spec that declares no output — or only ones the
        kind consumes itself — carries nothing to host state, and capping it would gate calls
        for the flow this condition exists to avoid inventing."""
        assert sandbox_tool_declarations(
            spec, outbound_max_confidentiality="private", output_sink=_SINK
        ) == {"source_integrity": "trusted"}

    def test_a_call_time_spec_and_a_sink_earn_the_cap_too(self):
        """It lands artifacts; not being able to name them yet changes nothing about the flow,
        and reading `declared_outputs` alone would leave the cap silently off."""
        assert sandbox_tool_declarations(
            _CALL_TIME_SPEC, outbound_max_confidentiality="private", output_sink=_SINK
        ) == {"source_integrity": "trusted", "max_allowed_confidentiality": "private"}


# ---------------------------------------------------------------------------
# SandboxToolSession.key — the host keys the sandbox, never the model
# ---------------------------------------------------------------------------


class TestSessionKey:
    def test_the_key_comes_from_the_host_context_and_the_factory(self):
        key = _session().key()
        assert key == SandboxKey(scope="scope-a", thread_id="thread-1", agent_dir="agent-1")

    def test_a_call_with_no_bound_thread_is_refused_by_name(self):
        message = _session(context=_context(thread_id=None), name="widget_run").key()
        assert message == (
            "Error: no active thread context — widget_run must be called from within a thread"
        )

    def test_the_scope_getter_is_not_consulted_when_there_is_no_thread(self):
        """No thread means no key at all — not a key built from a half-resolved context."""
        calls: list[str] = []

        context = CallerContext(
            current_scope=lambda: calls.append("scope") or "s",  # type: ignore[func-returns-value]
            current_thread_id=lambda: None,
            list_files=InMemoryStore.list,
        )
        assert isinstance(_session(context=context).key(), str)
        assert calls == []

    def test_the_key_follows_the_context_between_calls(self):
        current = {"thread": "thread-1"}
        session = _session(
            context=CallerContext(
                current_scope=lambda: "scope-a",
                current_thread_id=lambda: current["thread"],
                list_files=InMemoryStore.list,
            )
        )
        assert session.key().thread_id == "thread-1"  # type: ignore[union-attr]
        current["thread"] = "thread-2"
        assert session.key().thread_id == "thread-2"  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# SandboxToolSession.list_files — the injection-pinning boundary
# ---------------------------------------------------------------------------


class TestSessionListFiles:
    def test_it_returns_the_hosts_listing(self):
        store = InMemoryStore({"a.bicep": "1", "b/c.bicep": "2"})
        assert sorted(asyncio.run(_session().list_files(store))) == ["a.bicep", "b/c.bicep"]

    def test_a_failure_is_a_refusal_rather_than_an_empty_listing(self):
        """An empty list would read as "the file store is empty" and refuse for the wrong reason."""

        async def _boom(store):
            raise RuntimeError("store is down")

        session = _session(context=_context(lister=_boom))
        assert asyncio.run(session.list_files(InMemoryStore({}))) == (
            "Error: could not list the file store: store is down"
        )


# ---------------------------------------------------------------------------
# SandboxToolSession.acquire — what the model may be told, and what only the log gets
# ---------------------------------------------------------------------------


class TestSessionAcquire:
    def test_a_healthy_acquire_returns_the_sandbox(self):
        backend = InProcessSandboxBackend()
        session = _session(backend)
        assert asyncio.run(session.acquire(_KEY)) is backend.sandbox
        assert backend.keys == [_KEY]
        assert backend.specs == [_SPEC]

    def test_a_missing_sdk_is_named_because_it_carries_no_account_detail(self, caplog):
        session = _session(InProcessSandboxBackend(acquire_error=ImportError("no sdk")))
        with caplog.at_level(logging.WARNING, logger="test_workload"):
            assert asyncio.run(session.acquire(_KEY)) == (
                "Error: the sandbox backend is not installed — degrading to T0"
            )
        assert "sandbox SDK unavailable: no sdk" in caplog.text

    def test_a_missing_backend_is_named_too(self, caplog):
        session = _session(InProcessSandboxBackend(acquire_error=NoSandboxBackend("none here")))
        with caplog.at_level(logging.WARNING, logger="test_workload"):
            assert asyncio.run(session.acquire(_KEY)) == (
                "Error: no sandbox backend is configured — degrading to T0"
            )

    def test_a_value_error_is_surfaced_verbatim_because_this_stack_authored_it(self):
        """Image resolution raises these, and they are the actionable half of a misconfig."""
        session = _session(
            InProcessSandboxBackend(acquire_error=ValueError("No disk image for 'bicep:1'"))
        )
        assert asyncio.run(session.acquire(_KEY)) == "Error: No disk image for 'bicep:1'"

    def test_a_provider_failure_reaches_the_log_and_never_the_model(self, caplog):
        """Tool results are persisted into the transcript; SDK errors carry account detail."""

        class _HttpError(Exception):
            status_code = 400

            def __str__(self) -> str:
                return "Operation returned an invalid status 'Bad Request'"

            class response:  # noqa: N801 - mimics the SDK's attribute shape
                @staticmethod
                def text() -> str:
                    return '{"error":"principal lacks a role on group acas-x"}'

        session = _session(InProcessSandboxBackend(acquire_error=_HttpError()))
        with caplog.at_level(logging.WARNING, logger="test_workload"):
            out = asyncio.run(session.acquire(_KEY))

        assert out == "Error: sandbox unavailable — degrading to T0 (LLM self-check only)"
        assert "status=400" in caplog.text
        assert "principal lacks a role" in caplog.text
        assert "principal lacks a role" not in out
        assert "acas-x" not in out

    def test_the_records_carry_the_workloads_logger_and_its_own_prefix(self, caplog):
        """A kind passes its logger so its records keep its name — the prefix rides the format.

        Both halves matter to a structured exporter: the logger NAME is how a deployment
        filters first-party records, and `record.msg` (the format string, not the rendered
        line) is what several exporters index on. Composing the prefix into the format keeps
        an extracted line indistinguishable from one the workload wrote by hand.
        """
        session = _session(
            InProcessSandboxBackend(acquire_error=ImportError("no sdk")),
            name="widget_run",
            logger=logging.getLogger("some_kind._tool"),
        )
        with caplog.at_level(logging.WARNING, logger="some_kind"):
            asyncio.run(session.acquire(_KEY))

        (record,) = [r for r in caplog.records if r.name.startswith("some_kind")]
        assert record.msg == "widget_run: sandbox SDK unavailable: %s"
        assert record.getMessage() == "widget_run: sandbox SDK unavailable: no sdk"

    def test_a_percent_in_a_tool_name_is_not_read_as_a_format_specifier(self):
        """The prefix is baked into the format string, so it has to be escaped once."""
        session = _session(
            InProcessSandboxBackend(acquire_error=ImportError("no sdk")),
            name="odd%name",
            logger=logging.getLogger("some_kind._tool"),
        )
        records: list[logging.LogRecord] = []
        handler = logging.Handler()
        handler.emit = records.append  # type: ignore[method-assign]
        logging.getLogger("some_kind._tool").addHandler(handler)
        try:
            asyncio.run(session.acquire(_KEY))
        finally:
            logging.getLogger("some_kind._tool").removeHandler(handler)

        assert records[0].getMessage() == "odd%name: sandbox SDK unavailable: no sdk"


# ---------------------------------------------------------------------------
# sandboxed_tool — attach or do not attach, and what the attached tool declares
# ---------------------------------------------------------------------------


def _body(session: SandboxToolSession):
    async def widget_run(target: str) -> str:
        """Do a thing to ``target`` inside a sandbox.

        Args:
            target: What to do it to.
        """
        key = session.key()
        if isinstance(key, str):
            return key
        sandbox = await session.acquire(key)
        if isinstance(sandbox, str):
            return sandbox
        result = await sandbox.exec(
            ["echo", target], working_directory=session.spec.work_dir, timeout=5
        )
        return result.stdout

    return widget_run


def _attach(router, *, context=None, spec=_SPEC, **kw):
    return sandboxed_tool(
        _body,
        router=router,
        context=context if context is not None else _context(),
        agent_dir="agent-1",
        spec=spec,
        name="widget_run",
        **kw,
    )


def _call(tool, **kwargs):
    fn = getattr(tool, "func", None) or getattr(tool, "__wrapped__", None) or tool
    return asyncio.run(fn(**kwargs))


class TestAttachGate:
    """A host with no sandbox gets no tool, not a tool that fails when the model calls it."""

    def test_no_router_attaches_nothing(self):
        assert _attach(None) == []

    def test_a_router_with_no_backend_attaches_nothing(self):
        assert _attach(SandboxRouter([])) == []

    def test_the_body_is_never_built_when_unconfigured(self):
        built: list[str] = []

        def _counting(session):
            built.append("built")
            return _body(session)

        assert (
            sandboxed_tool(
                _counting,
                router=None,
                context=_context(),
                agent_dir="agent-1",
                spec=_SPEC,
                name="widget_run",
            )
            == []
        )
        assert built == []

    def test_a_configured_router_attaches_exactly_one_tool(self):
        tools = _attach(_router(InProcessSandboxBackend()))
        assert len(tools) == 1


class TestAttachedToolShape:
    def _tool(self, **kw):
        (tool,) = _attach(_router(InProcessSandboxBackend()), **kw)
        return tool

    def test_the_name_is_the_one_the_factory_was_given(self):
        assert self._tool().name == "widget_run"

    def test_the_description_is_the_bodys_docstring_verbatim(self):
        """MAF passes ``__doc__`` through untouched, indentation included."""
        assert self._tool().description == _body(_session()).__doc__

    def test_approval_mode_defaults_to_never_require(self):
        assert self._tool().approval_mode == "never_require"

    def test_approval_mode_is_overridable(self):
        assert self._tool(approval_mode="always_require").approval_mode == "always_require"

    def test_the_declarations_come_from_the_spec(self):
        assert self._tool().additional_properties == {"source_integrity": "trusted"}

    def test_explicit_declarations_win_over_the_derivation(self):
        assert self._tool(declarations={"source_integrity": "untrusted"}).additional_properties == {
            "source_integrity": "untrusted"
        }

    def test_an_outbound_cap_reaches_the_tool_when_the_host_asks_for_one(self):
        assert self._tool(outbound_max_confidentiality="private").additional_properties == {
            "source_integrity": "trusted",
            "max_allowed_confidentiality": "private",
        }

    def test_the_sink_reaches_the_derivation_and_not_only_the_workload(self):
        """A closed-egress spec earns the cap here only if the sink was threaded through."""
        (tool,) = _attach(
            _router(_pulling_backend()),
            spec=_LANDING_SPEC,
            outbound_max_confidentiality="private",
            output_sink=_SINK,
        )
        assert tool.additional_properties == {
            "source_integrity": "trusted",
            "max_allowed_confidentiality": "private",
        }

    def test_source_integrity_reaches_the_derivation_without_the_declarations_escape_hatch(self):
        """The pair a kind running model-written code needs: no integrity claim *and* a sink.
        `declarations=` is refused alongside a sink, so before this parameter existed the two
        could not both be had."""
        (tool,) = _attach(
            _router(_pulling_backend()),
            spec=_LANDING_SPEC,
            source_integrity=None,
            outbound_max_confidentiality="private",
            output_sink=_SINK,
        )
        assert tool.additional_properties == {"max_allowed_confidentiality": "private"}

    def test_an_explicit_mapping_still_wins_over_it(self):
        assert self._tool(
            source_integrity=None, declarations={"source_integrity": "untrusted"}
        ).additional_properties == {"source_integrity": "untrusted"}

    def test_the_declarations_dict_is_not_shared_with_the_caller(self):
        declarations = {"source_integrity": "trusted"}
        tool = self._tool(declarations=declarations)
        declarations["source_integrity"] = "tampered"
        assert tool.additional_properties == {"source_integrity": "trusted"}


class TestAttachedToolRuns:
    def test_the_body_reaches_the_sandbox_through_the_session(self):
        backend = InProcessSandboxBackend(InProcessSandbox(default_stdout="ok"))
        (tool,) = _attach(_router(backend))

        assert _call(tool, target="thing") == "ok"
        assert backend.keys == [SandboxKey("scope-a", "thread-1", "agent-1")]
        assert backend.sandbox.commands == [("echo thing", "/work", 5)]

    def test_a_refusal_is_returned_as_the_tools_answer(self):
        backend = InProcessSandboxBackend()
        (tool,) = _attach(_router(backend), context=_context(thread_id=None))

        assert _call(tool, target="thing") == (
            "Error: no active thread context — widget_run must be called from within a thread"
        )
        assert backend.keys == []


class TestEgressIsCheckedWhereTheToolAttaches:
    """The attach gate answers "nothing configured" and "cannot honour this" differently.

    Collapsing them would ship a workload with containment it does not have, wearing the same
    empty list as a host that simply left the feature off.
    """

    def test_a_backend_that_cannot_confine_egress_raises(self):
        with pytest.raises(SandboxEgressNotEnforced):
            _attach(_router(InProcessSandboxBackend(egress=Egress.UNRESTRICTED)))

    def test_nothing_configured_still_returns_an_empty_list(self):
        assert _attach(SandboxRouter([])) == []

    def test_a_backend_that_can_confine_egress_attaches(self):
        assert len(_attach(_router(InProcessSandboxBackend()))) == 1


class TestTheIsolationFloorStillApplies:
    """The glue adds no way around the router's construction-time refusal."""

    def test_a_backend_below_the_floor_cannot_be_attached(self):
        """Supersedes the deployed flag with a host-declared floor (two-axis policy, axis 1)."""
        with pytest.raises(SandboxBackendNotPermitted):
            _attach(SandboxRouter([InProcessSandboxBackend(isolation=Isolation.CONTAINER)]))

    def test_a_workload_that_raises_the_floor_refuses_at_attach_time(self):
        """The spec's own floor is the other half: a kind may demand more than the host does."""
        with pytest.raises(SandboxBackendNotPermitted):
            _attach(
                _router(InProcessSandboxBackend()),
                spec=SandboxSpec(kind="test", min_isolation=Isolation.MICROVM),
            )


# ---------------------------------------------------------------------------
# output_sink — the two refusals, and where they sit relative to the attach gate
# ---------------------------------------------------------------------------


class TestTheSinkRefusals:
    """Both refusals answer a question the caller got wrong, and both wait for the gate.

    Placing either one ahead of the attach gate would turn a host that simply left sandboxing
    off into a host whose agent factory raises — the one thing rule 1 of ``sandboxed_tool``
    promises never happens.
    """

    def test_a_sink_with_an_explicit_declarations_mapping_is_refused(self):
        """The mapping wins verbatim, so the pair would attach a tool blind to its own sink."""
        with pytest.raises(ValueError, match="never both"):
            _attach(
                _router(InProcessSandboxBackend()),
                declarations={"source_integrity": "untrusted"},
                output_sink=_SINK,
            )

    def test_a_spec_that_lands_without_a_sink_is_refused(self):
        with pytest.raises(SandboxOutputSinkRequired, match="out.png"):
            _attach(_router(_pulling_backend()), spec=_LANDING_SPEC)

    def test_an_unconfigured_host_gets_an_empty_list_from_either_refusal(self):
        assert _attach(None, spec=_LANDING_SPEC) == []
        assert _attach(SandboxRouter([]), spec=_LANDING_SPEC) == []
        assert _attach(None, declarations={}, output_sink=_SINK) == []

    def test_a_consume_only_spec_needs_no_sink(self):
        """A SARIF file the kind parses itself is a source, and never reaches host state."""
        assert len(_attach(_router(_pulling_backend()), spec=_CONSUME_SPEC)) == 1

    def test_a_landing_spec_with_a_sink_attaches(self):
        tools = _attach(_router(_pulling_backend()), spec=_LANDING_SPEC, output_sink=_SINK)
        assert len(tools) == 1

    def test_a_call_time_spec_without_a_sink_is_refused_with_nothing_to_name(self):
        with pytest.raises(SandboxOutputSinkRequired, match="names at call time"):
            _attach(_router(_pulling_backend()), spec=_CALL_TIME_SPEC)

    def test_a_call_time_spec_with_a_sink_attaches(self):
        assert (
            len(_attach(_router(_pulling_backend()), spec=_CALL_TIME_SPEC, output_sink=_SINK)) == 1
        )


class TestTheSessionCarriesTheSinkThatWasChecked:
    """The object that checked the invariant is the one that has to honour it.

    A kind closing over its own sink could hand `collect_outputs` a different one from the sink
    whose presence satisfied the refusal above, and nothing would notice.
    """

    def test_the_body_reads_the_sink_off_the_session(self):
        seen: list[OutputSink | None] = []

        def _capture(session: SandboxToolSession):
            seen.append(session.output_sink)
            return _body(session)

        sandboxed_tool(
            _capture,
            router=_router(_pulling_backend()),
            context=_context(),
            agent_dir="agent-1",
            spec=_LANDING_SPEC,
            name="widget_run",
            output_sink=_SINK,
        )
        assert seen == [_SINK]

    def test_a_workload_that_lands_nothing_sees_none(self):
        assert _session().output_sink is None


class TestDeclaredOutputsImplyTheCapability:
    """`Sandbox` promises a kind never has to feature-detect the pull surface, and this is
    what pays for it: a spec that declares outputs and does not require `FILES_OUT` skips the
    capability match entirely, and fails inside the sandbox instead of at the factory."""

    _UNDECLARED = dataclasses.replace(_LANDING_SPEC, requires=DEFAULT_CAPABILITIES)

    def test_declaring_an_output_without_requiring_the_capability_is_refused(self):
        with pytest.raises(ValueError, match="files_out"):
            _attach(_router(_pulling_backend()), spec=self._UNDECLARED, output_sink=_SINK)

    def test_a_consume_output_needs_it_just_the_same(self):
        """It is read through the same surface; only where its bytes go afterwards differs."""
        spec = dataclasses.replace(_CONSUME_SPEC, requires=DEFAULT_CAPABILITIES)
        with pytest.raises(ValueError, match="files_out"):
            _attach(_router(_pulling_backend()), spec=spec)

    def test_naming_outputs_at_call_time_needs_it_just_the_same(self):
        """The names arrive later; the surface that reads them is required now."""
        spec = dataclasses.replace(_CALL_TIME_SPEC, requires=DEFAULT_CAPABILITIES)
        with pytest.raises(ValueError, match="files_out"):
            _attach(_router(_pulling_backend()), spec=spec, output_sink=_SINK)

    def test_a_spec_declaring_nothing_needs_nothing_new(self):
        """Which is the other half of the rule: grow `requires` from what you declare."""
        assert len(_attach(_router(InProcessSandboxBackend()), spec=_NO_EGRESS_SPEC)) == 1

    def test_an_unconfigured_host_still_gets_an_empty_list(self):
        assert _attach(None, spec=self._UNDECLARED, output_sink=_SINK) == []


# ---------------------------------------------------------------------------
# The purge participant, re-exported
# ---------------------------------------------------------------------------


class TestPurgerReExport:
    def test_the_glue_exposes_the_same_class_the_package_does(self):
        import maf_sandbox

        assert SandboxPurger is maf_sandbox.SandboxPurger

    def test_a_host_can_wire_the_whole_maf_surface_from_this_one_module(self):
        backend = InProcessSandboxBackend()
        purger = SandboxPurger(_router(backend))
        assert asyncio.run(purger.purge_scoped_thread("scope-a", "thread-1")) == 1


@dataclasses.dataclass(frozen=True)
class _Entry:
    """The two `FileStoreEntry` fields the walk reads, and nothing else."""

    name: str
    type: str


class _TreeStore:
    """An `AgentFileStore` narrowed to `list_children`, answering one level at a time."""

    def __init__(self, tree: dict[str, list[_Entry]], *, fails: Exception | None = None) -> None:
        self._tree = tree
        self._fails = fails
        self.asked: list[str] = []

    async def list_children(self, directory: str) -> list[_Entry]:
        if self._fails is not None:
            raise self._fails
        self.asked.append(directory)
        return self._tree.get(directory, [])


class TestListAllFiles:
    """The store walk four samples each wrote for themselves."""

    def test_it_returns_every_file_as_a_store_relative_path(self):
        store = _TreeStore(
            {
                "": [_Entry("a.txt", "file"), _Entry("infra", "directory")],
                "infra": [_Entry("main.bicep", "file"), _Entry("modules", "directory")],
                "infra/modules": [_Entry("net.bicep", "file")],
            }
        )

        assert asyncio.run(list_all_files(store)) == [
            "a.txt",
            "infra/main.bicep",
            "infra/modules/net.bicep",
        ]

    def test_directories_are_walked_and_never_listed_as_files(self):
        """A directory in the listing would be a name a workload could substitute into a
        command, and it is not a file it can act on."""
        store = _TreeStore({"": [_Entry("infra", "directory")], "infra": []})

        assert asyncio.run(list_all_files(store)) == []
        assert store.asked == ["", "infra"]

    def test_an_empty_store_lists_nothing(self):
        assert asyncio.run(list_all_files(_TreeStore({"": []}))) == []

    def test_a_store_failure_propagates_rather_than_reading_as_an_empty_store(self):
        """The distinction the injection-pinning boundary rests on: "no files" and "could not
        ask" refuse a name for entirely different reasons, and only one of them is the
        caller's fault."""
        store = _TreeStore({}, fails=RuntimeError("store is down"))

        with pytest.raises(RuntimeError, match="store is down"):
            asyncio.run(list_all_files(store))


class TestListNoFiles:
    def test_it_lists_nothing_whatever_it_is_handed(self):
        assert asyncio.run(list_no_files(object())) == []
