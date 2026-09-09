"""Literal method policy, normalization and refusal before a backend is called."""

from __future__ import annotations

import asyncio
import dataclasses
import json
from typing import Any

import pytest

from maf_sandbox import (
    DEFAULT_CAPABILITIES,
    Capability,
    EffectiveState,
    Egress,
    EgressRule,
    Isolation,
    IsolationScope,
    SandboxAcquired,
    SandboxBackendNotPermitted,
    SandboxCapabilityDenied,
    SandboxCapabilityNotSupported,
    SandboxKey,
    SandboxRouter,
    SandboxSpec,
    Selection,
    SourceChannel,
)
from maf_sandbox.maf import _channel_clause
from maf_sandbox.testing import FAKE_BACKEND_DECLARATIONS, InProcessSandboxBackend

KEY = SandboxKey(scope="test", thread_id="thread", agent_dir="agent")


def spec(*entries: str | EgressRule) -> SandboxSpec:
    return SandboxSpec(kind="test", egress=Egress.ALLOWLIST, egress_allow=entries)


def backend(
    tokens: frozenset[str] | None = frozenset({"GET"}), *, name: str = "methods"
) -> InProcessSandboxBackend:
    return InProcessSandboxBackend(
        name=name,
        declarations=dataclasses.replace(
            FAKE_BACKEND_DECLARATIONS,
            capabilities=DEFAULT_CAPABILITIES | {Capability.EGRESS_METHODS},
            egress_method_tokens=tokens,
        ),
    )


class TestMethodVocabulary:
    @pytest.mark.parametrize(
        "method", ["", "GET/foo", "GET:foo", "GET,POST", " GET", "GÉT", "GET\n"]
    )
    def test_malformed_tokens_are_refused(self, method: str):
        with pytest.raises(ValueError, match="HTTP token"):
            EgressRule("example.com", (method,))

    def test_custom_tokens_and_case_are_preserved(self):
        tokens = ("GET", "get", "PROPFIND", "!#$%&'*+-.^_`|~0123AZaz")
        assert EgressRule("example.com", tokens).methods == tokens

    @pytest.mark.parametrize("methods", [(), ("GET", "GET")])
    def test_empty_or_repeated_methods_are_refused(self, methods: tuple[str, ...]):
        with pytest.raises(ValueError):
            EgressRule("example.com", methods)

    @pytest.mark.parametrize("methods", ["GET", ["GET"]])
    def test_mutable_or_bare_string_methods_are_refused(self, methods: Any):
        with pytest.raises(TypeError):
            EgressRule("example.com", methods)

    def test_scoping_derives_an_opt_in_requirement(self):
        original = DEFAULT_CAPABILITIES
        scoped = spec(EgressRule("example.com", ("GET",)))
        assert scoped.requires == original
        assert scoped.required_capabilities == original | {Capability.EGRESS_METHODS}
        assert Capability.EGRESS_METHODS not in original
        with pytest.raises(dataclasses.FrozenInstanceError):
            scoped.egress_allow[0].methods = ("POST",)  # type: ignore[union-attr,misc]

    def test_all_methods_rules_become_plain_strings_without_requiring_the_capability(self):
        normalized = spec(EgressRule("Example.com"), "example.com", "other.example")
        assert normalized.egress_allow == ("Example.com", "other.example")
        assert normalized.requires == DEFAULT_CAPABILITIES

    def test_equivalent_methods_collapse_preserving_the_first_spelling_and_order(self):
        first = EgressRule("Example.com", ("POST", "GET"))
        assert spec(first, EgressRule("example.com", ("GET", "POST"))).egress_allow == (first,)

    @pytest.mark.parametrize(
        "other",
        ["EXAMPLE.com", EgressRule("EXAMPLE.com", ("POST",)), EgressRule("EXAMPLE.com", ("get",))],
    )
    def test_conflicting_policies_for_one_host_refuse(self, other: str | EgressRule):
        with pytest.raises(ValueError, match="conflicting"):
            spec(EgressRule("example.com", ("GET",)), other)

    @pytest.mark.parametrize("mode", [Egress.CLOSED, Egress.UNRESTRICTED])
    def test_the_wrong_mode_keeps_the_documented_value_error(self, mode: Egress):
        with pytest.raises(ValueError, match=r"example.com \(GET\)"):
            SandboxSpec(
                kind="test", egress=mode, egress_allow=(EgressRule("example.com", ("GET",)),)
            )

    @pytest.mark.parametrize("host", ["", "https://example.com", "a,b", "a b"])
    def test_core_does_not_take_over_host_validation(self, host: str):
        assert spec(EgressRule(host)).egress_allow == (host,)


class TestMethodRouting:
    @pytest.mark.parametrize("entries", [("example.com",), (EgressRule("example.com"),), ()])
    @pytest.mark.parametrize("selection", [Selection.FIXED, Selection.PER_SPEC])
    def test_removing_method_scope_serves_an_ordinary_backend(
        self, entries: tuple[str | EgressRule, ...], selection: Selection
    ):
        provider = InProcessSandboxBackend()
        router = SandboxRouter(
            [provider],
            min_isolation=Isolation.NONE,
            selection=selection,
            denied_capabilities={Capability.EGRESS_METHODS},
        )
        scoped = spec(EgressRule("example.com", ("GET",)))
        unscoped = dataclasses.replace(scoped, egress_allow=entries)
        router.ensure_can_serve(unscoped)
        assert router.backend_for(unscoped) is provider
        asyncio.run(router.acquire(KEY, unscoped))
        assert KEY in provider.keys
        with pytest.raises(SandboxCapabilityDenied):
            router.ensure_can_serve(scoped)
        assert router.backend_for(scoped) is None

    def test_replacement_preserves_explicit_requirements_until_the_caller_changes_them(self):
        explicit = DEFAULT_CAPABILITIES | {Capability.EGRESS_METHODS, Capability.HOST_TOOLS}
        scoped = dataclasses.replace(spec(EgressRule("example.com", ("GET",))), requires=explicit)
        unscoped = dataclasses.replace(scoped, egress_allow=("example.com",))
        assert unscoped.requires == unscoped.required_capabilities == explicit
        provider = InProcessSandboxBackend()
        router = SandboxRouter([provider], min_isolation=Isolation.NONE)
        with pytest.raises(SandboxCapabilityNotSupported, match="egress_methods"):
            router.ensure_can_serve(unscoped)
        revised = dataclasses.replace(unscoped, requires=DEFAULT_CAPABILITIES)
        router.ensure_can_serve(revised)

    def test_replacement_cannot_drop_enforcement_while_method_scope_remains(self):
        router = SandboxRouter([InProcessSandboxBackend()], min_isolation=Isolation.NONE)
        scoped = spec(EgressRule("example.com", ("GET",)))
        revised = dataclasses.replace(scoped, image="new-image", requires=frozenset())
        with pytest.raises(SandboxCapabilityNotSupported, match="egress_methods"):
            router.ensure_can_serve(revised)

    def test_a_non_declarer_refuses_before_acquire(self):
        provider = InProcessSandboxBackend()
        router = SandboxRouter([provider], min_isolation=Isolation.NONE)
        scoped = spec(EgressRule("example.com", ("GET",)))
        with pytest.raises(SandboxCapabilityNotSupported, match="egress_methods"):
            router.ensure_can_serve(scoped)
        with pytest.raises(SandboxCapabilityNotSupported, match="egress_methods"):
            asyncio.run(router.acquire(KEY, scoped))
        assert provider.keys == []

    @pytest.mark.parametrize("method", ["get", "PROPFIND", "POST"])
    def test_the_finite_token_set_refuses_at_preflight_and_acquire(self, method: str):
        provider = backend()
        router = SandboxRouter([provider], min_isolation=Isolation.NONE)
        scoped = spec(EgressRule("example.com", (method,)))
        with pytest.raises(SandboxCapabilityNotSupported, match=method):
            router.ensure_can_serve(scoped)
        with pytest.raises(SandboxCapabilityNotSupported, match=method):
            asyncio.run(router.acquire(KEY, scoped))
        assert provider.keys == []

    @pytest.mark.parametrize("tokens", [None, frozenset({"get", "PROPFIND"})])
    def test_declared_tokens_are_accepted_verbatim(self, tokens: frozenset[str] | None):
        provider = backend(tokens)
        router = SandboxRouter([provider], min_isolation=Isolation.NONE)
        scoped = spec(EgressRule("example.com", ("get", "PROPFIND")))
        router.ensure_can_serve(scoped)
        asyncio.run(router.acquire(KEY, scoped))
        assert provider.keys

    def test_omitting_the_token_declaration_does_not_promise_every_token(self):
        router = SandboxRouter([backend(frozenset())], min_isolation=Isolation.NONE)
        with pytest.raises(SandboxCapabilityNotSupported):
            router.ensure_can_serve(spec(EgressRule("example.com", ("GET",))))

    def test_selection_skips_a_backend_that_cannot_hold_the_literal_method(self):
        first, second = backend(name="first"), backend(None, name="second")
        router = SandboxRouter(
            [first, second], min_isolation=Isolation.NONE, selection=Selection.PER_SPEC
        )
        scoped = spec(EgressRule("example.com", ("PROPFIND",)))
        router.ensure_can_serve(scoped)
        asyncio.run(router.acquire(KEY, scoped))
        assert not first.keys
        assert second.keys

    def test_host_denial_applies_to_the_derived_capability(self):
        router = SandboxRouter(
            [backend()],
            min_isolation=Isolation.NONE,
            denied_capabilities={Capability.EGRESS_METHODS},
        )
        with pytest.raises(SandboxCapabilityDenied):
            router.ensure_can_serve(spec(EgressRule("example.com", ("GET",))))

    def test_malformed_declaration_does_not_silently_select_another_backend(self):
        bad: Any = "GET"
        with pytest.raises(SandboxBackendNotPermitted, match="egress_method_tokens"):
            SandboxRouter(
                [backend(bad), backend(None)],
                min_isolation=Isolation.NONE,
                selection=Selection.PER_SPEC,
            )


class TestMethodRecording:
    def test_effective_state_is_json_native_and_preserves_methods(self):
        scoped = spec("other.example", EgressRule("example.com", ("GET", "get")))
        event = SandboxAcquired(
            key=KEY,
            spec=scoped,
            isolation_scope=IsolationScope.CONVERSATION,
            backend="test",
            isolation=Isolation.NONE,
            declarations=backend(None).declarations,
            seconds=0,
        )
        state = EffectiveState.of(event)
        assert state is not None
        assert Capability.EGRESS_METHODS in state.requires
        unscoped = dataclasses.replace(scoped, egress_allow=("example.com",))
        unscoped_state = EffectiveState.of(dataclasses.replace(event, spec=unscoped))
        assert unscoped_state is not None
        assert Capability.EGRESS_METHODS not in unscoped_state.requires
        assert json.loads(json.dumps(state.as_dict()))["egress_allow"] == [
            "other.example",
            {"host": "example.com", "methods": ["GET", "get"]},
        ]

    def test_a_channel_refusal_names_the_methods(self):
        clause = _channel_clause(SourceChannel.EGRESS, spec(EgressRule("example.com", ("GET",))))
        assert "example.com (GET)" in clause
