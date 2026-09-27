"""Trusted group descriptions and acquisition scope boundaries, without Azure reads."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest
from maf_sandbox import (
    NO_ATTACHED_IDENTITY,
    Capability,
    ConfiguredIdentity,
    IdentityScope,
    SandboxCapabilityNotSupported,
    SandboxKey,
    SandboxRouter,
    SandboxSpec,
)
from maf_sandbox._effective_state import close_effective_state_notes, open_effective_state_notes
from test_acas_backend import (
    _backend_with,
    _config,
    _guest_removing,
    _GuestGroupClient,
    _spec_requiring,
)

from maf_sandbox_acas import (
    AcasGroupIdentity,
    AcasIdentityScopeMismatch,
    AcasSandboxBackend,
    AcasSandboxConfig,
)


class _ClosingGroupClient(_GuestGroupClient):
    async def close(self):
        pass


@pytest.mark.parametrize(
    "kwargs",
    [
        {"scope": IdentityScope.PER_SANDBOX},
        {"scope": IdentityScope.PER_SCOPE},
        {"scope": IdentityScope.PER_SCOPE, "scope_id": ""},
        {"scope": IdentityScope.PER_SCOPE, "scope_id": "   "},
        {"scope": IdentityScope.PER_SCOPE, "scope_id": 1},
        {"scope": IdentityScope.SHARED, "scope_id": "tenant"},
        {"scope": IdentityScope.NONE, "scope_id": "tenant"},
        {"scope": "invalid"},
    ],
)
def test_invalid_group_configuration_refuses(kwargs):
    with pytest.raises(ValueError):
        AcasGroupIdentity(**kwargs)


def test_scope_normalization_and_config_validation():
    identity = AcasGroupIdentity("per_scope", "private-scope")
    assert identity.scope is IdentityScope.PER_SCOPE
    assert "private-scope" not in repr(identity)
    assert identity.declaration == ConfiguredIdentity(IdentityScope.PER_SCOPE, True)
    with pytest.raises(TypeError, match="group_identity"):
        AcasSandboxConfig(endpoint="https://example.com", group_identity={"scope": "shared"})


@pytest.mark.parametrize(
    ("identity", "description"),
    [
        (None, None),
        (AcasGroupIdentity(IdentityScope.NONE), ConfiguredIdentity()),
        (AcasGroupIdentity(), ConfiguredIdentity(IdentityScope.SHARED, True)),
        (
            AcasGroupIdentity(IdentityScope.PER_SCOPE, "tenant"),
            ConfiguredIdentity(IdentityScope.PER_SCOPE, True),
        ),
    ],
)
def test_configuration_preserves_ordinary_admission_and_strict_refusal(identity, description):
    backend = AcasSandboxBackend(_config(group_identity=identity))
    assert backend.declarations.configured_identity == description
    assert backend.declarations.attached_identity == NO_ATTACHED_IDENTITY
    assert Capability.ATTACHED_IDENTITY not in backend.declarations.capabilities
    router = SandboxRouter([backend])
    router.ensure_can_serve(SandboxSpec(kind="ordinary"))
    with pytest.raises(SandboxCapabilityNotSupported):
        router.ensure_can_serve(
            SandboxSpec(
                kind="strict",
                requires=frozenset({Capability.ATTACHED_IDENTITY}),
                max_identity_scope=IdentityScope.SHARED,
                max_identity_retention_seconds=60,
            )
        )


@pytest.mark.parametrize("warm", [False, True])
def test_wrong_scope_refuses_before_credentials_or_service_even_for_a_held_sandbox(warm):
    async def scenario():
        client = _ClosingGroupClient(_guest_removing(True))
        backend = _backend_with(client)
        key = SandboxKey("other-tenant", "thread", "agent")
        spec = _spec_requiring(Capability.EXEC)
        if warm:
            await backend.acquire(key, spec)
        resolver = AsyncMock(side_effect=AssertionError("credentials must not be resolved"))
        backend._config = replace(
            backend._config,
            group_identity=AcasGroupIdentity(IdentityScope.PER_SCOPE, "tenant"),
            credential_resolver=resolver,
        )
        before = list(client.probes)
        with pytest.raises(AcasIdentityScopeMismatch, match="another caller scope") as caught:
            await backend.acquire(key, spec)
        assert "other-tenant" not in str(caught.value)
        assert client.probes == before
        resolver.assert_not_awaited()
        await backend.aclose()

    asyncio.run(scenario())


def test_matching_scope_reuses_and_cleanup_can_reach_a_previous_scope():
    async def scenario():
        client = _ClosingGroupClient(_guest_removing(True))
        backend = _backend_with(
            client, _config(group_identity=AcasGroupIdentity(IdentityScope.PER_SCOPE, "tenant"))
        )
        key = SandboxKey("tenant", "thread", "agent")
        spec = _spec_requiring(Capability.EXEC)
        notes, token = open_effective_state_notes()
        try:
            first = await SandboxRouter([backend]).acquire(key, spec)
        finally:
            close_effective_state_notes(token)
        assert notes[0].configured_identity == ConfiguredIdentity(IdentityScope.PER_SCOPE, True)
        assert "tenant" not in json.dumps(notes[0].as_dict())
        second = await backend.acquire(key, spec)
        assert first.instance_id == second.instance_id
        backend._config = replace(
            backend._config, group_identity=AcasGroupIdentity(IdentityScope.PER_SCOPE, "new-tenant")
        )
        assert await backend.dispose(key) is None
        assert first.instance_id in client.deleted
        await backend.aclose()

    asyncio.run(scenario())
