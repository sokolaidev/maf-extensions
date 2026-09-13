"""Attached authority admission, explicit channels and conservative defaults."""

from __future__ import annotations

import asyncio
import dataclasses
import itertools
import json
from typing import Any

import pytest

from maf_sandbox import (
    DEFAULT_CAPABILITIES,
    IDENTITY_SCOPE_RANK,
    NO_ATTACHED_IDENTITY,
    AttachedIdentity,
    AuthorityChannel,
    BackendDeclarations,
    Capability,
    EffectiveState,
    Egress,
    EgressRule,
    Identity,
    IdentityScope,
    Isolation,
    IsolationScope,
    SandboxAcquired,
    SandboxAttachedIdentityNotPermitted,
    SandboxBackendNotPermitted,
    SandboxCapabilityDenied,
    SandboxCapabilityNotSupported,
    SandboxKey,
    SandboxRouter,
    SandboxSpec,
    Selection,
)
from maf_sandbox.maf import sandbox_tool_declarations
from maf_sandbox.testing import FAKE_BACKEND_DECLARATIONS, InProcessSandboxBackend

CHANNELS = frozenset({AuthorityChannel.EGRESS_HEADER})
SCOPES = tuple(scope for scope in IdentityScope if scope is not IdentityScope.NONE)
KEY = SandboxKey(scope="scope", thread_id="thread", agent_dir="agent")


def attached_spec(scope: IdentityScope = IdentityScope.SHARED, seconds: int = 60) -> SandboxSpec:
    return SandboxSpec(
        kind="authority",
        requires=DEFAULT_CAPABILITIES | {Capability.ATTACHED_IDENTITY},
        max_identity_scope=scope,
        max_identity_retention_seconds=seconds,
        egress=Egress.ALLOWLIST,
        egress_allow=(EgressRule("api.example", authority="urn:example:resource"),),
    )


def provider(
    scope: IdentityScope = IdentityScope.PER_SANDBOX,
    seconds: int = 60,
    name: str = "attached",
) -> InProcessSandboxBackend:
    return InProcessSandboxBackend(
        name=name,
        declarations=dataclasses.replace(
            FAKE_BACKEND_DECLARATIONS,
            capabilities=DEFAULT_CAPABILITIES | {Capability.ATTACHED_IDENTITY},
            attached_identity=AttachedIdentity(scope, seconds, CHANNELS),
        ),
    )


def router(backend: InProcessSandboxBackend, **kwargs: Any) -> SandboxRouter:
    return SandboxRouter([backend], min_isolation=Isolation.NONE, **kwargs)


def test_scope_order_is_exhaustive_and_sharing_only_widens():
    assert set(IDENTITY_SCOPE_RANK) == set(IdentityScope)
    assert sorted(IdentityScope, key=IDENTITY_SCOPE_RANK.__getitem__) == [
        IdentityScope.NONE,
        IdentityScope.PER_SANDBOX,
        IdentityScope.PER_SCOPE,
        IdentityScope.SHARED,
    ]
    assert len(set(IDENTITY_SCOPE_RANK.values())) == len(IdentityScope)


@pytest.mark.parametrize("scope", [*IdentityScope, "per_scope"])
def test_scopes_normalize(scope: Any):
    attachment = (
        AttachedIdentity(scope)
        if scope == IdentityScope.NONE
        else AttachedIdentity(scope, 60, CHANNELS)
    )
    assert isinstance(attachment.scope, IdentityScope)


@pytest.mark.parametrize("value", [None, 0, -1, True, False, 1.5, float("inf"), float("nan"), "60"])
def test_both_retention_bounds_require_positive_integers(value: Any):
    with pytest.raises(ValueError, match="positive integer"):
        AttachedIdentity(IdentityScope.SHARED, value, CHANNELS)
    with pytest.raises(ValueError, match="positive integer"):
        dataclasses.replace(attached_spec(), max_identity_retention_seconds=value)


@pytest.mark.parametrize("scope", [None, IdentityScope.NONE])
def test_opt_in_requires_a_sharing_bound(scope: Any):
    with pytest.raises(ValueError, match="max_identity_scope"):
        dataclasses.replace(attached_spec(), max_identity_scope=scope)


def test_user_authority_cannot_be_an_attachment_scope():
    with pytest.raises(ValueError):
        AttachedIdentity(Identity.USER, 60, CHANNELS)  # type: ignore[arg-type]


@pytest.mark.parametrize("change", [{"channels": CHANNELS}, {"auto_delete_seconds": 60}])
def test_no_attachment_cannot_carry_channels_or_retention(change: dict[str, Any]):
    with pytest.raises(ValueError, match="no attached identity"):
        AttachedIdentity(**change)


@pytest.mark.parametrize("channels", [frozenset(), frozenset({"guest_endpoint"})])
def test_no_unbounded_or_unknown_channel(channels: Any):
    with pytest.raises(ValueError):
        AttachedIdentity(IdentityScope.SHARED, 60, channels)


@pytest.mark.parametrize("channels", [None, "egress_header", ["egress_header"]])
def test_channel_collection_cannot_be_misread(channels: Any):
    with pytest.raises(TypeError):
        AttachedIdentity(IdentityScope.SHARED, 60, channels)


def test_defaults_attach_nothing_including_the_shipped_fake():
    assert BackendDeclarations().attached_identity == NO_ATTACHED_IDENTITY
    assert FAKE_BACKEND_DECLARATIONS.attached_identity == NO_ATTACHED_IDENTITY
    backend = InProcessSandboxBackend()
    router(backend).ensure_can_serve(SandboxSpec(kind="ordinary"))
    with pytest.raises(SandboxCapabilityNotSupported):
        router(backend, max_identity_scope=IdentityScope.SHARED).ensure_can_serve(attached_spec())


@pytest.mark.parametrize("selection", list(Selection))
@pytest.mark.parametrize("has_capability,has_attachment", [(False, True), (True, False)])
def test_inconsistent_declarations_refuse_at_construction(
    selection: Selection, has_capability: bool, has_attachment: bool
):
    declaration = dataclasses.replace(
        FAKE_BACKEND_DECLARATIONS,
        capabilities=DEFAULT_CAPABILITIES
        | ({Capability.ATTACHED_IDENTITY} if has_capability else set()),
        attached_identity=AttachedIdentity(IdentityScope.SHARED, 60, CHANNELS)
        if has_attachment
        else NO_ATTACHED_IDENTITY,
    )
    with pytest.raises(SandboxBackendNotPermitted, match="capabilities and attached_identity"):
        router(
            InProcessSandboxBackend(declarations=declaration),
            max_identity_scope=IdentityScope.SHARED,
            selection=selection,
        )


@pytest.mark.parametrize("selection", list(Selection))
def test_bad_declaration_does_not_hide_behind_an_ordinary_candidate(selection: Selection):
    backend = InProcessSandboxBackend(
        declarations=dataclasses.replace(FAKE_BACKEND_DECLARATIONS, attached_identity=None)  # type: ignore[arg-type]
    )
    with pytest.raises(SandboxBackendNotPermitted, match="attached_identity"):
        SandboxRouter(
            [backend, InProcessSandboxBackend()], min_isolation=Isolation.NONE, selection=selection
        )


@pytest.mark.parametrize("actual,host,workload", itertools.product(SCOPES, IdentityScope, SCOPES))
@pytest.mark.parametrize("selection", list(Selection))
def test_scope_matrix(
    actual: IdentityScope, host: IdentityScope, workload: IdentityScope, selection: Selection
):
    admitted = IDENTITY_SCOPE_RANK[actual] <= min(
        IDENTITY_SCOPE_RANK[host], IDENTITY_SCOPE_RANK[workload]
    )
    if admitted:
        router(provider(actual), max_identity_scope=host, selection=selection).ensure_can_serve(
            attached_spec(workload)
        )
    else:
        with pytest.raises(SandboxAttachedIdentityNotPermitted):
            router(provider(actual), max_identity_scope=host, selection=selection).ensure_can_serve(
                attached_spec(workload)
            )


@pytest.mark.parametrize("selection", list(Selection))
@pytest.mark.parametrize("seconds", [59, 60, 61])
def test_retention_matrix(selection: Selection, seconds: int):
    subject = router(
        provider(seconds=seconds), max_identity_scope=IdentityScope.SHARED, selection=selection
    )
    if seconds <= 60:
        subject.ensure_can_serve(attached_spec())
    else:
        with pytest.raises(SandboxAttachedIdentityNotPermitted, match="retention"):
            subject.ensure_can_serve(attached_spec())


@pytest.mark.parametrize("selection", list(Selection))
def test_host_permission_does_not_substitute_for_workload_opt_in(selection: Selection):
    subject = router(provider(), max_identity_scope=IdentityScope.SHARED, selection=selection)
    with pytest.raises(SandboxAttachedIdentityNotPermitted, match="ambient"):
        subject.ensure_can_serve(SandboxSpec(kind="ordinary"))


def test_no_authority_rule_refuses_the_header_channel():
    spec = dataclasses.replace(attached_spec(), egress=Egress.CLOSED, egress_allow=())
    with pytest.raises(SandboxAttachedIdentityNotPermitted, match="channels"):
        router(provider(), max_identity_scope=IdentityScope.SHARED).ensure_can_serve(spec)
    assert sandbox_tool_declarations(spec, outbound_max_confidentiality="private") == {
        "max_allowed_confidentiality": "private"
    }


def test_host_can_deny_the_opted_in_capability():
    subject = router(
        provider(),
        max_identity_scope=IdentityScope.SHARED,
        denied_capabilities={Capability.ATTACHED_IDENTITY},
    )
    with pytest.raises(SandboxCapabilityDenied):
        subject.ensure_can_serve(attached_spec())


def test_per_spec_routes_ordinary_and_attached_workloads_separately():
    ordinary = InProcessSandboxBackend(name="ordinary")
    attached = provider()
    subject = SandboxRouter(
        [attached, ordinary],
        min_isolation=Isolation.NONE,
        selection=Selection.PER_SPEC,
        max_identity_scope=IdentityScope.SHARED,
    )
    assert subject.backend_for(SandboxSpec(kind="ordinary")) is ordinary
    assert subject.backend_for(attached_spec()) is attached
    blocked = SandboxRouter(
        [attached, ordinary], min_isolation=Isolation.NONE, selection=Selection.PER_SPEC
    )
    assert blocked.backend_for(SandboxSpec(kind="ordinary")) is ordinary


def test_cold_and_warm_acquire_recheck_declarations_before_backend_call():
    async def run():
        backend = provider()
        subject = router(backend, max_identity_scope=IdentityScope.SHARED)
        spec = attached_spec()
        first = await subject.acquire(KEY, spec)
        assert await subject.acquire(KEY, spec) is first
        backend._declarations = dataclasses.replace(
            backend.declarations,
            attached_identity=AttachedIdentity(IdentityScope.SHARED, 61, CHANNELS),
        )
        with pytest.raises(SandboxAttachedIdentityNotPermitted, match="retention"):
            await subject.acquire(KEY, spec)
        await subject.dispose(KEY)

    asyncio.run(run())


@pytest.mark.parametrize("audience", ["", " ", "a b", "a\n", "a\x00", 123])
def test_authority_audience_validation(audience: Any):
    with pytest.raises(ValueError, match="audience"):
        EgressRule("api.example", authority=audience)


@pytest.mark.parametrize("codepoint", [*range(0x20), *range(0x7F, 0xA0)])
def test_authority_rejects_every_control_character(codepoint: int):
    with pytest.raises(ValueError, match="audience"):
        EgressRule("api.example", authority=f"urn:resource:{chr(codepoint)}")


@pytest.mark.parametrize("codepoint", [0x7E, 0xA1, 0xFF])
def test_authority_preserves_noncontrol_characters(codepoint: int):
    audience = f"urn:resource:{chr(codepoint)}"
    assert EgressRule("api.example", authority=audience).authority == audience


def test_authority_rejects_wildcard_destinations():
    with pytest.raises(ValueError, match="concrete host"):
        EgressRule("*.example", authority="urn:resource")


def test_rule_requires_explicit_opt_in():
    with pytest.raises(ValueError, match="requires"):
        SandboxSpec(
            kind="ambient",
            egress=Egress.ALLOWLIST,
            egress_allow=(EgressRule("api.example", authority="urn:resource"),),
        )


def test_canonicalization_preserves_audience_and_derives_methods_only_for_restriction():
    first = EgressRule("API.example", authority="urn:Resource")
    spec = dataclasses.replace(
        attached_spec(), egress_allow=(first, EgressRule("api.example", authority="urn:Resource"))
    )
    assert spec.egress_allow == (first,)
    assert spec.required_capabilities == DEFAULT_CAPABILITIES | {Capability.ATTACHED_IDENTITY}
    restricted = dataclasses.replace(
        spec, egress_allow=(dataclasses.replace(first, methods=("GET",)),)
    )
    assert restricted.required_capabilities == spec.required_capabilities | {
        Capability.EGRESS_METHODS
    }


@pytest.mark.parametrize(
    "other",
    ["api.example", EgressRule("api.example"), EgressRule("api.example", authority="urn:resource")],
)
def test_conflicting_audiences_including_plain_hosts_refuse(other: str | EgressRule):
    with pytest.raises(ValueError, match="conflicting"):
        dataclasses.replace(
            attached_spec(),
            egress_allow=(EgressRule("api.example", authority="urn:Resource"), other),
        )


def test_vocabulary_and_rule_json_round_trip():
    spec = attached_spec()
    rule = spec.egress_allow[0]
    assert isinstance(rule, EgressRule)
    assert EgressRule(**json.loads(json.dumps(dataclasses.asdict(rule)))) == rule
    encoded = json.loads(
        json.dumps(
            dataclasses.asdict(AttachedIdentity(IdentityScope.PER_SCOPE, 60, CHANNELS)),
            default=sorted,
        )
    )
    encoded["channels"] = frozenset(encoded["channels"])
    assert AttachedIdentity(**encoded) == AttachedIdentity(IdentityScope.PER_SCOPE, 60, CHANNELS)
    assert (
        json.loads(json.dumps(dataclasses.asdict(spec), default=sorted))["max_identity_scope"]
        == "shared"
    )


def test_effective_state_preserves_authority_and_distinguishes_unreadable_from_none():
    event = SandboxAcquired(
        key=KEY,
        spec=attached_spec(),
        backend="attached",
        declarations=provider().declarations,
        isolation_scope=IsolationScope.CONVERSATION,
        isolation=Isolation.NONE,
        seconds=0,
    )
    snapshot = EffectiveState.of(event)
    assert snapshot is not None
    encoded = json.loads(json.dumps(snapshot.as_dict()))
    assert encoded["egress_allow"] == [
        {"host": "api.example", "methods": None, "authority": "urn:example:resource"}
    ]
    assert encoded["attached_identity"] == {
        "scope": "per_sandbox",
        "auto_delete_seconds": 60,
        "channels": ["egress_header"],
    }
    assert encoded["max_identity_scope"] == "shared"
    assert encoded["max_identity_retention_seconds"] == 60
    for declarations, expected in [
        (None, None),
        (FAKE_BACKEND_DECLARATIONS, {"scope": "none", "auto_delete_seconds": None, "channels": []}),
    ]:
        ordinary = EffectiveState.of(dataclasses.replace(event, declarations=declarations))
        assert ordinary is not None
        assert ordinary.as_dict()["attached_identity"] == expected
