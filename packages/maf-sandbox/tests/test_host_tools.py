"""Tests for the host-tools safety contract (issue #133, part A).

The contract is what has to exist before anything can call a host tool, so what is pinned here is
the *shape of refusal* as much as the happy path: an unstamped function refused where the
host can fix it, an undeclared name unreachable from inside, a cap that ends a run with a
sentence rather than an exception, and a USER-identity tool that registers loudly and never
serves.  Router-level denial — the sixth layer — is pinned in ``test_sandbox_router.py``
beside the rest of the router's policy.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import importlib
import inspect
import json
import logging
import time
from collections.abc import Generator

import pytest

import maf_sandbox._host_tools as host_tools_module
from maf_sandbox import (
    DEFAULT_MAX_HOST_TOOL_CALLS_PER_RUN,
    DEFAULT_TRANSFER_LIMITS,
    FLOW_DECLARED_KEY,
    INTEGRITY_RANK,
    HostToolCallResult,
    HostToolDeclaration,
    HostToolIdentityNotAllowed,
    HostToolNotDeclared,
    HostToolRegistry,
    HostToolRun,
    Identity,
    MafSandboxHostToolsWarning,
    SandboxLimits,
    SourceIntegrity,
    TransferLimits,
    declaration_of,
    sandbox_tool,
)


@pytest.fixture(autouse=True)
def _quiet_registration_notice(monkeypatch: pytest.MonkeyPatch):
    """Pretend the one-time notice already fired, so only the tests about it see it."""
    monkeypatch.setattr(host_tools_module._RegistrationNotice, "warned", True)


def _pure(x: int) -> int:
    return x * 2


def _stamped_pure():
    @sandbox_tool(source=None, sink=None, identity=None)
    def doubled(x: int) -> int:
        return x * 2

    return doubled


class TestSandboxToolDecorator:
    def test_stamps_and_returns_the_same_function(self):
        def lookup() -> str:
            return "docs"

        stamped = sandbox_tool(source=SourceIntegrity.TRUSTED, sink=None, identity=Identity.APP)(
            lookup
        )
        assert stamped is lookup
        declaration = declaration_of(stamped)
        assert declaration == HostToolDeclaration(
            source=SourceIntegrity.TRUSTED, sink=None, identity=Identity.APP
        )

    def test_every_leg_is_mandatory(self):
        """No defaults, by design: an unanswered leg is a `TypeError`, not an assumption."""
        with pytest.raises(TypeError):
            sandbox_tool(source=None, sink=None)  # type: ignore[call-arg]
        with pytest.raises(TypeError):
            sandbox_tool(source=None, identity=None)  # type: ignore[call-arg]
        with pytest.raises(TypeError):
            sandbox_tool(sink=None, identity=None)  # type: ignore[call-arg]

    def test_an_unknown_source_tier_is_refused(self):
        """The enum constructor is the refuse-unknown policy, here as everywhere."""
        with pytest.raises(ValueError):
            sandbox_tool(source="verified", sink=None, identity=None)  # type: ignore[arg-type]

    def test_an_unknown_identity_is_refused(self):
        with pytest.raises(ValueError):
            sandbox_tool(source=None, sink=None, identity="root")  # type: ignore[arg-type]

    def test_a_non_string_sink_is_refused(self):
        """Sink values are opaque host vocabulary — opaque, but still strings."""
        with pytest.raises(TypeError, match="host's own confidentiality vocabulary"):
            sandbox_tool(source=None, sink=7, identity=None)  # type: ignore[arg-type]

    def test_a_partial_hand_stamp_reads_as_unstamped(self):
        """Two legs of three answered is no declaration: the sentinel means every leg was."""

        def tool() -> None:
            pass

        setattr(tool, FLOW_DECLARED_KEY, {"source": None, "sink": None})
        assert declaration_of(tool) is None

    def test_an_undecorated_function_reads_as_unstamped(self):
        assert declaration_of(_pure) is None


class TestIntegrityRank:
    def test_every_member_is_ranked(self):
        """An unranked tier would raise `KeyError` inside the aggregate's fold."""
        assert set(INTEGRITY_RANK) == set(SourceIntegrity)

    def test_untrusted_is_the_weakest(self):
        assert INTEGRITY_RANK[SourceIntegrity.UNTRUSTED] < INTEGRITY_RANK[SourceIntegrity.TRUSTED]


class TestRegistryConstruction:
    def test_the_cap_default_is_the_named_constant(self):
        assert HostToolRegistry().max_host_tool_calls_per_run == DEFAULT_MAX_HOST_TOOL_CALLS_PER_RUN

    def test_the_default_cap_bites_before_the_response_count_leg(self):
        """So with everything defaulted, exhaustion names the host-tool-call cap, not the ledger."""
        assert DEFAULT_MAX_HOST_TOOL_CALLS_PER_RUN < DEFAULT_TRANSFER_LIMITS.max_files

    def test_a_cap_below_one_is_refused(self):
        with pytest.raises(ValueError, match="empty registry"):
            HostToolRegistry(max_host_tool_calls_per_run=0)

    def test_the_pair_of_directions_type_is_refused_as_response_limits(self):
        """The same TransferLimits-vs-SandboxLimits confusion the router already refuses."""
        with pytest.raises(TypeError, match="TransferLimits"):
            HostToolRegistry(response_limits=SandboxLimits())  # type: ignore[arg-type]


class TestTheRunIdentity:
    """`HostToolRun.run_id` — a documented identity a host attributes a call by (#446)."""

    def test_each_run_gets_a_distinct_identity_by_default(self):
        first = HostToolRun(HostToolRegistry())
        second = HostToolRun(HostToolRegistry())
        assert first.run_id and second.run_id
        assert first.run_id != second.run_id

    def test_a_caller_supplied_identity_is_kept(self):
        assert HostToolRun(HostToolRegistry(), run_id="turn-7").run_id == "turn-7"

    def test_the_identity_is_stable_across_host_tool_calls(self):
        """A ledger keyed on it must see one run as one run, not a fresh id per call."""
        registry = HostToolRegistry()
        registry.register(_stamped_pure())
        run = HostToolRun(registry)
        before = run.run_id
        assert _call_host_tool(run, "doubled", {"x": 1}).ok
        assert run.run_id == before

    @pytest.mark.parametrize("bad", ["", 0, object()])
    def test_an_empty_or_non_string_identity_is_refused(self, bad: object):
        """An identity that cannot tell two runs apart is rejected at construction."""
        with pytest.raises(ValueError, match="run_id"):
            HostToolRun(HostToolRegistry(), run_id=bad)  # type: ignore[arg-type]


class TestRegistration:
    def test_starts_empty(self):
        """Layer 1: nothing is callable as a host tool until a developer explicitly registers it."""
        registry = HostToolRegistry()
        assert len(registry) == 0
        assert registry.names() == frozenset()

    def test_registers_under_the_function_name_by_default(self):
        registry = HostToolRegistry()
        registry.register(_pure)
        assert registry.names() == frozenset({"_pure"})
        assert registry.resolve("_pure") is _pure

    def test_a_duplicate_name_is_refused(self):
        registry = HostToolRegistry()
        registry.register(_pure)
        with pytest.raises(ValueError, match="already registered"):
            registry.register(_stamped_pure(), name="_pure")

    def test_a_non_callable_is_refused(self):
        registry = HostToolRegistry()
        with pytest.raises(TypeError, match="callable"):
            registry.register("not a function")  # type: ignore[arg-type]

    def test_a_non_string_name_is_refused(self):
        """A tool keyed by 7 registers, appears in a `frozenset[str]`, and no guest can ever
        name it: a transport carries the name as JSON text."""
        registry = HostToolRegistry()
        with pytest.raises(TypeError, match="must be a string"):
            registry.register(_stamped_pure(), name=7)  # type: ignore[arg-type]

    def test_a_callable_whose_own_name_is_not_a_string_is_refused(self):
        """`__name__` is an ordinary attribute, so the default name is not a string by fiat."""

        class Odd:
            __name__ = 7

            def __call__(self) -> int:
                return 1

        registry = HostToolRegistry()
        with pytest.raises(TypeError, match="must be a string"):
            registry.register(Odd())

    def test_the_gate_refuses_an_unstamped_function_at_registration(self):
        """Where the host can fix it — one decorator away — not later at call time."""
        registry = HostToolRegistry(require_declared=True)
        with pytest.raises(HostToolNotDeclared, match="@sandbox_tool"):
            registry.register(_pure)

    def test_the_gate_and_the_snapshot_are_one_read(self):
        """A stamp is an attribute, so it can be a property that answers once and then stops.
        Read twice, such a function passes the gate and registers as undeclared — the refusal
        this gate promises the host arriving instead as a sentence to the model at call time."""
        stamp = HostToolDeclaration(source=None, sink=None, identity=None)
        reads: list[int] = []

        class Flickering:
            def __call__(self) -> int:
                return 1

        def answer_once(self: object) -> HostToolDeclaration | None:
            reads.append(1)
            return stamp if len(reads) == 1 else None

        setattr(Flickering, FLOW_DECLARED_KEY, property(answer_once))

        registry = HostToolRegistry(require_declared=True)
        registry.register(Flickering(), name="flickering")

        assert len(reads) == 1, "the gate and the snapshot must be the same read"
        assert registry.declaration_for("flickering") == stamp

    def test_the_gate_admits_a_stamped_function(self):
        registry = HostToolRegistry(require_declared=True)
        registry.register(_stamped_pure())
        assert len(registry) == 1

    def test_with_the_gate_off_an_unstamped_function_registers(self):
        registry = HostToolRegistry()
        registry.register(_pure)
        assert registry.resolve("_pure") is _pure


class TestRegistrationNotice:
    def test_the_first_registration_warns_once(self, monkeypatch: pytest.MonkeyPatch):
        import warnings

        monkeypatch.setattr(host_tools_module._RegistrationNotice, "warned", False)
        registry = HostToolRegistry()
        with pytest.warns(MafSandboxHostToolsWarning, match="bypass the middleware chain"):
            registry.register(_stamped_pure(), name="first")
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            registry.register(_stamped_pure(), name="second")
        assert not any(isinstance(w.message, MafSandboxHostToolsWarning) for w in caught)

    def test_the_notice_points_at_the_host_that_registered(self, monkeypatch: pytest.MonkeyPatch):
        """A notice blaming this package's own frame tells the host nothing about where."""
        import warnings

        monkeypatch.setattr(host_tools_module._RegistrationNotice, "warned", False)
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            HostToolRegistry().register(_stamped_pure())
        assert len(caught) == 1
        assert caught[0].filename == __file__

    def test_the_notice_is_suppressible_by_category(self, monkeypatch: pytest.MonkeyPatch):
        import warnings

        monkeypatch.setattr(host_tools_module._RegistrationNotice, "warned", False)
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            warnings.filterwarnings("ignore", category=MafSandboxHostToolsWarning)
            HostToolRegistry().register(_stamped_pure())
        assert caught == []

    def test_registration_survives_dash_w_error(self, monkeypatch: pytest.MonkeyPatch):
        """The notice must never decide whether registering succeeds."""
        import warnings

        monkeypatch.setattr(host_tools_module._RegistrationNotice, "warned", False)
        registry = HostToolRegistry()
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            registry.register(_stamped_pure())
        assert len(registry) == 1


class TestAggregate:
    def test_an_empty_registry_has_no_opinion(self):
        aggregate = HostToolRegistry().aggregate()
        assert aggregate.result_integrity is None
        assert aggregate.outbound_caps == frozenset()
        assert aggregate.identities == frozenset()
        assert aggregate.requires_approval is False
        assert aggregate.has_undeclared is False

    def test_it_carries_both_ceilings_the_router_folds_against(self):
        """The bytes alone do not bound the transport: how many files it moves, and how many
        refusals nothing debits, are the dispatch bound's — so the surface carries it too."""
        limits = TransferLimits(max_bytes_per_file=64, max_total_bytes=128, max_files=2)
        aggregate = HostToolRegistry(
            response_limits=limits, max_host_tool_calls_per_run=7
        ).aggregate()
        assert aggregate.response_limits == limits
        assert aggregate.max_host_tool_calls_per_run == 7

    def test_result_integrity_is_the_weakest_over_sources(self):
        registry = HostToolRegistry()
        registry.register(
            sandbox_tool(source=SourceIntegrity.TRUSTED, sink=None, identity=None)(lambda: "docs"),
            name="docs",
        )
        registry.register(
            sandbox_tool(source=SourceIntegrity.UNTRUSTED, sink=None, identity=None)(lambda: "web"),
            name="web",
        )
        assert registry.aggregate().result_integrity is SourceIntegrity.UNTRUSTED

    def test_a_pure_or_sink_only_tool_does_not_drag_the_result(self):
        """The fold runs over *sources only* — that is the per-leg part of layer 4."""
        registry = HostToolRegistry()
        registry.register(
            sandbox_tool(source=SourceIntegrity.TRUSTED, sink=None, identity=None)(lambda: "docs"),
            name="docs",
        )
        registry.register(_stamped_pure(), name="pure")
        registry.register(
            sandbox_tool(source=None, sink="internal", identity=Identity.APP)(lambda note: None),
            name="audit",
        )
        assert registry.aggregate().result_integrity is SourceIntegrity.TRUSTED

    def test_sink_caps_are_collected_verbatim_and_never_folded(self):
        """Confidentiality is opaque host vocabulary: two distinct caps are both reported,
        because ranking them would need an ordering this package refuses to invent."""
        registry = HostToolRegistry()
        registry.register(
            sandbox_tool(source=None, sink="internal", identity=None)(lambda note: None),
            name="one",
        )
        registry.register(
            sandbox_tool(source=None, sink="secret", identity=None)(lambda note: None),
            name="two",
        )
        assert registry.aggregate().outbound_caps == frozenset({"internal", "secret"})

    def test_a_user_identity_tool_raises_the_surface_to_approval(self):
        registry = HostToolRegistry(allowed_identities=frozenset({Identity.APP, Identity.USER}))
        registry.register(
            sandbox_tool(source=None, sink=None, identity=Identity.USER)(lambda: None),
            name="as_user",
        )
        aggregate = registry.aggregate()
        assert aggregate.requires_approval is True
        assert Identity.USER in aggregate.identities

    def test_a_declared_none_identity_contributes_nothing(self):
        registry = HostToolRegistry()
        registry.register(_stamped_pure())
        assert registry.aggregate().identities == frozenset()

    def test_a_user_tool_is_refused_by_the_default_registry(self):
        """Secure by default: USER authority is not registrable without an opt-in."""
        registry = HostToolRegistry()
        with pytest.raises(HostToolIdentityNotAllowed, match="user"):
            registry.register(
                sandbox_tool(source=None, sink=None, identity=Identity.USER)(lambda: None),
                name="as_user",
            )

    def test_opting_in_allows_a_user_tool(self):
        registry = HostToolRegistry(allowed_identities=frozenset({Identity.APP, Identity.USER}))
        registry.register(
            sandbox_tool(source=None, sink=None, identity=Identity.USER)(lambda: None),
            name="as_user",
        )
        assert "as_user" in registry.names()

    def test_a_none_identity_tool_is_always_allowed_even_with_an_empty_set(self):
        """No-authority tools carry nothing to gate, so no set forbids them."""
        registry = HostToolRegistry(allowed_identities=frozenset())
        registry.register(_stamped_pure())
        assert len(registry) == 1

    def test_an_unstamped_tool_is_refused_when_app_is_not_allowed(self):
        """Unstamped reads as APP, so a set without APP refuses it — naming why."""
        registry = HostToolRegistry(allowed_identities=frozenset({Identity.USER}))
        with pytest.raises(HostToolIdentityNotAllowed, match="unstamped, read as 'app'"):
            registry.register(_pure)

    def test_allowed_identities_defaults_to_app_only(self):
        assert HostToolRegistry().allowed_identities == frozenset({Identity.APP})

    def test_allowed_identities_rejects_non_identity_members(self):
        with pytest.raises(TypeError, match="Identity"):
            HostToolRegistry(allowed_identities=frozenset({"app"}))  # type: ignore[arg-type]

    def test_an_undeclared_tool_fails_safe_on_every_leg(self):
        """Gate off: untrusted source, the APP authority it factually runs with, flagged."""
        registry = HostToolRegistry()
        registry.register(_pure)
        aggregate = registry.aggregate()
        assert aggregate.result_integrity is SourceIntegrity.UNTRUSTED
        assert aggregate.identities == frozenset({Identity.APP})
        assert aggregate.has_undeclared is True

    def test_a_stamp_removed_after_registration_does_not_reach_the_aggregate(self):
        """The claim is captured when the tool is registered, so removing it changes nothing.

        Stronger than catching the removal: there is no window in which the registry and the
        function disagree, so nothing has to notice.
        """
        registry = HostToolRegistry(require_declared=True)
        tool = _stamped_pure()
        registry.register(tool, name="doubled")
        delattr(tool, FLOW_DECLARED_KEY)
        aggregate = registry.aggregate()
        assert aggregate.has_undeclared is False
        assert registry.declaration_for("doubled") is not None

    def test_a_stamp_replaced_after_registration_does_not_reach_the_aggregate(self):
        """A swap is the dangerous half: a *complete* declaration passes every later gate."""
        registry = HostToolRegistry()
        tool = _stamped_pure()
        registry.register(tool, name="doubled")
        setattr(
            tool,
            FLOW_DECLARED_KEY,
            HostToolDeclaration(source=SourceIntegrity.UNTRUSTED, sink="secret", identity=None),
        )
        aggregate = registry.aggregate()
        assert aggregate.outbound_caps == frozenset(), "the swapped sink cap must not appear"
        assert aggregate.result_integrity is None, (
            "the registered declaration is a pure tool (source=None); the swapped UNTRUSTED "
            "source must not drag the result integrity down"
        )

    def test_taking_the_aggregate_seals_the_registry(self):
        """A host derives a spec from this; widening the surface afterwards must not be silent."""
        registry = HostToolRegistry()
        registry.register(_stamped_pure(), name="doubled")
        registry.aggregate()
        with pytest.raises(ValueError, match="sealed"):
            registry.register(_stamped_pure(), name="another")

    def test_the_sealed_surface_is_the_one_used_for_host_tool_calls(self):
        registry = HostToolRegistry()
        registry.register(_stamped_pure(), name="doubled")
        registry.aggregate()
        with pytest.raises(ValueError):
            registry.register(_stamped_pure(), name="late")
        assert registry.names() == frozenset({"doubled"})

    def test_the_identity_set_is_readable_only_through_the_sealing_aggregate(self):
        """A policy view that does not seal is a way around the seal: read an empty identity
        set, build a spec and a router that denies `Identity.APP` from it, then register an
        APP tool afterwards and call it past a deny list that never saw it."""
        registry = HostToolRegistry()
        assert not hasattr(registry, "identities")
        assert registry.aggregate().identities == frozenset()


class TestConcurrentHostToolCallsCannotOversubscribeTheLedger:
    """The tool body is the one place `call` awaits — which is long enough for a second
    host-tool call to walk past a ledger the first has not written to yet."""

    def test_the_second_of_two_in_flight_calls_is_refused_without_running(self):
        calls: list[int] = []
        entered = asyncio.Event()
        release = asyncio.Event()

        @sandbox_tool(source=None, sink="file_store", identity=None)
        async def slow(x: int) -> int:
            calls.append(x)
            entered.set()
            await release.wait()
            return x

        registry = HostToolRegistry(
            response_limits=dataclasses.replace(DEFAULT_TRANSFER_LIMITS, max_files=1)
        )
        registry.register(slow)
        run = HostToolRun(registry)

        async def scenario() -> tuple[HostToolCallResult, HostToolCallResult]:
            first = asyncio.create_task(run.call("slow", {"x": 1}))
            await entered.wait()  # the first call is inside its body, holding the one slot
            second = asyncio.create_task(run.call("slow", {"x": 2}))
            # A task and a bounded wait rather than a plain `await`: a regression here does
            # not refuse the second call, it runs its body — which blocks on `release`, and
            # awaiting it directly would deadlock the test instead of failing it.
            await asyncio.wait([second], timeout=5)
            release.set()
            return await first, await second

        first_result, second_result = asyncio.run(scenario())

        assert first_result.ok
        assert not second_result.ok
        assert second_result.refusal is not None
        assert "delivered-response cap" in second_result.refusal
        assert calls == [1], "the only slot was already spoken for; the second body must not run"

    def test_a_cancelled_call_gives_its_reserved_slot_back(self):
        """Cancellation is a `BaseException`, so it walks past any check on the outcome — and
        a run that loses a slot to one can never deliver the response it was holding."""
        entered = asyncio.Event()

        @sandbox_tool(source=None, sink="file_store", identity=None)
        async def never() -> int:
            entered.set()
            await asyncio.Event().wait()
            return 1

        registry = HostToolRegistry(
            response_limits=dataclasses.replace(DEFAULT_TRANSFER_LIMITS, max_files=1)
        )
        registry.register(never)
        registry.register(_stamped_pure(), name="doubled")
        run = HostToolRun(registry)

        async def scenario() -> HostToolCallResult:
            abandoned = asyncio.create_task(run.call("never"))
            await entered.wait()
            abandoned.cancel()
            await asyncio.wait([abandoned])
            assert abandoned.cancelled(), "the premise: the call was abandoned mid-body"
            return await run.call("doubled", {"x": 21})

        result = asyncio.run(scenario())
        assert result.ok, result.refusal

    def test_a_cancelled_host_tool_call_is_recorded(self, caplog: pytest.LogCaptureFixture):
        """#355: a cancel is the one outcome that otherwise leaves no trace — a refusal and a
        failure are logged, a success is deliberately quiet. It is logged, naming the tool, so a
        host that wired no `host_tool_calls_observer` still learns a sink may have fired; and the ledger
        stays consistent (the reserved slot goes back, the attempt still counts)."""
        entered = asyncio.Event()

        @sandbox_tool(source=None, sink="file_store", identity=None)
        async def never() -> int:
            entered.set()
            await asyncio.Event().wait()
            return 1

        registry = HostToolRegistry()
        registry.register(never)
        run = HostToolRun(registry)

        async def scenario() -> None:
            call = asyncio.create_task(run.call("never"))
            await entered.wait()
            call.cancel()
            await asyncio.wait([call])
            assert call.cancelled(), "the premise: the call was cancelled mid-body"

        with caplog.at_level(logging.WARNING, logger="maf_sandbox._host_tools"):
            asyncio.run(scenario())

        assert any(
            "cancelled mid-effect" in record.getMessage() and "never" in record.getMessage()
            for record in caplog.records
        ), "a cancelled host-tool call must leave a record naming the tool"
        assert run._calls == 1, "the attempt is counted"
        assert run._delivered == 0, "the reserved slot was returned"


class TestACeilingMustBeAbleToCompare:
    """A non-integer ceiling removes itself, which is the one thing a safety cap must not do."""

    @pytest.mark.parametrize("bad", [float("nan"), float("inf"), 2.5, True, "8", None])
    def test_the_host_tool_call_cap_refuses_anything_but_a_plain_integer(self, bad: object):
        with pytest.raises(TypeError, match="max_host_tool_calls_per_run"):
            HostToolRegistry(max_host_tool_calls_per_run=bad)  # type: ignore[arg-type]

    @pytest.mark.parametrize("leg", ["max_bytes_per_file", "max_total_bytes", "max_files"])
    @pytest.mark.parametrize("bad", [float("nan"), float("inf"), True])
    def test_each_response_ceiling_refuses_anything_but_a_plain_integer(
        self, leg: str, bad: object
    ):
        limits = dataclasses.replace(DEFAULT_TRANSFER_LIMITS, **{leg: bad})
        with pytest.raises(TypeError, match=leg):
            HostToolRegistry(response_limits=limits)

    def test_nan_would_otherwise_pass_every_comparison(self):
        """Why this is a type check and not a range check: NaN satisfies both bounds."""
        nan = float("nan")
        assert not nan < 1 and not nan > 1

    @pytest.mark.parametrize("leg", ["max_bytes_per_file", "max_total_bytes", "max_files"])
    @pytest.mark.parametrize("bound", [-1, 0])
    def test_a_response_ceiling_that_could_carry_nothing_is_refused(self, leg: str, bound: int):
        """Zero as well as negative: the smallest JSON value is one byte, so a zero on any leg
        is a registry that can never deliver — an empty one, reached the expensive way."""
        limits = dataclasses.replace(DEFAULT_TRANSFER_LIMITS, **{leg: bound})
        with pytest.raises(ValueError, match=leg):
            HostToolRegistry(response_limits=limits)


class TestTheResponseLedgerIsCheckedBeforeTheSideEffect:
    """A sink tool's body runs in the host process; refusing after it has run is too late."""

    def test_an_exhausted_ledger_refuses_without_calling_the_tool(self):
        calls: list[int] = []

        @sandbox_tool(source=None, sink="file_store", identity=None)
        def writes(x: int) -> int:
            calls.append(x)
            return x

        registry = HostToolRegistry(
            response_limits=dataclasses.replace(DEFAULT_TRANSFER_LIMITS, max_files=1)
        )
        registry.register(writes, name="writes")
        run = HostToolRun(registry)

        assert _call_host_tool(run, "writes", {"x": 1}).ok
        second = _call_host_tool(run, "writes", {"x": 2})

        assert not second.ok
        assert second.refusal is not None and "delivered-response cap" in second.refusal
        assert calls == [1], "the second call must not have reached the tool body"

    def test_an_exhausted_byte_budget_refuses_without_calling_the_tool(self):
        """The count leg is not the only one knowable before a size: once the run's total is
        spent, no response of any size fits, and running the body only spends the effect."""
        calls: list[int] = []

        @sandbox_tool(source=None, sink="file_store", identity=None)
        def writes(x: int) -> str:
            calls.append(x)
            return "y" * 20

        registry = HostToolRegistry(
            response_limits=TransferLimits(max_bytes_per_file=64, max_total_bytes=22, max_files=8)
        )
        registry.register(writes, name="writes")
        run = HostToolRun(registry)

        assert _call_host_tool(run, "writes", {"x": 1}).ok  # 22 bytes: the budget, exactly
        second = _call_host_tool(run, "writes", {"x": 2})

        assert not second.ok
        assert second.refusal is not None and "byte budget" in second.refusal
        assert calls == [1], "nothing could have been delivered, so nothing should have run"

    def test_a_cap_the_framing_cannot_fit_refuses_without_calling_the_tool(self):
        """A per-response cap smaller than the transport's envelope can carry no response.

        Knowable before the call, like the two above, and reachable in ordinary
        configuration: the registry only refuses a cap below one byte, so a five-byte cap is
        accepted and then a transport's framing puts every value out of reach. Running the
        body first would mean a sink acting in the host process for a result the run can
        never hand back.
        """
        calls: list[int] = []

        @sandbox_tool(source=None, sink="file_store", identity=None)
        def writes(x: int) -> int:
            calls.append(x)
            return 1

        registry = HostToolRegistry(
            response_limits=TransferLimits(max_bytes_per_file=5, max_total_bytes=4096, max_files=8)
        )
        registry.register(writes, name="writes")

        refused = asyncio.run(HostToolRun(registry).call("writes", {"x": 1}, framing_bytes=11))

        assert not refused.ok
        assert refused.refusal is not None and "per-response cap" in refused.refusal
        assert calls == [], "the tool ran for a response that could never be delivered"

    def test_a_negative_framing_allowance_is_a_programming_error(self):
        """It would widen every ceiling beneath it, so it raises rather than refusing."""
        registry = HostToolRegistry()
        registry.register(sandbox_tool(source=None, sink=None, identity=None)(lambda: 1), name="f")
        with pytest.raises(ValueError, match="framing_bytes"):
            asyncio.run(HostToolRun(registry).call("f", None, framing_bytes=-1))

    @pytest.mark.parametrize("allowance", [float("nan"), float("inf"), 2.5, True, "8", None])
    def test_a_framing_allowance_that_is_not_a_plain_integer_is_refused(self, allowance: object):
        """A range check cannot catch these, and the caps are what they would switch off.

        `nan` is the one that matters: it passes `< 0`, compares false against every ceiling
        it is added to, and then lands in `_delivered_bytes`, where it disables the run's byte
        accounting for the rest of the run. The others are refused for ordinary reasons.
        """
        registry = HostToolRegistry()
        registry.register(sandbox_tool(source=None, sink=None, identity=None)(lambda: 1), name="f")
        with pytest.raises(TypeError, match="framing_bytes"):
            asyncio.run(HostToolRun(registry).call("f", None, framing_bytes=allowance))  # type: ignore[arg-type]

    def test_a_nan_framing_allowance_cannot_disable_the_byte_ledger(self):
        """The consequence, not the type: a run that admitted one would stop capping anything.

        Sized so the second call must be refused. If `nan` reached `_delivered_bytes` the
        first call would poison it and every later comparison would be false — the cap gone
        for good rather than for one call.
        """
        registry = HostToolRegistry(
            response_limits=TransferLimits(max_bytes_per_file=64, max_total_bytes=22, max_files=8)
        )
        registry.register(
            sandbox_tool(source=None, sink=None, identity=None)(lambda: "y" * 20), name="f"
        )
        run = HostToolRun(registry)

        with pytest.raises(TypeError):
            asyncio.run(run.call("f", None, framing_bytes=float("nan")))  # type: ignore[arg-type]

        assert _call_host_tool(run, "f").ok, "the refused call spent the budget"
        exhausted = _call_host_tool(run, "f")
        assert not exhausted.ok, "the byte ledger stopped counting"
        assert exhausted.refusal is not None and "byte budget" in exhausted.refusal


class TestHostToolCallResult:
    def test_exactly_one_side_must_be_set(self):
        with pytest.raises(ValueError):
            HostToolCallResult()
        with pytest.raises(ValueError):
            HostToolCallResult(value_json="1", refusal="Error: no")
        assert HostToolCallResult(value_json="1").ok is True
        assert HostToolCallResult(refusal="Error: no").ok is False


def _call_host_tool(run: HostToolRun, name: str, arguments=None) -> HostToolCallResult:
    return asyncio.run(run.call(name, arguments))


def _nesting_json_refuses_to_encode() -> list[object]:
    """A structure whose only problem is depth, found rather than assumed.

    How deep it takes is the platform's business — CPython's guard is a C-recursion budget,
    and a depth that raises on Windows encodes happily on Linux — so a hard-coded number
    would pin one runner and quietly prove nothing on the other.
    """
    for depth in (2_000, 12_000, 60_000):
        deep: list[object] = []
        node = deep
        for _ in range(depth):
            child: list[object] = []
            node.append(child)
            node = child
        try:
            json.dumps(deep)
        except RecursionError:
            return deep
    raise AssertionError("json.dumps encoded 60k levels of nesting without a RecursionError")


class TestHostToolCall:
    def test_a_registered_tool_round_trips_its_value_as_json(self):
        registry = HostToolRegistry()
        registry.register(_stamped_pure(), name="doubled")
        result = _call_host_tool(HostToolRun(registry), "doubled", {"x": 21})
        assert result.ok
        assert result.value_json is not None
        assert json.loads(result.value_json) == 42

    def test_an_async_tool_is_awaited(self):
        @sandbox_tool(source=None, sink=None, identity=None)
        async def fetch(x: int) -> dict[str, int]:
            return {"x": x}

        registry = HostToolRegistry()
        registry.register(fetch)
        result = _call_host_tool(HostToolRun(registry), "fetch", {"x": 7})
        assert result.ok
        assert result.value_json is not None
        assert json.loads(result.value_json) == {"x": 7}

    def test_an_unregistered_name_is_unreachable(self):
        """The one-door pin: a host function that exists but was never registered has no
        path in — resolution goes exclusively through the registry."""
        registry = HostToolRegistry()
        registry.register(_stamped_pure(), name="doubled")
        result = _call_host_tool(HostToolRun(registry), "_pure", {"x": 1})
        assert not result.ok
        assert result.refusal == "Error: '_pure' is not a registered host tool"

    def test_an_unhashable_name_is_refused_not_raised(self):
        """A transport hands over whatever the guest's JSON parsed to, and a name that came
        as an array cannot be a dict key at all — the lookup raises before any refusal."""
        with pytest.raises(TypeError):
            {}.get(["doubled"])  # type: ignore[arg-type]  # the premise

        registry = HostToolRegistry()
        registry.register(_stamped_pure(), name="doubled")
        result = _call_host_tool(HostToolRun(registry), ["doubled"])  # type: ignore[arg-type]
        assert not result.ok
        assert result.refusal == "Error: a host tool name must be a string, not list"

    @pytest.mark.parametrize(
        ("name", "arguments"),
        [
            ("z" * 100_000, None),
            ("doubled", {"x": 1, "z" * 100_000: 1}),
        ],
        ids=["the name that did not resolve", "the keyword that did not bind"],
    )
    def test_a_refusal_never_grows_with_the_text_the_guest_sent(self, name: str, arguments):
        """Refusals are the one response `TransferLimits` never sees — it bounds what a tool
        delivered, and a refusal delivered nothing — so anything they quote is a payload the
        guest sizes. 100 KB in, a sentence out.

        The second case satisfies `x` deliberately: `bind` reports a missing argument before
        an unexpected one, so a call that omits it never reaches the guest-chosen keyword."""
        registry = HostToolRegistry()
        registry.register(_stamped_pure(), name="doubled")
        result = _call_host_tool(HostToolRun(registry), name, arguments)
        assert not result.ok
        assert result.refusal is not None
        assert len(result.refusal) < 400, "the guest chose the length of this refusal"
        assert "truncated" in result.refusal

    def test_the_cap_returns_a_sanitized_refusal_rather_than_raising(self):
        registry = HostToolRegistry(max_host_tool_calls_per_run=2)
        registry.register(_stamped_pure(), name="doubled")
        run = HostToolRun(registry)
        assert _call_host_tool(run, "doubled", {"x": 1}).ok
        assert _call_host_tool(run, "doubled", {"x": 2}).ok
        third = _call_host_tool(run, "doubled", {"x": 3})
        assert not third.ok
        assert third.refusal is not None and "host-tool-call cap (2) is exhausted" in third.refusal

    def test_refused_attempts_burn_the_cap_too(self):
        """A guest probing unknown names is spending exactly the budget the cap bounds."""
        registry = HostToolRegistry(max_host_tool_calls_per_run=2)
        registry.register(_stamped_pure(), name="doubled")
        run = HostToolRun(registry)
        _call_host_tool(run, "nope")
        _call_host_tool(run, "nope")
        third = _call_host_tool(run, "doubled", {"x": 1})
        assert not third.ok
        assert third.refusal is not None and "exhausted" in third.refusal

    def test_a_fresh_run_starts_with_a_fresh_count(self):
        registry = HostToolRegistry(max_host_tool_calls_per_run=1)
        registry.register(_stamped_pure(), name="doubled")
        assert _call_host_tool(HostToolRun(registry), "doubled", {"x": 1}).ok
        assert _call_host_tool(HostToolRun(registry), "doubled", {"x": 1}).ok

    def test_arguments_are_validated_host_side_at_the_one_door(self):
        registry = HostToolRegistry()
        registry.register(_stamped_pure(), name="doubled")
        result = _call_host_tool(HostToolRun(registry), "doubled", {"y": 1})
        assert not result.ok
        assert result.refusal is not None and "do not bind" in result.refusal
        # The diagnostic is the point of quoting the error at all — a model fixes its own
        # call from it. `bind` reports the missing argument before it notices the extra one.
        assert "missing a required argument" in result.refusal

        named = _call_host_tool(HostToolRun(registry), "doubled", {"x": 1, "y": 2})
        assert named.refusal is not None and "unexpected keyword argument 'y'" in named.refusal

    def test_non_mapping_arguments_are_refused(self):
        registry = HostToolRegistry()
        registry.register(_stamped_pure(), name="doubled")
        result = _call_host_tool(HostToolRun(registry), "doubled", [1, 2])  # type: ignore[arg-type]
        assert not result.ok
        assert result.refusal is not None and "JSON object" in result.refusal

    def test_a_user_identity_tool_is_refused_where_the_host_mints_nothing(self):
        """Declarable without a minter, and the refusal names the one thing serving takes."""

        @sandbox_tool(source=None, sink=None, identity=Identity.USER)
        def as_user() -> str:
            return "never"

        registry = HostToolRegistry(allowed_identities=frozenset({Identity.APP, Identity.USER}))
        registry.register(as_user)
        result = _call_host_tool(HostToolRun(registry), "as_user")
        assert not result.ok
        assert result.refusal is not None
        assert "mint_user_identity" in result.refusal


def _as_user_tool():
    """A USER-identity tool that reports the authority it was handed."""

    @sandbox_tool(source=None, sink=None, identity=Identity.USER)
    def whoami(user_identity: str) -> str:
        return f"acting as {user_identity}"

    return whoami


def _minting_registry(mint=None, **overrides) -> HostToolRegistry:
    async def _mint(run_id: str) -> str:
        return f"token-for-{run_id}"

    return HostToolRegistry(
        allowed_identities=frozenset({Identity.APP, Identity.USER}),
        mint_user_identity=_mint if mint is None else mint,
        **overrides,
    )


class TestServingTheUsersIdentity:
    """A `USER` tool is callable where the registry mints the authority, and refused where not.

    One run holds one identity, and a guest cannot choose the one its own call runs under.
    """

    def test_a_minted_identity_reaches_the_body(self):
        registry = _minting_registry()
        registry.register(_as_user_tool())

        result = _call_host_tool(HostToolRun(registry, run_id="run-7"), "whoami")

        assert result.ok, result.refusal
        assert result.value_json == '"acting as token-for-run-7"'

    def test_the_identity_is_minted_once_for_the_run(self):
        """One run, one identity: the bound the whole mechanism rests on."""
        minted: list[str] = []

        async def _mint(run_id: str) -> str:
            minted.append(run_id)
            return f"token-{len(minted)}"

        registry = _minting_registry(_mint)
        registry.register(_as_user_tool())
        run = HostToolRun(registry, run_id="run-7")

        first = _call_host_tool(run, "whoami")
        second = _call_host_tool(run, "whoami")

        assert minted == ["run-7"]
        assert first.value_json == second.value_json == '"acting as token-1"'

    def test_overlapping_calls_share_the_one_identity(self):
        """Calls of one run may overlap, and two of them must not mint two authorities.

        The mint is awaited, so without serialization both callers find the cache empty
        before either finishes and the run ends up with two.
        """
        minted: list[str] = []

        async def _mint(run_id: str) -> str:
            minted.append(run_id)
            await asyncio.sleep(0)  # let the other caller reach the same empty cache
            return f"token-{len(minted)}"

        registry = _minting_registry(_mint)
        registry.register(_as_user_tool())
        run = HostToolRun(registry, run_id="run-7")

        async def _both():
            return await asyncio.gather(run.call("whoami"), run.call("whoami"))

        first, second = asyncio.run(_both())

        assert minted == ["run-7"], f"minted {len(minted)} times for one run"
        assert first.value_json == second.value_json == '"acting as token-1"'

    def test_a_cancel_while_minting_is_recorded(self, caplog):
        """The record `call` promises (#355), at an await the mint added."""
        started = asyncio.Event()

        async def _mint(run_id: str) -> str:
            started.set()
            await asyncio.sleep(3600)
            return "never"  # pragma: no cover - the sleep is cancelled first

        registry = _minting_registry(_mint)
        registry.register(_as_user_tool())
        run = HostToolRun(registry, run_id="run-7")

        async def _cancel_mid_mint():
            call = asyncio.ensure_future(run.call("whoami"))
            await started.wait()
            call.cancel()
            with pytest.raises(asyncio.CancelledError):
                await call

        with caplog.at_level(logging.WARNING, logger="maf_sandbox._host_tools"):
            asyncio.run(_cancel_mid_mint())

        assert [r for r in caplog.records if "was cancelled" in r.getMessage()], caplog.text

    def test_a_rejected_mint_is_logged_by_type_rather_than_value(self, caplog):
        """A misconfigured minter answering with `bytes` is answering with a real token."""

        async def _mint(run_id: str):
            return b"super-secret-token"

        registry = _minting_registry(_mint)
        registry.register(_as_user_tool())

        with caplog.at_level(logging.WARNING, logger="maf_sandbox._host_tools"):
            result = _call_host_tool(HostToolRun(registry), "whoami")

        assert not result.ok
        assert "super-secret-token" not in caplog.text, caplog.text
        assert "bytes" in caplog.text, caplog.text

    def test_each_run_mints_its_own(self):
        registry = _minting_registry()
        registry.register(_as_user_tool())

        one = _call_host_tool(HostToolRun(registry, run_id="run-a"), "whoami")
        two = _call_host_tool(HostToolRun(registry, run_id="run-b"), "whoami")

        assert one.value_json == '"acting as token-for-run-a"'
        assert two.value_json == '"acting as token-for-run-b"'

    def test_a_guest_cannot_choose_the_authority_it_runs_under(self):
        """The refusal a spoofing attempt gets, rather than a silent overwrite."""
        registry = _minting_registry()
        registry.register(_as_user_tool())

        result = _call_host_tool(
            HostToolRun(registry), "whoami", {"user_identity": "token-i-picked"}
        )

        assert not result.ok
        assert result.refusal is not None and "not an argument a caller may send" in result.refusal

    def test_a_spoofed_argument_is_refused_before_anything_is_minted(self):
        """Refused before the mint, so an attempt costs the host no token at all."""
        minted: list[str] = []

        async def _mint(run_id: str) -> str:
            minted.append(run_id)
            return "token"

        registry = _minting_registry(_mint)
        registry.register(_as_user_tool())

        _call_host_tool(HostToolRun(registry), "whoami", {"user_identity": "mine"})

        assert minted == []

    def test_a_mint_that_raises_refuses_the_call(self):
        async def _mint(run_id: str) -> str:
            raise RuntimeError("the token service said no")

        registry = _minting_registry(_mint)
        registry.register(_as_user_tool())

        result = _call_host_tool(HostToolRun(registry), "whoami")

        assert not result.ok
        assert result.refusal is not None and "could not be minted" in result.refusal
        assert "token service" not in result.refusal, "the host's detail stays in the host's log"

    def test_a_mint_that_answers_with_nothing_usable_refuses_too(self):
        """An empty string reaching a body as its authority is a call with none, unnoticed."""

        async def _mint(run_id: str) -> str:
            return ""

        registry = _minting_registry(_mint)
        registry.register(_as_user_tool())

        result = _call_host_tool(HostToolRun(registry), "whoami")

        assert not result.ok
        assert result.refusal is not None and "could not be minted" in result.refusal

    def test_a_failed_mint_is_not_cached(self):
        """A transient the next call may survive, unlike a success, which must not repeat."""
        attempts: list[str] = []

        async def _mint(run_id: str) -> str:
            attempts.append(run_id)
            if len(attempts) == 1:
                raise RuntimeError("transient")
            return "token-2"

        registry = _minting_registry(_mint)
        registry.register(_as_user_tool())
        run = HostToolRun(registry, run_id="run-7")

        first = _call_host_tool(run, "whoami")
        second = _call_host_tool(run, "whoami")

        assert not first.ok
        assert second.ok, second.refusal
        assert second.value_json == '"acting as token-2"'

    def test_a_user_tool_that_cannot_receive_the_identity_is_refused_at_registration(self):
        """At the host's own configuration site, where the fix is one parameter away."""

        @sandbox_tool(source=None, sink=None, identity=Identity.USER)
        def as_user() -> str:
            return "never"

        with pytest.raises(ValueError, match="user_identity"):
            _minting_registry().register(as_user)

    def test_kwargs_counts_as_receiving_it(self):
        """A body may fan its arguments out rather than name each one."""

        @sandbox_tool(source=None, sink=None, identity=Identity.USER)
        def as_user(**given: object) -> str:
            return f"acting as {given['user_identity']}"

        registry = _minting_registry()
        registry.register(as_user)

        result = _call_host_tool(HostToolRun(registry, run_id="run-7"), "as_user")

        assert result.ok, result.refusal
        assert result.value_json == '"acting as token-for-run-7"'

    def test_a_tool_of_another_identity_is_never_handed_one(self):
        """The injection is the USER leg's, not every tool's."""
        registry = _minting_registry()
        registry.register(_stamped_pure(), name="doubled")

        result = _call_host_tool(HostToolRun(registry), "doubled", {"x": 2})

        assert result.ok, result.refusal
        assert result.value_json == "4"

    def test_a_minter_that_is_not_callable_is_refused_at_construction(self):
        with pytest.raises(TypeError, match="mint_user_identity must be callable"):
            HostToolRegistry(mint_user_identity="a token")  # type: ignore[arg-type]

    def test_a_host_tool_call_uses_the_declaration_registration_captured(self):
        """A stamp removed after registration neither refuses nor widens: it is not read."""
        registry = HostToolRegistry(require_declared=True)
        tool = _stamped_pure()
        registry.register(tool, name="doubled")
        delattr(tool, FLOW_DECLARED_KEY)
        result = _call_host_tool(HostToolRun(registry), "doubled", {"x": 1})
        assert result.ok, "the registered claim stands; the attribute is no longer consulted"

    def test_an_identity_swapped_in_after_registration_cannot_reach_the_host_tool_call(self):
        """The swap that matters: USER authority arriving after the aggregate was derived."""
        registry = HostToolRegistry()
        tool = _stamped_pure()
        registry.register(tool, name="doubled")
        setattr(
            tool,
            FLOW_DECLARED_KEY,
            HostToolDeclaration(source=None, sink=None, identity=Identity.USER),
        )
        result = _call_host_tool(HostToolRun(registry), "doubled", {"x": 1})
        assert result.ok, "the call reads the registered declaration, not the current one"


class TestHostToolCallFailureLadder:
    def test_a_raising_tool_sends_the_detail_to_the_log_not_the_guest(
        self, caplog: pytest.LogCaptureFixture
    ):
        @sandbox_tool(source=None, sink=None, identity=None)
        def broken() -> None:
            raise RuntimeError("endpoint https://internal.example refused the token")

        registry = HostToolRegistry()
        registry.register(broken)
        with caplog.at_level(logging.WARNING):
            result = _call_host_tool(HostToolRun(registry), "broken")
        assert not result.ok
        assert (
            result.refusal == "Error: host tool 'broken' failed — the reason is in the host's log"
        )
        assert "internal.example" not in result.refusal
        assert "internal.example" in caplog.text

    def test_a_callable_without_a_signature_is_refused_not_raised(
        self, caplog: pytest.LogCaptureFixture
    ):
        """`register` takes any callable, and some built-ins expose no signature to read —
        that must follow the failure ladder rather than escaping `call`.

        `max` is one such built-in today; the assertion below pins the premise, so this test
        fails loudly if CPython ever gives it a text signature rather than passing vacuously.
        """
        with pytest.raises(ValueError):
            inspect.signature(max)

        registry = HostToolRegistry()
        registry.register(max, name="largest")
        with caplog.at_level(logging.WARNING):
            result = _call_host_tool(HostToolRun(registry), "largest", {"iterable": [1, 2]})
        assert not result.ok
        assert result.refusal is not None and "signature could not be read" in result.refusal
        assert "largest" in caplog.text

    def test_a_signature_that_raises_is_refused_not_raised(self, caplog: pytest.LogCaptureFixture):
        """The other half of the same door: not "no signature" but one whose *retrieval* fails.
        A `__signature__` property can raise anything, and `inspect.signature` passes it on."""

        class Proxy:
            @property
            def __signature__(self) -> inspect.Signature:
                raise RuntimeError("upstream https://internal.example is unreachable")

            def __call__(self) -> int:
                return 1

        proxy = Proxy()
        with pytest.raises(RuntimeError):
            inspect.signature(proxy)

        registry = HostToolRegistry()
        registry.register(proxy, name="proxied")
        with caplog.at_level(logging.WARNING):
            result = _call_host_tool(HostToolRun(registry), "proxied")
        assert not result.ok
        assert result.refusal is not None and "signature could not be read" in result.refusal
        assert "internal.example" not in result.refusal
        assert "internal.example" in caplog.text

    @pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
    def test_a_non_finite_float_is_refused_rather_than_delivered_unparseable(self, value: float):
        """Python's default `json.dumps` emits bare NaN/Infinity, which strict parsers on the
        guest side reject — delivering that as success hands over a payload nothing can read."""

        @sandbox_tool(source=None, sink=None, identity=None)
        def measure() -> float:
            return value

        registry = HostToolRegistry()
        registry.register(measure)
        result = _call_host_tool(HostToolRun(registry), "measure")
        assert not result.ok
        assert result.refusal is not None and "cannot be carried as JSON" in result.refusal

    def test_an_unserializable_value_is_refused(self):
        @sandbox_tool(source=None, sink=None, identity=None)
        def opaque() -> object:
            return object()

        registry = HostToolRegistry()
        registry.register(opaque)
        result = _call_host_tool(HostToolRun(registry), "opaque")
        assert not result.ok
        assert result.refusal is not None and "cannot be carried as JSON" in result.refusal

    def test_a_lone_surrogate_is_refused_rather_than_escaping_the_ladder(self):
        """`ensure_ascii=False` leaves the surrogate in the text and serializes happily; the
        *encode* is what raises, which is why it has to sit inside the guard, not after it."""
        with pytest.raises(UnicodeEncodeError):
            json.dumps("\ud800", ensure_ascii=False).encode("utf-8")

        @sandbox_tool(source=None, sink=None, identity=None)
        def half_a_pair() -> str:
            return "\ud800"

        registry = HostToolRegistry()
        registry.register(half_a_pair)
        result = _call_host_tool(HostToolRun(registry), "half_a_pair")
        assert not result.ok
        assert result.refusal is not None and "cannot be carried as JSON" in result.refusal

    def test_a_deeply_nested_result_is_refused_rather_than_escaping_the_ladder(self):
        """`RecursionError` is not a `ValueError`, so the narrow guard let it past — and it
        arrives before any byte cap has been consulted, since nothing was encoded at all."""
        deep = _nesting_json_refuses_to_encode()

        @sandbox_tool(source=None, sink=None, identity=None)
        def nested() -> list[object]:
            return deep

        registry = HostToolRegistry()
        registry.register(nested)
        result = _call_host_tool(HostToolRun(registry), "nested")
        assert not result.ok
        assert result.refusal is not None and "cannot be carried as JSON" in result.refusal


class TestResponseCaps:
    def _registry(self, **limit_overrides) -> HostToolRegistry:
        limits = TransferLimits(
            max_bytes_per_file=limit_overrides.get("max_bytes_per_file", 64),
            max_total_bytes=limit_overrides.get("max_total_bytes", 128),
            max_files=limit_overrides.get("max_files", 8),
        )
        registry = HostToolRegistry(response_limits=limits)

        @sandbox_tool(source=None, sink=None, identity=None)
        def payload(size: int) -> str:
            return "x" * size

        registry.register(payload)
        return registry

    def test_a_response_over_the_per_response_cap_is_refused(self):
        result = _call_host_tool(HostToolRun(self._registry()), "payload", {"size": 100})
        assert not result.ok
        assert result.refusal is not None and "per-response cap allows 64" in result.refusal

    def test_the_run_total_is_a_running_ledger(self):
        run = HostToolRun(self._registry())
        assert _call_host_tool(run, "payload", {"size": 60}).ok
        assert _call_host_tool(run, "payload", {"size": 60}).ok
        third = _call_host_tool(run, "payload", {"size": 60})
        assert not third.ok
        assert third.refusal is not None and "total response cap" in third.refusal

    def test_the_delivered_response_count_is_capped(self):
        run = HostToolRun(self._registry(max_files=1, max_total_bytes=10_000))
        assert _call_host_tool(run, "payload", {"size": 1}).ok
        second = _call_host_tool(run, "payload", {"size": 1})
        assert not second.ok
        assert second.refusal is not None and "delivered-response cap (1)" in second.refusal

    def test_a_refused_response_does_not_spend_the_ledger(self):
        """Over-cap bytes are never delivered, so they must not count as if they were."""
        run = HostToolRun(self._registry())
        assert not _call_host_tool(run, "payload", {"size": 100}).ok
        assert _call_host_tool(run, "payload", {"size": 60}).ok

    def test_a_refusal_gives_its_reserved_response_slot_back(self):
        """The count slot is taken before the call so it can be held across it; a run that
        once overran the per-response cap must not be one response poorer forever after."""
        run = HostToolRun(self._registry(max_files=1, max_total_bytes=10_000))
        assert not _call_host_tool(run, "payload", {"size": 100}).ok
        assert _call_host_tool(run, "payload", {"size": 1}).ok


class TestTheRegistryObservesEveryHostToolCall:
    """Verify host-tool-call attribution and observer lifecycle behavior."""

    @staticmethod
    def _recording(events: list[tuple[str, object, HostToolRun, object]]):
        def factory(run: HostToolRun, name: object):
            # A token unique to this context: a run is not one, and the pairing must hold
            # against a regression that opens the same host-tool call's context twice.
            token = object()

            @contextlib.contextmanager
            def cm():
                # Recorded when ``__enter__`` runs, not when the factory was called: a
                # factory whose context is never entered is not observation.
                events.append(("enter", token, run, name))
                try:
                    yield
                finally:
                    # `finally`, because on a failing or cancelled host-tool call the exit is a
                    # throw into the yield, and a line after it would never run.
                    events.append(("exit", token, run, name))

            return cm()

        return factory

    def _pairing(self, events: list[tuple[str, object, HostToolRun, object]]) -> None:
        """Each context enters and exits exactly once — no LIFO, because two contexts of
        different runs may legally overlap."""
        states: dict[object, str] = {}
        for kind, token, run, _ in events:
            state = states.get(token)
            if kind == "enter":
                assert state is None, "an enter for a context already entered"
                states[token] = "open"
            else:
                assert state == "open", f"an exit before the enter, or twice: {run!r}"
                states[token] = "closed"
        assert all(state == "closed" for state in states.values()), (
            "an enter the host-tool call never closed"
        )

    def test_the_observer_is_absent_by_default(self):
        """No host, no observer: the property is ``None`` and a call is exactly today's."""
        registry = HostToolRegistry()
        assert registry.host_tool_calls_observer is None
        registry.register(_stamped_pure())
        result = _call_host_tool(HostToolRun(registry), "doubled", {"x": 21})
        assert result.ok

    def test_one_run_per_program_and_the_name_of_the_call(self):
        """Two programs on one host: the run object is what a ledger attributes a call by."""
        events: list[tuple[str, object, HostToolRun, object]] = []
        registry = HostToolRegistry(host_tool_calls_observer=self._recording(events))
        registry.register(_stamped_pure())
        run_a = HostToolRun(registry)
        run_b = HostToolRun(registry)
        assert _call_host_tool(run_a, "doubled", {"x": 1}).ok
        assert _call_host_tool(run_b, "doubled", {"x": 2}).ok
        assert [(kind, run is run_a, name) for kind, _, run, name in events] == [
            ("enter", True, "doubled"),
            ("exit", True, "doubled"),
            ("enter", False, "doubled"),
            ("exit", False, "doubled"),
        ]
        self._pairing(events)

    def test_the_observer_can_attribute_a_call_by_run_id(self):
        """#446's point: two programs' calls are told apart by the run's own ``run_id`` — a
        documented, loggable identity — not by the object the observer happens to be handed."""
        events: list[tuple[str, object, HostToolRun, object]] = []
        registry = HostToolRegistry(host_tool_calls_observer=self._recording(events))
        registry.register(_stamped_pure())
        run_a = HostToolRun(registry, run_id="prog-a")
        run_b = HostToolRun(registry)  # a generated identity, distinct from prog-a
        assert _call_host_tool(run_a, "doubled", {"x": 1}).ok
        assert _call_host_tool(run_b, "doubled", {"x": 2}).ok
        entered = [run.run_id for kind, _, run, _ in events if kind == "enter"]
        assert entered == ["prog-a", run_b.run_id]
        assert run_b.run_id != "prog-a"

    def test_a_refused_host_tool_call_is_observed_too(self):
        """A refusal is a call the host still ran, so it is observed: a cap refusal returns
        before any body runs, and without the enter/exit pair nothing else names the run."""
        events: list[tuple[str, object, HostToolRun, object]] = []
        registry = HostToolRegistry(
            max_host_tool_calls_per_run=1,
            host_tool_calls_observer=self._recording(events),
        )
        registry.register(_stamped_pure())
        run = HostToolRun(registry)
        assert _call_host_tool(run, "doubled", {"x": 1}).ok
        assert not _call_host_tool(run, "doubled", {"x": 2}).ok
        self._pairing(events)
        assert [kind for kind, *_ in events] == ["enter", "exit", "enter", "exit"]
        assert all(event_run is run for _, _, event_run, _ in events)

    def test_the_observer_sees_an_unresolved_name(self):
        """A name that never resolves is observed too, and the unhashable refusal is reachable."""
        events: list[tuple[str, object, HostToolRun, object]] = []
        registry = HostToolRegistry(host_tool_calls_observer=self._recording(events))
        run = HostToolRun(registry)
        unknown = _call_host_tool(run, "never_registered")
        assert not unknown.ok
        assert unknown.refusal is not None and "not a registered host tool" in unknown.refusal
        self._pairing(events)
        assert [kind for kind, *_ in events] == ["enter", "exit"]
        assert events[0][3] == "never_registered"

    def test_an_unstring_name_is_seen_as_given(self):
        """The name is handed over as given: a non-string appears only on the refusal that ends it."""
        events: list[tuple[str, object, HostToolRun, object]] = []
        registry = HostToolRegistry(host_tool_calls_observer=self._recording(events))
        run = HostToolRun(registry)
        not_a_name = ["doubled"]
        refused = _call_host_tool(run, not_a_name)  # type: ignore[arg-type]
        assert not refused.ok
        self._pairing(events)
        assert events[0][2:] == (run, not_a_name)

    def test_a_byte_budget_refusal_is_observed_and_never_reaches_the_body(self):
        """A byte-budget refusal is decided before the body runs, so the observer is the only
        thing that saw it — and the body spy is the check that it really never ran."""
        events: list[tuple[str, object, HostToolRun, object]] = []
        body_calls: list[int] = []
        # The budget is spent by the framing alone, so a one-byte value does not even matter.
        registry = HostToolRegistry(
            response_limits=TransferLimits(max_bytes_per_file=64, max_total_bytes=5, max_files=8),
            host_tool_calls_observer=self._recording(events),
        )

        @sandbox_tool(source=None, sink=None, identity=None)
        def payload(size: int) -> str:
            body_calls.append(size)
            return "x" * size

        registry.register(payload)
        run = HostToolRun(registry)
        refused = asyncio.run(run.call("payload", {"size": 1}, framing_bytes=5))
        assert not refused.ok
        assert refused.refusal is not None and "byte budget" in refused.refusal
        assert body_calls == []
        self._pairing(events)
        assert [kind for kind, *_ in events] == ["enter", "exit"]
        assert events[0][3] == "payload"

    def test_two_in_flight_host_tool_calls_pair_their_observers(self):
        """Concurrency correctness is the ``with``, not a primitive: each host-tool call enters
        its own observer, so an interleaving is legal but never crosses."""
        entered = asyncio.Event()
        release = asyncio.Event()

        @sandbox_tool(source=None, sink="file_store", identity=None)
        async def slow(x: int) -> int:
            entered.set()
            await release.wait()
            return x

        events: list[tuple[str, object, HostToolRun, object]] = []
        registry = HostToolRegistry(
            response_limits=dataclasses.replace(DEFAULT_TRANSFER_LIMITS, max_files=1),
            host_tool_calls_observer=self._recording(events),
        )
        registry.register(slow)
        run = HostToolRun(registry)

        async def scenario() -> None:
            first = asyncio.create_task(run.call("slow", {"x": 1}))
            await entered.wait()  # inside its body, observer still entered
            second = asyncio.create_task(run.call("slow", {"x": 2}))
            # Pin the second observer's enter, not the host-tool call's outcome: a second that is
            # no longer observed never enters, and the wait must fail the suite instead of
            # hanging. Release only once it is in — the interleave the test exists to pin.
            deadline = time.monotonic() + 5
            while sum(kind == "enter" for kind, *_ in events) < 2:
                if time.monotonic() > deadline:
                    pytest.fail("the second host-tool call never entered its observer")
                await asyncio.sleep(0)
            release.set()
            await asyncio.gather(first, second)

        asyncio.run(scenario())
        self._pairing(events)
        assert sum(kind == "enter" for kind, *_ in events) == 2

    def test_a_cancelled_host_tool_call_still_exits_its_observer(self):
        """Cancellation is a ``BaseException`` with no outcome — the exit is structural, so it
        still happens, and the slot the body was holding goes back with it."""
        entered = asyncio.Event()

        @sandbox_tool(source=None, sink="file_store", identity=None)
        async def never() -> int:
            entered.set()
            await asyncio.Event().wait()
            return 1

        events: list[tuple[str, object, HostToolRun, object]] = []
        registry = HostToolRegistry(
            response_limits=dataclasses.replace(DEFAULT_TRANSFER_LIMITS, max_files=1),
            host_tool_calls_observer=self._recording(events),
        )
        registry.register(never)
        run = HostToolRun(registry)

        async def scenario() -> None:
            abandoned = asyncio.create_task(run.call("never"))
            await entered.wait()
            abandoned.cancel()
            await asyncio.wait([abandoned])
            assert abandoned.cancelled(), "the premise: the call was abandoned mid-body"

        asyncio.run(scenario())
        self._pairing(events)
        assert [kind for kind, *_ in events] == ["enter", "exit"]

    def test_a_context_the_host_tool_call_never_enters_records_nothing(self):
        """The ``enter`` event is the ``__enter__`` itself: a context the host-tool call never
        enters records no event at all, not an enter that no exit will ever pair."""
        events: list[tuple[str, object, HostToolRun, object]] = []
        observer = self._recording(events)
        observer(HostToolRun(HostToolRegistry()), "doubled")
        assert events == []

    def test_observation_begins_before_the_body_and_ends_after(self):
        """The body's position in the stream is the pin: a host-tool call that entered its
        observer after the body ran — or exited it before — breaks the shape."""
        order: list[str] = []

        @contextlib.contextmanager
        def cm():
            order.append("enter")
            try:
                yield
            finally:
                order.append("exit")

        def factory(run: HostToolRun, name: object) -> contextlib.AbstractContextManager[object]:
            return cm()

        @sandbox_tool(source=None, sink=None, identity=None)
        def marks_body() -> None:
            order.append("body")

        registry = HostToolRegistry(host_tool_calls_observer=factory)
        registry.register(marks_body)
        assert _call_host_tool(HostToolRun(registry), "marks_body").ok
        assert order == ["enter", "body", "exit"]

    def test_a_framing_rejection_happens_before_the_observation(self):
        """The door's own checks, so no enter exists for an exit to pair: a host-tool call that
        raised on framing has spent nothing, and the host saw nothing to attribute."""
        events: list[tuple[str, object, HostToolRun, object]] = []
        registry = HostToolRegistry(host_tool_calls_observer=self._recording(events))
        registry.register(_stamped_pure())
        run = HostToolRun(registry)
        with pytest.raises(ValueError, match="framing_bytes"):
            asyncio.run(run.call("doubled", {"x": 1}, framing_bytes=-1))
        assert events == []

    def _observer_that_raises(self, where: str):
        """An observer whose ``factory`` / ``__enter__`` / ``__exit__`` raises, per ``where``."""

        @contextlib.contextmanager
        def cm():
            if where == "enter":
                raise RuntimeError("the observer's __enter__")
            yield
            if where == "exit":
                raise RuntimeError("the observer's __exit__")

        def factory(run: HostToolRun, name: object):
            if where == "factory":
                raise RuntimeError("the observer's factory")
            return cm()

        return factory

    def _observer_that_cancels(self, where: str):
        """An observer whose ``factory`` / ``__enter__`` / ``__exit__`` raises
        ``asyncio.CancelledError`` — a ``BaseException``, so the guard must take it too."""

        @contextlib.contextmanager
        def cm():
            if where == "enter":
                raise asyncio.CancelledError()
            yield
            if where == "exit":
                raise asyncio.CancelledError()

        def factory(run: HostToolRun, name: object):
            if where == "factory":
                raise asyncio.CancelledError()
            return cm()

        return factory

    def test_a_failing_observer_costs_the_host_a_log_line_not_the_host_tool_call(self, caplog):
        caplog.at_level(logging.WARNING)
        for where in ("factory", "enter", "exit"):
            caplog.clear()
            registry = HostToolRegistry(host_tool_calls_observer=self._observer_that_raises(where))
            registry.register(_stamped_pure())
            with caplog.at_level(logging.WARNING):
                result = _call_host_tool(HostToolRun(registry), "doubled", {"x": 21})
            assert result.ok, where
            assert "observer" in caplog.text, where

    def test_an_observer_that_cancels_costs_the_host_a_log_line_not_the_host_tool_call(
        self, caplog
    ):
        """``CancelledError`` is a ``BaseException``, and the host-tool call is not the observer's
        to cancel: each guard must take it and log, the way it takes any other observer failure."""
        caplog.at_level(logging.WARNING)
        for where in ("factory", "enter", "exit"):
            caplog.clear()
            registry = HostToolRegistry(host_tool_calls_observer=self._observer_that_cancels(where))
            registry.register(_stamped_pure())
            with caplog.at_level(logging.WARNING):
                result = _call_host_tool(HostToolRun(registry), "doubled", {"x": 21})
            assert result.ok, where
            assert "observer" in caplog.text, where

    def test_an_observer_that_exits_the_process_still_does(self, caplog):
        """``SystemExit`` is the host's control flow, not an observer failure: contained for
        ``Exception`` and ``CancelledError`` alike, it escapes the guards rather than being
        logged and the host-tool call carried on past a process that is on its way out."""
        for where in ("factory", "enter", "exit"):
            caplog.clear()
            registry = HostToolRegistry(host_tool_calls_observer=self._observer_that_exits(where))
            registry.register(_stamped_pure())
            with pytest.raises(SystemExit):
                _call_host_tool(HostToolRun(registry), "doubled", {"x": 21})
            assert "observer" not in caplog.text, where

    @staticmethod
    def _observer_that_exits(where: str):
        """An observer whose ``factory`` / ``__enter__`` / ``__exit__`` raises ``SystemExit``."""

        @contextlib.contextmanager
        def cm():
            if where == "enter":
                raise SystemExit(3)
            yield
            if where == "exit":
                raise SystemExit(3)

        def factory(run: HostToolRun, name: object):
            if where == "factory":
                raise SystemExit(3)
            return cm()

        return factory

    def test_an_observer_that_closes_its_own_generator_is_contained(self, caplog):
        """``GeneratorExit`` from the observer's own generator is an observer failure — the
        host-tool call is not made to fail, or to stop, over it, the way an ``Exception`` or a
        ``CancelledError`` is not."""
        caplog.at_level(logging.WARNING)
        for where in ("factory", "enter", "exit"):
            caplog.clear()
            registry = HostToolRegistry(host_tool_calls_observer=self._observer_that_closes(where))
            registry.register(_stamped_pure())
            with caplog.at_level(logging.WARNING):
                result = _call_host_tool(HostToolRun(registry), "doubled", {"x": 21})
            assert result.ok, where
            assert "observer" in caplog.text, where

    @staticmethod
    def _observer_that_closes(where: str):
        """An observer whose ``factory`` / ``__enter__`` / ``__exit__`` raises ``GeneratorExit``
        from its own generator — the shape a context manager in cleanup has."""

        @contextlib.contextmanager
        def cm():
            if where == "enter":
                raise GeneratorExit()
            yield
            if where == "exit":
                raise GeneratorExit()

        def factory(run: HostToolRun, name: object):
            if where == "factory":
                raise GeneratorExit()
            return cm()

        return factory

    def test_an_exit_that_returns_true_does_not_swallow_a_failure(self):
        """A body that fails becomes a refusal for the guest; the observer's ``__exit__`` —
        even one returning ``True``, the form that swallows an exception in a ``with`` —
        cannot change that refusal."""

        @sandbox_tool(source=None, sink=None, identity=None)
        def broken() -> None:
            raise RuntimeError("boom")

        registry = HostToolRegistry(host_tool_calls_observer=self._observer_that_returns_true())
        registry.register(broken)
        result = _call_host_tool(HostToolRun(registry), "broken")
        assert not result.ok
        assert result.refusal is not None and "failed" in result.refusal

    def test_an_exit_that_returns_true_does_not_swallow_a_cancellation(self):
        """The only exception a host-tool call can propagate is a cancellation; an ``__exit__`` that
        returns ``True`` is exactly the form that would swallow it — and does not."""
        entered = asyncio.Event()

        @sandbox_tool(source=None, sink="file_store", identity=None)
        async def never() -> int:
            entered.set()
            await asyncio.Event().wait()
            return 1

        registry = HostToolRegistry(
            response_limits=dataclasses.replace(DEFAULT_TRANSFER_LIMITS, max_files=1),
            host_tool_calls_observer=self._observer_that_returns_true(),
        )
        registry.register(never)
        run = HostToolRun(registry)

        async def scenario() -> None:
            abandoned = asyncio.create_task(run.call("never"))
            await entered.wait()
            abandoned.cancel()
            await asyncio.wait([abandoned])
            assert abandoned.cancelled(), "an __exit__ returning True swallowed the cancellation"

        asyncio.run(scenario())

    @staticmethod
    def _observer_that_returns_true():
        """An observer whose ``__exit__`` returns ``True`` — the form that swallows an
        exception handed to a ``with`` block.  It is a class, because a
        ``@contextlib.contextmanager`` generator's return value never reaches ``__exit__``."""

        class _swallowing:
            def __enter__(self) -> None:
                return None

            def __exit__(self, *exc_info: object) -> bool:
                return True

        def factory(run: HostToolRun, name: object):
            return _swallowing()

        return factory

    def test_a_non_callable_observer_is_refused_at_construction(self):
        with pytest.raises(TypeError, match="must be callable"):
            HostToolRegistry(host_tool_calls_observer=3)  # type: ignore[arg-type]

    def test_a_coroutine_function_observer_is_refused_at_construction(self):
        """A coroutine ``factory`` would be a context manager no one awaits — an observer that
        never fires, which is the one failure mode a host could not notice from the outside."""

        async def factory(run: HostToolRun, name: object):
            return contextlib.nullcontext()

        with pytest.raises(TypeError, match="must be synchronous"):
            HostToolRegistry(host_tool_calls_observer=factory)  # type: ignore[arg-type]

    def test_an_async_callable_instance_observer_is_refused_at_construction(self):
        """The same observer no one awaits, as an instance: only its ``__call__`` is the
        coroutine function a bare ``iscoroutinefunction`` sees."""

        class _async_call:
            async def __call__(
                self, run: HostToolRun, name: object
            ) -> contextlib.AbstractContextManager[object]:
                return contextlib.nullcontext()

        with pytest.raises(TypeError, match="must be synchronous"):
            HostToolRegistry(host_tool_calls_observer=_async_call())  # type: ignore[arg-type]

    def test_the_property_reflects_what_the_host_registered(self):
        observer = lambda run, name: contextlib.nullcontext()  # noqa: E731
        registry = HostToolRegistry(host_tool_calls_observer=observer)
        assert registry.host_tool_calls_observer is observer

    def test_an_observer_whose_enter_returns_a_span_is_admitted(self):
        """Tracing and span context managers return their span from ``__enter__``: the
        host-tool call discards that value, so the public type must not refuse to name it."""

        @contextlib.contextmanager
        def span() -> Generator[object]:
            yield "span"

        def factory(run: HostToolRun, name: object) -> contextlib.AbstractContextManager[object]:
            return span()

        registry = HostToolRegistry(host_tool_calls_observer=factory)
        registry.register(_stamped_pure())
        assert _call_host_tool(HostToolRun(registry), "doubled", {"x": 1}).ok


#: Every name 0.25 removed, against the module a dependent imported it from — the package for a
#: public name, the defining module for the path that skips ``__init__``.
_REMOVED_NAMES = [
    ("maf_sandbox", "DEFAULT_MAX_DISPATCHES_PER_RUN"),
    ("maf_sandbox", "DispatchResult"),
    ("maf_sandbox", "dispatch_over_exec"),
    ("maf_sandbox", "fold_dispatch_transfer_limits"),
    ("maf_sandbox._host_tools", "DEFAULT_MAX_DISPATCHES_PER_RUN"),
    ("maf_sandbox._host_tools", "DispatchResult"),
    ("maf_sandbox._host_tools_over_exec", "dispatch_over_exec"),
    ("maf_sandbox._host_tools_over_exec", "fold_dispatch_transfer_limits"),
]


class TestThePreRenameSpellingIsGone:
    """The mirror of the alias tests 0.24 carried: every old name fails, and names what it wanted.

    A name, a member and a keyword each need their own check — removing one kind does not
    remove another, which is why the aliases needed four shapes to add.
    """

    @pytest.mark.parametrize(("module", "name"), _REMOVED_NAMES)
    def test_the_removed_names_do_not_resolve(self, module: str, name: str):
        imported = importlib.import_module(module)
        assert not hasattr(imported, name)
        assert name not in getattr(imported, "__all__", ())

    @pytest.mark.parametrize("name", ["max_dispatches_per_run", "dispatch_observer"])
    def test_the_removed_registry_properties_are_gone(self, name: str):
        assert not hasattr(HostToolRegistry(), name)

    def test_the_removed_run_method_is_gone(self):
        assert not hasattr(HostToolRun(HostToolRegistry()), "dispatch")

    @pytest.mark.parametrize(
        ("keyword", "value"), [("max_dispatches_per_run", 5), ("dispatch_observer", None)]
    )
    def test_the_removed_keywords_are_refused_by_name(self, keyword: str, value: object):
        # Passed as a mapping, not as a literal keyword: a literal is a call a static analyser
        # has to flag as a wrong argument name, which is the very thing being asserted.
        with pytest.raises(TypeError, match=f"unexpected keyword argument '{keyword}'"):
            HostToolRegistry(**{keyword: value})
