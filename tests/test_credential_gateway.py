"""Credential ownership, admission, and host-channel integration across container backends."""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest
from maf_sandbox import (
    Capability,
    Egress,
    EgressRule,
    IdentityScope,
    IsolationScope,
    SandboxKey,
    SandboxSpec,
)
from maf_sandbox.credentials import CredentialGateway, CredentialGrant, GatewayLease
from maf_sandbox_docker import DockerSandboxBackend, DockerSandboxConfig
from maf_sandbox_wslc import WslcSandboxBackend, WslcSandboxConfig


def credential_spec(lifetime=300):
    return SandboxSpec(
        kind="credential-test",
        image="python:3.13-slim",
        egress=Egress.ALLOWLIST,
        egress_allow=(
            EgressRule(
                "api.example.com", methods=("GET",), paths=("/v1/*",), authority="api-audience"
            ),
        ),
        requires=frozenset({Capability.EXEC, Capability.ATTACHED_IDENTITY}),
        isolation_scope=IsolationScope.CALL,
        max_identity_scope=IdentityScope.PER_SANDBOX,
        max_identity_retention_seconds=lifetime,
    )


def grant():
    return CredentialGrant(
        "api-audience", "https://api.example.com:8443", "synthetic-secret", time.time() + 300
    )


@pytest.mark.parametrize("field", ["scope", "thread_id", "agent_id", "call_id"])
def test_empty_ownership_is_refused_before_provider(field):
    key = replace(SandboxKey("tenant/user", "thread", "agent", "call"), **{field: ""})
    with pytest.raises(ValueError, match="trusted scope"):
        GatewayLease.start(CredentialGateway(AsyncMock()), key, credential_spec())


@pytest.mark.parametrize(
    "backend_type,config_type",
    [(DockerSandboxBackend, DockerSandboxConfig), (WslcSandboxBackend, WslcSandboxConfig)],
)
def test_duplicate_audience_is_refused_before_provisioning(backend_type, config_type):
    async def check():
        provider = AsyncMock()
        backend = backend_type(
            config_type(egress_proxy_image="proxy", credential_gateway=CredentialGateway(provider))
        )
        backend._acquire_generation = AsyncMock()
        spec = credential_spec()
        spec = replace(
            spec,
            egress_allow=(
                *spec.egress_allow,
                EgressRule("other.example.com", authority="api-audience"),
            ),
        )
        with pytest.raises(ValueError, match="unique audience"):
            await backend.acquire(SandboxKey("user", "thread", "agent", "call"), spec)
        backend._acquire_generation.assert_not_awaited()
        provider.assert_not_awaited()

    asyncio.run(check())


@pytest.mark.parametrize(
    "changes",
    [
        {"origin": "http://api.example.com"},
        {"origin": "https://name:password@api.example.com"},
        {"origin": "https://api.example.com/path"},
        {"token": "secret\r\nHeader:value"},
        {"token": ""},
        {"expires_at": float("nan")},
        {"expires_at": 1},
    ],
)
def test_invalid_grants_refused_without_disclosing_token(changes):
    with pytest.raises(ValueError) as caught:
        replace(grant(), **changes)
    assert "synthetic-secret" not in str(caught.value)
    assert "synthetic-secret" not in repr(grant())


def test_provider_receives_complete_fresh_boundary_and_exact_audiences():
    async def check():
        provider = AsyncMock(return_value=[grant()])
        gateway = CredentialGateway(provider)
        spec = credential_spec()
        keys = [SandboxKey("tenant/user", "thread", "agent", "call")]
        for field in ("scope", "thread_id", "agent_id", "call_id"):
            keys.append(replace(keys[0], **{field: "other"}))
        generations = set()
        for key in [*keys, keys[0]]:
            lease = GatewayLease.start(gateway, key, spec)
            assert lease is not None
            generations.add(lease.generation)
            payload = json.loads(
                await lease.payload(gateway, key, spec, "runtime-instance", "172.22.0.3", "a" * 48)
            )
            request = provider.call_args.args[0]
            assert request.key == key
            assert request.instance_id == "runtime-instance"
            assert request.audiences == {"api-audience"}
            assert payload["entries"][0]["port"] == 8443
            assert payload["entries"][0]["token"] == "synthetic-secret"
        assert len(generations) == 6
        assert provider.await_count == 6

    asyncio.run(check())


@pytest.mark.parametrize(
    "returned",
    [
        [],
        [grant(), grant()],
        [replace(grant(), audience="foreign")],
        [replace(grant(), origin="https://foreign.example.com")],
    ],
)
def test_incomplete_or_foreign_provider_answer_fails_closed(returned):
    async def check():
        gateway = CredentialGateway(AsyncMock(return_value=returned))
        key = SandboxKey("user", "thread", "agent", "call")
        spec = credential_spec()
        lease = GatewayLease.start(gateway, key, spec)
        assert lease is not None
        with pytest.raises(ValueError):
            await lease.payload(gateway, key, spec, "instance", "172.22.0.3", "a" * 48)

    asyncio.run(check())


@pytest.mark.parametrize(
    "backend_type,config_type",
    [
        (DockerSandboxBackend, DockerSandboxConfig),
        (WslcSandboxBackend, WslcSandboxConfig),
    ],
)
def test_backends_require_proxy_and_generate_independent_instances(backend_type, config_type):
    async def check():
        gateway = CredentialGateway(AsyncMock(return_value=[grant()]))
        with pytest.raises(ValueError, match="proxy image"):
            backend_type(config_type(credential_gateway=gateway))
        config = config_type(egress_proxy_image="proxy", credential_gateway=gateway)
        key = SandboxKey("tenant/user", "thread", "agent", "same-call")
        names = set()
        for backend in [backend_type(config), backend_type(config)]:
            assert Capability.ATTACHED_IDENTITY in backend.declarations.capabilities
            assert backend.declarations.attached_identity == gateway.attached_identity
            backend._acquire_generation = AsyncMock(return_value=object())
            for _ in range(2):
                await backend.acquire(key, credential_spec())
                names.add(backend._acquire_generation.call_args.args[2])
        assert len(names) == 4

    asyncio.run(check())
