"""Tests for the sandbox router (issue #663).

The router has exactly two jobs, and both are tested here rather than inferred:

- picking a backend from configuration;
- refusing a backend weaker than a VM boundary when the host says it is deployed.

The second is a security property. The #408 ruling put execution in a VM-isolated sandbox
because a shared-kernel container sits next to the host's credentials, and the security
posture doc's T3/T7 rows now rest on that — so "the router would refuse" needs to be a test,
not a comment.
"""

from __future__ import annotations

import asyncio

import pytest
from sandbox_router import (
    ExecResult,
    Isolation,
    NoSandboxBackend,
    SandboxBackend,
    SandboxKey,
    SandboxPurger,
    SandboxRouter,
    SandboxSpec,
)

_KEY = SandboxKey(scope="scope-a", thread_id="thread-1", agent_dir="devops-engineer")
_SPEC = SandboxSpec(kind="test")


class _FakeSandbox:
    def __init__(self) -> None:
        self.files: dict[str, str] = {}
        self.commands: list[str] = []

    async def write_file(self, path: str, content: str) -> None:
        self.files[path] = content

    async def exec(self, command: str, *, working_directory: str, timeout: float) -> ExecResult:
        self.commands.append(command)
        return ExecResult(stdout="")


class _FakeBackend:
    """A backend whose isolation is whatever the test needs it to be."""

    def __init__(self, name: str = "fake", isolation: str = Isolation.VM) -> None:
        self._name = name
        self._isolation = isolation
        self.acquired: list[SandboxKey] = []
        self.disposed: list[SandboxKey] = []
        self.purged: list[tuple[str, str]] = []
        self.purge_count = 1
        self.sandbox = _FakeSandbox()

    @property
    def name(self) -> str:
        return self._name

    @property
    def isolation(self) -> str:
        return self._isolation

    async def acquire(self, key, spec):
        self.acquired.append(key)
        return self.sandbox

    async def dispose(self, key) -> None:
        self.disposed.append(key)

    async def dispose_scope(self, scope: str, thread_id: str) -> int:
        self.purged.append((scope, thread_id))
        return self.purge_count


class _ExplodingBackend(_FakeBackend):
    async def dispose_scope(self, scope: str, thread_id: str) -> int:
        raise RuntimeError("service unavailable")

    async def dispose(self, key) -> None:
        raise RuntimeError("service unavailable")


class TestProtocolConformance:
    def test_a_fake_backend_satisfies_the_protocol(self):
        """If this fails the fakes below have drifted from the contract they stand in for."""
        assert isinstance(_FakeBackend(), SandboxBackend)


class TestSelection:
    def test_no_backends_means_not_enabled(self):
        router = SandboxRouter([])
        assert router.enabled is False
        assert router.backend is None

    def test_defaults_to_the_first_registered_backend(self):
        first, second = _FakeBackend("first"), _FakeBackend("second")
        router = SandboxRouter([first, second])
        assert router.backend is first
        assert router.enabled is True

    def test_selects_by_name(self):
        first, second = _FakeBackend("first"), _FakeBackend("second")
        assert SandboxRouter([first, second], selected="second").backend is second

    def test_unknown_name_raises_with_the_registered_ones_named(self):
        with pytest.raises(NoSandboxBackend, match="registered: aca, fake"):
            SandboxRouter([_FakeBackend("fake"), _FakeBackend("aca")], selected="docker")

    def test_acquire_without_a_backend_raises(self):
        with pytest.raises(NoSandboxBackend):
            asyncio.run(SandboxRouter([]).acquire(_KEY, _SPEC))

    def test_acquire_delegates_to_the_selected_backend(self):
        backend = _FakeBackend()
        sandbox = asyncio.run(SandboxRouter([backend]).acquire(_KEY, _SPEC))
        assert backend.acquired == [_KEY]
        assert sandbox is backend.sandbox


class TestDeployedIsolationRule:
    """`SANDBOX_BACKEND=docker` + deployed must fail closed — issue #663's hard constraint."""

    def test_vm_isolation_is_permitted_when_deployed(self):
        router = SandboxRouter([_FakeBackend(isolation=Isolation.VM)], deployed=True)
        assert router.enabled is True

    @pytest.mark.parametrize("isolation", [Isolation.CONTAINER, Isolation.PROCESS])
    def test_weaker_isolation_is_refused_when_deployed(self, isolation):
        with pytest.raises(PermissionError, match="not permitted in a deployed environment"):
            SandboxRouter([_FakeBackend("docker", isolation)], deployed=True)

    def test_the_refusal_happens_at_construction_not_at_first_use(self):
        """A misconfigured deployment must not start with the feature apparently enabled."""
        with pytest.raises(PermissionError):
            SandboxRouter([_FakeBackend("docker", Isolation.CONTAINER)], deployed=True)

    def test_weaker_isolation_is_fine_locally(self):
        router = SandboxRouter([_FakeBackend("docker", Isolation.CONTAINER)], deployed=False)
        assert router.enabled is True

    def test_it_refuses_rather_than_falling_back_to_a_stronger_backend(self):
        """Falling back would hide the misconfiguration — the whole reason this is an error."""
        docker = _FakeBackend("docker", Isolation.CONTAINER)
        aca = _FakeBackend("aca", Isolation.VM)
        with pytest.raises(PermissionError):
            SandboxRouter([docker, aca], deployed=True, selected="docker")

    def test_an_unselected_weak_backend_does_not_poison_a_valid_selection(self):
        docker = _FakeBackend("docker", Isolation.CONTAINER)
        aca = _FakeBackend("aca", Isolation.VM)
        assert SandboxRouter([aca, docker], deployed=True, selected="aca").backend is aca


class TestPurge:
    def test_dispose_scope_asks_every_backend_not_only_the_selected_one(self):
        """A conversation may have been served while a different backend was configured."""
        first, second = _FakeBackend("first"), _FakeBackend("second")
        total = asyncio.run(SandboxRouter([first, second]).dispose_scope("scope-a", "thread-1"))
        assert total == 2
        assert first.purged == second.purged == [("scope-a", "thread-1")]

    def test_a_failing_backend_does_not_stop_the_others(self):
        good = _FakeBackend("good")
        total = asyncio.run(
            SandboxRouter([_ExplodingBackend("bad"), good]).dispose_scope("scope-a", "thread-1")
        )
        assert total == 1
        assert good.purged == [("scope-a", "thread-1")]

    def test_dispose_never_raises(self):
        asyncio.run(SandboxRouter([_ExplodingBackend()]).dispose(_KEY))

    def test_purger_is_duck_typed_on_purge_scoped_thread(self):
        """The host awaits this without importing the class, so the name is the contract."""
        backend = _FakeBackend()
        purger = SandboxPurger(SandboxRouter([backend]))
        assert asyncio.run(purger.purge_scoped_thread("scope-a", "thread-1")) == 1
        assert backend.purged == [("scope-a", "thread-1")]


class TestSpecDefaults:
    def test_egress_defaults_to_denying_everything(self):
        """A spec that forgets to mention egress must get the closed configuration."""
        assert SandboxSpec(kind="test").egress_allow == ()

    def test_labels_are_not_shared_between_specs(self):
        a, b = SandboxSpec(kind="a"), SandboxSpec(kind="b")
        a.labels["x"] = "1"
        assert b.labels == {}
