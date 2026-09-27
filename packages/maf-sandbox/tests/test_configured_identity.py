"""Host-reported identity stays observable without satisfying enforced attachment."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace

import pytest

from maf_sandbox import (
    BackendDeclarations,
    Capability,
    ConfiguredIdentity,
    EffectiveState,
    IdentityScope,
    Isolation,
    SandboxBackendNotPermitted,
    SandboxCapabilityNotSupported,
    SandboxKey,
    SandboxObserver,
    SandboxRouter,
    SandboxSpec,
    Selection,
)
from maf_sandbox._effective_state import close_effective_state_notes, open_effective_state_notes
from maf_sandbox.testing import FAKE_BACKEND_DECLARATIONS, InProcessSandboxBackend


def test_configuration_validation():
    identity = ConfiguredIdentity("shared", True)
    assert identity.scope is IdentityScope.SHARED
    with pytest.raises(ValueError):
        ConfiguredIdentity("unknown")
    with pytest.raises(TypeError, match="bool"):
        ConfiguredIdentity(IdentityScope.SHARED, 1)
    with pytest.raises(ValueError, match="cannot expose"):
        ConfiguredIdentity(IdentityScope.NONE, True)
    assert BackendDeclarations().configured_identity is None


def test_invalid_description_is_not_silently_ignored():
    backend = InProcessSandboxBackend(
        declarations=replace(FAKE_BACKEND_DECLARATIONS, configured_identity={"scope": "shared"})
    )
    with pytest.raises(SandboxBackendNotPermitted, match="configured_identity"):
        SandboxRouter([backend], min_isolation=Isolation.NONE)


@pytest.mark.parametrize("selection", list(Selection))
@pytest.mark.parametrize(
    "identity",
    [None, ConfiguredIdentity(), ConfiguredIdentity(IdentityScope.SHARED, True)],
)
def test_served_state_reports_configuration_without_changing_admission(selection, identity):
    backend = InProcessSandboxBackend(
        name="configured",
        declarations=replace(FAKE_BACKEND_DECLARATIONS, configured_identity=identity),
    )
    other = InProcessSandboxBackend(
        name="other", declarations=replace(FAKE_BACKEND_DECLARATIONS, capabilities=frozenset())
    )
    # The first registered backend cannot serve EXEC when routing per spec.
    backends = [other, backend] if selection is Selection.PER_SPEC else [backend, other]
    events = []

    class Observer(SandboxObserver):
        def sandbox_acquired(self, event):
            events.append(event)

    router = SandboxRouter(
        backends, min_isolation=Isolation.NONE, selection=selection, observer=Observer()
    )
    notes, token = open_effective_state_notes()
    try:
        asyncio.run(router.acquire(SandboxKey("tenant", "thread", "agent"), SandboxSpec(kind="k")))
    finally:
        close_effective_state_notes(token)
    (state,) = notes
    assert state.backend == "configured"
    assert state.configured_identity == identity
    assert events[-1].declarations.configured_identity == identity
    assert EffectiveState.of(events[-1]) == state
    encoded = json.loads(json.dumps(state.as_dict()))
    assert encoded["configured_identity"] == (
        None
        if identity is None
        else {
            "scope": str(identity.scope),
            "guest_token_endpoint": identity.guest_token_endpoint,
            "provenance": "host_configuration",
            "authority_lifetime_seconds": None,
        }
    )
    strict = SandboxSpec(
        kind="strict",
        requires=frozenset({Capability.ATTACHED_IDENTITY}),
        max_identity_scope=IdentityScope.SHARED,
        max_identity_retention_seconds=60,
    )
    with pytest.raises(SandboxCapabilityNotSupported):
        router.ensure_can_serve(strict)
