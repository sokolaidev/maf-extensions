"""Cleanup must earn reuse, respect both policy floors, and dispose at call scope."""

import dataclasses

import pytest

from maf_sandbox import (
    Capability,
    Cleanup,
    Isolation,
    IsolationScope,
    SandboxCapabilityDenied,
    SandboxCapabilityNotSupported,
    SandboxRouter,
    SandboxScopeNotEnforced,
    SandboxSpec,
)
from maf_sandbox._cleanup import established_cleanup, resolve_cleanup
from maf_sandbox.testing import FAKE_BACKEND_DECLARATIONS, InProcessSandboxBackend


@pytest.mark.parametrize("confined", [False, True])
@pytest.mark.parametrize("reclaim", [False, True])
@pytest.mark.parametrize("snapshot", [False, True])
def test_established_rungs_require_their_evidence(confined, reclaim, snapshot):
    caps = ({Capability.RECLAIM} if reclaim else set()) | (
        {Capability.SNAPSHOT} if snapshot else set()
    )
    spec = SandboxSpec(kind="test", confined_to_guest_call_path=confined)
    expected = {Cleanup.DISPOSE}
    if confined and reclaim:
        expected.add(Cleanup.RECLAIM)
    if snapshot:
        expected.add(Cleanup.RESET)
    assert established_cleanup(spec, frozenset(caps)) == expected


@pytest.mark.parametrize(
    "rungs,answers",
    [
        ({Cleanup.DISPOSE}, (Cleanup.DISPOSE, Cleanup.DISPOSE, Cleanup.DISPOSE)),
        ({Cleanup.RECLAIM, Cleanup.DISPOSE}, (Cleanup.RECLAIM, Cleanup.DISPOSE, Cleanup.DISPOSE)),
        ({Cleanup.RESET, Cleanup.DISPOSE}, (Cleanup.RESET, Cleanup.RESET, Cleanup.DISPOSE)),
        (set(Cleanup), (Cleanup.RECLAIM, Cleanup.RESET, Cleanup.DISPOSE)),
    ],
)
def test_resolution_selects_the_weakest_available_rung_at_each_floor(rungs, answers):
    assert tuple(resolve_cleanup(frozenset(rungs), floor) for floor in Cleanup) == answers


@pytest.mark.parametrize("host_floor", list(Cleanup))
@pytest.mark.parametrize("spec_floor", [None, *Cleanup])
@pytest.mark.parametrize(
    "confined,reclaim,snapshot,available",
    [
        (False, False, False, (False, False, True)),
        (False, True, False, (False, False, True)),
        (True, False, False, (False, False, True)),
        (True, True, False, (True, False, True)),
        (False, False, True, (False, True, True)),
        (True, True, True, (True, True, True)),
    ],
)
def test_router_respects_host_and_spec_floors(
    host_floor, spec_floor, confined, reclaim, snapshot, available
):
    caps = ({Capability.RECLAIM} if reclaim else set()) | (
        {Capability.SNAPSHOT} if snapshot else set()
    )
    backend = InProcessSandboxBackend(
        declarations=dataclasses.replace(
            FAKE_BACKEND_DECLARATIONS,
            capabilities=(
                FAKE_BACKEND_DECLARATIONS.capabilities - {Capability.RECLAIM, Capability.SNAPSHOT}
            )
            | caps,
        )
    )
    router = SandboxRouter([backend], min_isolation=Isolation.NONE, min_cleanup=host_floor)
    spec = SandboxSpec(kind="test", confined_to_guest_call_path=confined, min_cleanup=spec_floor)
    rungs = (Cleanup.RECLAIM, Cleanup.RESET, Cleanup.DISPOSE)
    floor = max(rungs.index(host_floor), rungs.index(spec_floor or Cleanup.RECLAIM))
    expected = next(rungs[i] for i in range(floor, 3) if available[i])
    assert router.effective_cleanup(spec) is expected


@pytest.mark.parametrize("at_host", [False, True])
def test_call_scope_always_disposes(at_host):
    backend = InProcessSandboxBackend(
        declarations=dataclasses.replace(
            FAKE_BACKEND_DECLARATIONS,
            capabilities=FAKE_BACKEND_DECLARATIONS.capabilities | set(Capability),
            isolation_scopes=frozenset(IsolationScope),
        )
    )
    router = SandboxRouter(
        [backend],
        min_isolation=Isolation.NONE,
        min_isolation_scope=IsolationScope.CALL if at_host else IsolationScope.CONVERSATION,
    )
    spec = SandboxSpec(
        kind="test",
        confined_to_guest_call_path=True,
        isolation_scope=IsolationScope.CONVERSATION if at_host else IsolationScope.CALL,
    )
    assert router.effective_cleanup(spec) is Cleanup.DISPOSE


@pytest.mark.parametrize("at_host", [False, True])
@pytest.mark.parametrize(
    "reason,refusal",
    [
        ("scope", SandboxScopeNotEnforced),
        ("capability", SandboxCapabilityNotSupported),
        ("denied", SandboxCapabilityDenied),
    ],
)
def test_call_cleanup_preserves_serving_refusals(at_host, reason, refusal):
    backend = InProcessSandboxBackend(
        declarations=dataclasses.replace(
            FAKE_BACKEND_DECLARATIONS,
            isolation_scopes=(
                frozenset({IsolationScope.CONVERSATION})
                if reason == "scope"
                else frozenset(IsolationScope)
            ),
            capabilities=(
                frozenset() if reason == "capability" else FAKE_BACKEND_DECLARATIONS.capabilities
            ),
        )
    )
    router = SandboxRouter(
        [backend],
        min_isolation=Isolation.NONE,
        min_isolation_scope=IsolationScope.CALL if at_host else IsolationScope.CONVERSATION,
        denied_capabilities=frozenset({Capability.EXEC}) if reason == "denied" else frozenset(),
    )
    spec = SandboxSpec(
        kind="test",
        requires=frozenset({Capability.EXEC}),
        isolation_scope=IsolationScope.CONVERSATION if at_host else IsolationScope.CALL,
    )
    with pytest.raises(refusal) as expected:
        router.ensure_can_serve(spec)
    with pytest.raises(refusal) as actual:
        router.effective_cleanup(spec)
    assert str(actual.value) == str(expected.value)
