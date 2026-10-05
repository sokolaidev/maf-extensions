"""Admission and direct-operation checks for every supported Linux grant combination."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace

import pytest
from maf_sandbox import (
    Capability,
    Isolation,
    IsolationScope,
    SandboxCapabilityNotSupported,
    SandboxRouter,
    SandboxScopeNotEnforced,
)
from test_docker_backend import _CALL_A, _CAPS_DROPPED, _KEY, _SPEC, _WORK, _backend_with, _machine

from maf_sandbox_docker import DockerSandboxConfig
from maf_sandbox_docker._backend import _container_name, _DockerResult

GRANTS = ("CHOWN", "DAC_OVERRIDE", "SETUID", "SETGID", "KILL")
COMBINATIONS = [tuple(g for i, g in enumerate(GRANTS) if mask & (1 << i)) for mask in range(32)]


@pytest.mark.parametrize("grants", COMBINATIONS)
@pytest.mark.parametrize("state", ["running", "stopped"])
@pytest.mark.parametrize("policy", ["extra", "implicit", "unknown", "privileged"])
def test_each_combination_refuses_an_unqualified_existing_container(grants, state, policy):
    payload = {
        "extra": json.dumps([["ALL"], [*grants, "SYS_ADMIN"], False]).encode(),
        "implicit": b"[null,null,false]",
        "unknown": b"null",
        "privileged": json.dumps([["ALL"], list(grants), True]).encode(),
    }[policy]
    name = _container_name(_CALL_A, _SPEC.kind)
    backend, fake = _backend_with(
        _machine(
            running=[name] if state == "running" else [],
            stopped=[name] if state == "stopped" else [],
            overrides={next(iter(_CAPS_DROPPED)): _DockerResult(0, payload, "")},
        ),
        DockerSandboxConfig(cap_add=grants),
    )
    error = RuntimeError if policy in {"unknown", "privileged"} else ValueError
    with pytest.raises(error, match="capability policy"):
        asyncio.run(backend.acquire(_CALL_A, _SPEC))
    assert not fake.matching("start")
    assert not fake.matching("exec")
    assert not fake.matching("cp")


@pytest.mark.parametrize("grants", COMBINATIONS)
def test_complete_combinations_enforce_scope_and_deletion(grants):
    backend, fake = _backend_with(_machine(), DockerSandboxConfig(cap_add=grants))
    declarations = backend.declarations
    assert (Capability.FILES_DELETE in declarations.capabilities) is (not grants)
    assert (Capability.RECLAIM in declarations.capabilities) is (not grants)
    assert declarations.isolation_scopes == (
        frozenset({IsolationScope.CALL})
        if grants
        else frozenset({IsolationScope.CALL, IsolationScope.CONVERSATION})
    )
    router = SandboxRouter([backend], min_isolation=Isolation.CONTAINER)
    if grants:
        with pytest.raises(SandboxScopeNotEnforced):
            router.ensure_can_serve(_SPEC)
        with pytest.raises(ValueError, match="call_id"):
            asyncio.run(backend.acquire(_KEY, _SPEC))
        assert not fake.calls
    else:
        router.ensure_can_serve(_SPEC)

    host_call_router = SandboxRouter(
        [backend], min_isolation=Isolation.CONTAINER, min_isolation_scope=IsolationScope.CALL
    )
    host_call_router.ensure_can_serve(_SPEC)
    call_spec = replace(_SPEC, isolation_scope=IsolationScope.CALL)
    router.ensure_can_serve(call_spec)
    delete_spec = replace(call_spec, requires=frozenset({Capability.FILES_DELETE}))
    if grants:
        with pytest.raises(SandboxCapabilityNotSupported):
            router.ensure_can_serve(delete_spec)
        with pytest.raises(SandboxCapabilityNotSupported):
            asyncio.run(backend.acquire(_CALL_A, delete_spec))
        assert not fake.calls

    async def scenario():
        sandbox = await host_call_router.acquire(_CALL_A, _SPEC)
        fake.mark()
        if grants:
            with pytest.raises(NotImplementedError, match="FILES_DELETE"):
                await sandbox.remove("child", working_directory=_WORK, recursive=True)
            with pytest.raises(NotImplementedError, match="RECLAIM"):
                await sandbox.reclaim("child", working_directory=_WORK, timeout=1)
            assert not fake.matching("exec")
            assert not fake.cp_since_mark()
        assert await host_call_router.dispose(_CALL_A) is None

    asyncio.run(scenario())
    workload = fake.only("run").args
    assert workload[workload.index("--cap-drop") + 1] == "ALL"
    assert {workload[i + 1] for i, arg in enumerate(workload) if arg == "--cap-add"} == set(grants)


@pytest.mark.parametrize("drop", [False, None, 0, 1, "true"])
def test_only_literal_true_can_enable_drop_all(drop):
    with pytest.raises(ValueError, match="cap_drop_all must be True"):
        DockerSandboxConfig(cap_drop_all=drop)


@pytest.mark.parametrize(
    "grant", ["SYS_ADMIN", "SYS_MODULE", "SYS_RAWIO", "NET_ADMIN", "NET_RAW", "SETPCAP", "FOWNER"]
)
def test_unsupported_grants_are_not_an_escape_hatch(grant):
    for grants in [(grant,), (*GRANTS, grant)]:
        with pytest.raises(ValueError, match="Unsupported Docker capability combination"):
            DockerSandboxConfig(cap_add=grants)
