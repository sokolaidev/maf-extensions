"""Tests for the host-tools safety contract (issue #133, part A).

The contract is what has to exist before anything can dispatch, so what is pinned here is
the *shape of refusal* as much as the happy path: an unstamped function refused where the
host can fix it, an undeclared name unreachable from inside, a cap that ends a run with a
sentence rather than an exception, and a USER-identity tool that registers loudly and never
serves.  Router-level denial — the sixth layer — is pinned in ``test_sandbox_router.py``
beside the rest of the router's policy.
"""

from __future__ import annotations

import asyncio
import dataclasses
import inspect
import json
import logging

import pytest

import maf_sandbox._host_tools as host_tools_module
from maf_sandbox import (
    DEFAULT_MAX_DISPATCHES_PER_RUN,
    DEFAULT_TRANSFER_LIMITS,
    FLOW_DECLARED_KEY,
    INTEGRITY_RANK,
    DispatchResult,
    HostToolDeclaration,
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
        assert HostToolRegistry().max_dispatches_per_run == DEFAULT_MAX_DISPATCHES_PER_RUN

    def test_the_default_cap_bites_before_the_response_count_leg(self):
        """So with everything defaulted, exhaustion names the dispatch cap, not the ledger."""
        assert DEFAULT_MAX_DISPATCHES_PER_RUN < DEFAULT_TRANSFER_LIMITS.max_files

    def test_a_cap_below_one_is_refused(self):
        with pytest.raises(ValueError, match="empty registry"):
            HostToolRegistry(max_dispatches_per_run=0)

    def test_the_pair_of_directions_type_is_refused_as_response_limits(self):
        """The same TransferLimits-vs-SandboxLimits confusion the router already refuses."""
        with pytest.raises(TypeError, match="TransferLimits"):
            HostToolRegistry(response_limits=SandboxLimits())  # type: ignore[arg-type]


class TestRegistration:
    def test_starts_empty(self):
        """Layer 1: nothing is dispatchable until a developer explicitly registers it."""
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

    def test_the_gate_refuses_an_unstamped_function_at_registration(self):
        """Where the host can fix it — one decorator away — not later at dispatch."""
        registry = HostToolRegistry(require_declared=True)
        with pytest.raises(HostToolNotDeclared, match="@sandbox_tool"):
            registry.register(_pure)

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
        registry = HostToolRegistry()
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

    def test_the_sealed_surface_is_the_one_that_dispatches(self):
        registry = HostToolRegistry()
        registry.register(_stamped_pure(), name="doubled")
        registry.aggregate()
        with pytest.raises(ValueError):
            registry.register(_stamped_pure(), name="late")
        assert registry.names() == frozenset({"doubled"})


class TestACeilingMustBeAbleToCompare:
    """A non-integer ceiling removes itself, which is the one thing a safety cap must not do."""

    @pytest.mark.parametrize("bad", [float("nan"), float("inf"), 2.5, True, "8", None])
    def test_the_dispatch_cap_refuses_anything_but_a_plain_integer(self, bad: object):
        with pytest.raises(TypeError, match="max_dispatches_per_run"):
            HostToolRegistry(max_dispatches_per_run=bad)  # type: ignore[arg-type]

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
    def test_a_negative_response_ceiling_is_refused(self, leg: str):
        limits = dataclasses.replace(DEFAULT_TRANSFER_LIMITS, **{leg: -1})
        with pytest.raises(ValueError, match=leg):
            HostToolRegistry(response_limits=limits)


class TestTheResponseLedgerIsCheckedBeforeTheSideEffect:
    """A sink tool's body runs in the host process; refusing after it has run is too late."""

    def test_an_exhausted_ledger_refuses_without_calling_the_tool(self):
        calls: list[int] = []

        @sandbox_tool(source=None, sink="workspace", identity=None)
        def writes(x: int) -> int:
            calls.append(x)
            return x

        registry = HostToolRegistry(
            response_limits=dataclasses.replace(DEFAULT_TRANSFER_LIMITS, max_files=1)
        )
        registry.register(writes, name="writes")
        run = HostToolRun(registry)

        assert _dispatch(run, "writes", {"x": 1}).ok
        second = _dispatch(run, "writes", {"x": 2})

        assert not second.ok
        assert second.refusal is not None and "delivered-response cap" in second.refusal
        assert calls == [1], "the second call must not have reached the tool body"


class TestDispatchResult:
    def test_exactly_one_side_must_be_set(self):
        with pytest.raises(ValueError):
            DispatchResult()
        with pytest.raises(ValueError):
            DispatchResult(value_json="1", refusal="Error: no")
        assert DispatchResult(value_json="1").ok is True
        assert DispatchResult(refusal="Error: no").ok is False


def _dispatch(run: HostToolRun, name: str, arguments=None) -> DispatchResult:
    return asyncio.run(run.dispatch(name, arguments))


class TestDispatch:
    def test_a_registered_tool_round_trips_its_value_as_json(self):
        registry = HostToolRegistry()
        registry.register(_stamped_pure(), name="doubled")
        result = _dispatch(HostToolRun(registry), "doubled", {"x": 21})
        assert result.ok
        assert result.value_json is not None
        assert json.loads(result.value_json) == 42

    def test_an_async_tool_is_awaited(self):
        @sandbox_tool(source=None, sink=None, identity=None)
        async def fetch(x: int) -> dict[str, int]:
            return {"x": x}

        registry = HostToolRegistry()
        registry.register(fetch)
        result = _dispatch(HostToolRun(registry), "fetch", {"x": 7})
        assert result.ok
        assert result.value_json is not None
        assert json.loads(result.value_json) == {"x": 7}

    def test_an_unregistered_name_is_unreachable(self):
        """The one-door pin: a host function that exists but was never registered has no
        path in — resolution goes exclusively through the registry."""
        registry = HostToolRegistry()
        registry.register(_stamped_pure(), name="doubled")
        result = _dispatch(HostToolRun(registry), "_pure", {"x": 1})
        assert not result.ok
        assert result.refusal == "Error: '_pure' is not a registered host tool"

    def test_the_cap_returns_a_sanitized_refusal_rather_than_raising(self):
        registry = HostToolRegistry(max_dispatches_per_run=2)
        registry.register(_stamped_pure(), name="doubled")
        run = HostToolRun(registry)
        assert _dispatch(run, "doubled", {"x": 1}).ok
        assert _dispatch(run, "doubled", {"x": 2}).ok
        third = _dispatch(run, "doubled", {"x": 3})
        assert not third.ok
        assert third.refusal is not None and "dispatch cap (2) is exhausted" in third.refusal

    def test_refused_attempts_burn_the_cap_too(self):
        """A guest probing unknown names is spending exactly the budget the cap bounds."""
        registry = HostToolRegistry(max_dispatches_per_run=2)
        registry.register(_stamped_pure(), name="doubled")
        run = HostToolRun(registry)
        _dispatch(run, "nope")
        _dispatch(run, "nope")
        third = _dispatch(run, "doubled", {"x": 1})
        assert not third.ok
        assert third.refusal is not None and "exhausted" in third.refusal

    def test_a_fresh_run_starts_with_a_fresh_count(self):
        registry = HostToolRegistry(max_dispatches_per_run=1)
        registry.register(_stamped_pure(), name="doubled")
        assert _dispatch(HostToolRun(registry), "doubled", {"x": 1}).ok
        assert _dispatch(HostToolRun(registry), "doubled", {"x": 1}).ok

    def test_arguments_are_validated_host_side_at_the_one_door(self):
        registry = HostToolRegistry()
        registry.register(_stamped_pure(), name="doubled")
        result = _dispatch(HostToolRun(registry), "doubled", {"y": 1})
        assert not result.ok
        assert result.refusal is not None and "do not bind" in result.refusal

    def test_non_mapping_arguments_are_refused(self):
        registry = HostToolRegistry()
        registry.register(_stamped_pure(), name="doubled")
        result = _dispatch(HostToolRun(registry), "doubled", [1, 2])  # type: ignore[arg-type]
        assert not result.ok
        assert result.refusal is not None and "JSON object" in result.refusal

    def test_a_user_identity_tool_is_refused_with_the_prerequisites_named(self):
        """Declarable but not servable: the refusal says what serving would take."""

        @sandbox_tool(source=None, sink=None, identity=Identity.USER)
        def as_user() -> str:
            return "never"

        registry = HostToolRegistry()
        registry.register(as_user)
        result = _dispatch(HostToolRun(registry), "as_user")
        assert not result.ok
        assert result.refusal is not None
        assert "per-run token minting" in result.refusal
        assert "audience-within-egress" in result.refusal
        assert "env channel" in result.refusal

    def test_dispatch_uses_the_declaration_registration_captured(self):
        """A stamp removed after registration neither refuses nor widens: it is not read."""
        registry = HostToolRegistry(require_declared=True)
        tool = _stamped_pure()
        registry.register(tool, name="doubled")
        delattr(tool, FLOW_DECLARED_KEY)
        result = _dispatch(HostToolRun(registry), "doubled", {"x": 1})
        assert result.ok, "the registered claim stands; the attribute is no longer consulted"

    def test_an_identity_swapped_in_after_registration_cannot_reach_dispatch(self):
        """The swap that matters: USER authority arriving after the aggregate was derived."""
        registry = HostToolRegistry()
        tool = _stamped_pure()
        registry.register(tool, name="doubled")
        setattr(
            tool,
            FLOW_DECLARED_KEY,
            HostToolDeclaration(source=None, sink=None, identity=Identity.USER),
        )
        result = _dispatch(HostToolRun(registry), "doubled", {"x": 1})
        assert result.ok, "dispatch reads the registered declaration, not the current one"


class TestDispatchFailureLadder:
    def test_a_raising_tool_sends_the_detail_to_the_log_not_the_guest(
        self, caplog: pytest.LogCaptureFixture
    ):
        @sandbox_tool(source=None, sink=None, identity=None)
        def broken() -> None:
            raise RuntimeError("endpoint https://internal.example refused the token")

        registry = HostToolRegistry()
        registry.register(broken)
        with caplog.at_level(logging.WARNING):
            result = _dispatch(HostToolRun(registry), "broken")
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
        that must follow the failure ladder rather than escaping `dispatch`.

        `max` is one such built-in today; the assertion below pins the premise, so this test
        fails loudly if CPython ever gives it a text signature rather than passing vacuously.
        """
        with pytest.raises(ValueError):
            inspect.signature(max)

        registry = HostToolRegistry()
        registry.register(max, name="largest")
        with caplog.at_level(logging.WARNING):
            result = _dispatch(HostToolRun(registry), "largest", {"iterable": [1, 2]})
        assert not result.ok
        assert result.refusal is not None and "exposes no signature" in result.refusal
        assert "largest" in caplog.text

    @pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
    def test_a_non_finite_float_is_refused_rather_than_delivered_unparseable(self, value: float):
        """Python's default `json.dumps` emits bare NaN/Infinity, which strict parsers on the
        guest side reject — delivering that as success hands over a payload nothing can read."""

        @sandbox_tool(source=None, sink=None, identity=None)
        def measure() -> float:
            return value

        registry = HostToolRegistry()
        registry.register(measure)
        result = _dispatch(HostToolRun(registry), "measure")
        assert not result.ok
        assert result.refusal is not None and "cannot be carried as JSON" in result.refusal

    def test_an_unserializable_value_is_refused(self):
        @sandbox_tool(source=None, sink=None, identity=None)
        def opaque() -> object:
            return object()

        registry = HostToolRegistry()
        registry.register(opaque)
        result = _dispatch(HostToolRun(registry), "opaque")
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
        result = _dispatch(HostToolRun(self._registry()), "payload", {"size": 100})
        assert not result.ok
        assert result.refusal is not None and "per-response cap allows 64" in result.refusal

    def test_the_run_total_is_a_running_ledger(self):
        run = HostToolRun(self._registry())
        assert _dispatch(run, "payload", {"size": 60}).ok
        assert _dispatch(run, "payload", {"size": 60}).ok
        third = _dispatch(run, "payload", {"size": 60})
        assert not third.ok
        assert third.refusal is not None and "total response cap" in third.refusal

    def test_the_delivered_response_count_is_capped(self):
        run = HostToolRun(self._registry(max_files=1, max_total_bytes=10_000))
        assert _dispatch(run, "payload", {"size": 1}).ok
        second = _dispatch(run, "payload", {"size": 1})
        assert not second.ok
        assert second.refusal is not None and "delivered-response cap (1)" in second.refusal

    def test_a_refused_response_does_not_spend_the_ledger(self):
        """Over-cap bytes are never delivered, so they must not count as if they were."""
        run = HostToolRun(self._registry())
        assert not _dispatch(run, "payload", {"size": 100}).ok
        assert _dispatch(run, "payload", {"size": 60}).ok
