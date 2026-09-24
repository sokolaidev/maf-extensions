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
from maf_sandbox_docker._backend import _DockerResult
from maf_sandbox_wslc import WslcSandboxBackend, WslcSandboxConfig
from maf_sandbox_wslc._backend import _WslcResult


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


@pytest.mark.parametrize("engine", ["docker", "wslc"])
@pytest.mark.parametrize("selection", ["kind", "key", "scope"])
@pytest.mark.parametrize("listing", ["full", "workload-only", "failed"])
@pytest.mark.parametrize("resource", ["workload", "proxy", "network"])
def test_incomplete_generation_purge_retries_without_listing(
    monkeypatch, engine, selection, listing, resource
):
    async def check():
        gateway = CredentialGateway(AsyncMock())
        backend = (
            DockerSandboxBackend(
                DockerSandboxConfig(egress_proxy_image="proxy", credential_gateway=gateway)
            )
            if engine == "docker"
            else WslcSandboxBackend(
                WslcSandboxConfig(egress_proxy_image="proxy", credential_gateway=gateway)
            )
        )
        key = SandboxKey("user", "thread", "agent", "call")
        owners = [
            (key, "work"),
            (key, "work"),
            (key, "other-kind"),
            (replace(key, agent_id="other-agent"), "work"),
            (replace(key, call_id="other-call"), "work"),
            (replace(key, scope="other-user"), "work"),
            (replace(key, thread_id="other-thread"), "work"),
        ]
        names = [f"maf-sandbox-{engine}-{i:032x}" for i in range(len(owners))]
        for i, (owner, kind) in enumerate(owners):
            backend._registry[
                (owner.scope, owner.thread_id, owner.agent_id, owner.call_id, kind, f"{i:032x}")
            ] = names[i]
        count = {"kind": 2, "key": 3, "scope": 5}[selection]
        selected = names[:count]
        inventory = [item for name in selected for item in (name, name + "-proxy")]
        if listing == "workload-only":
            inventory = selected
        monkeypatch.setattr(
            backend,
            "_list_names_by_labels",
            AsyncMock(return_value=None if listing == "failed" else inventory),
        )
        monkeypatch.setattr(backend, "_drain_attributed_proxy", AsyncMock(return_value=None))
        remaining = {item for name in names for item in (name, name + "-proxy", name + "-net")}
        failed_target = names[0] + {"workload": "", "proxy": "-proxy", "network": "-net"}[resource]
        refusing = True
        calls = []

        async def command(*args, **kwargs):
            target = args[-1]
            assert args[:2] in {
                ("rm", "-f"),
                ("network", "rm"),
                ("container", "remove"),
                ("network", "remove"),
            }
            calls.append(target)
            if target == failed_target and refusing:
                code, stdout, stderr = 1, b"", "engine refused"
            elif target in remaining:
                remaining.remove(target)
                code, stdout, stderr = 0, target.encode(), ""
            else:
                code, stdout = 1, b""
                if engine == "docker":
                    stderr = (
                        f"No such {'network' if args[0] == 'network' else 'container'}: {target}"
                    )
                else:
                    stderr = (
                        f"network {target} not found"
                        if args[0] == "network"
                        else "WSLC_E_CONTAINER_NOT_FOUND"
                    )
            return (
                _DockerResult(code, stdout, stderr)
                if engine == "docker"
                else _WslcResult(code, stdout, stderr.encode())
            )

        monkeypatch.setattr(backend, "_docker" if engine == "docker" else "_wslc", command)

        async def dispose():
            if selection == "scope":
                report = await backend.dispose_scope(key.scope, key.thread_id)
                return report.undisposed
            return await backend.dispose(key, kind="work" if selection == "kind" else None)

        first = await dispose()
        assert first is not None
        assert failed_target in first.detail
        prefix = (key.scope, key.thread_id, key.agent_id, key.call_id)
        assert backend._undeleted == {prefix: {names[0]}}
        assert backend._undeleted_kinds == {prefix: {names[0]: "work"}}
        untouched = {
            item for name in names[count:] for item in (name, name + "-proxy", name + "-net")
        }
        assert remaining == untouched | {failed_target}
        assert set(backend._registry.values()) == set(names[count:])

        refusing = False
        calls.clear()
        monkeypatch.setattr(backend, "_list_names_by_labels", AsyncMock(return_value=None))
        second = await dispose()
        assert second is not None and second.code == "unlisted"
        assert set(calls) == {names[0], names[0] + "-proxy", names[0] + "-net"}
        assert remaining == untouched
        assert not backend._undeleted and not backend._undeleted_kinds
        assert not backend._disposal_tokens
        assert set(backend._registry.values()) == set(names[count:])

    asyncio.run(check())


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
