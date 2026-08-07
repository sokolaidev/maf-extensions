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


# ---------------------------------------------------------------------------
# Independence from the host application — the invariant the split exists for
# ---------------------------------------------------------------------------

#: The one place this distribution names the application it currently ships inside.  It is
#: here because the guard below needs something to look for; everywhere else the host is
#: referred to by role, so moving this tree to its own repository is a file move plus this
#: single line.
_HOST_PACKAGE = "ats"


class TestNoHostDependency:
    """This package must not import the application it currently ships inside.

    Everything else here would keep passing if someone added ``from <host>.config import
    Settings`` to a module — the tests run in a process where the host package is
    importable, so the coupling would be invisible until the day someone tried to extract
    the package.  A source scan suffices: this module's only imports are the standard
    library (see :class:`TestZeroDependencies` below), so the host cannot arrive
    transitively.  Each sandbox distribution carries its own copy of this scan over its own
    sources, so extracting any one of them keeps its guard.
    """

    def _sources(self):
        import pathlib

        import sandbox_router

        root = pathlib.Path(sandbox_router.__file__).parent  # type: ignore[arg-type]
        distribution = root.parent.parent
        paths = []
        # `scripts` does not exist here today; the guarded leg keeps a future operator
        # script inside the scan instead of silently outside it (the backend has one).
        for directory in (root, distribution / "tests", distribution / "scripts"):
            if not directory.is_dir():
                continue
            paths.extend(directory.rglob("*.py"))
        return paths

    def test_sources_exist(self):
        """Guards the scan below against silently finding nothing."""
        assert len(self._sources()) >= 5

    def test_nothing_imports_the_host_application(self):
        import re

        host = re.escape(_HOST_PACKAGE)
        pattern = re.compile(rf"(?m)^\s*(?:from\s+{host}[.\s]|import\s+{host}[.\s])")
        offenders = [
            str(p) for p in self._sources() if pattern.search(p.read_text(encoding="utf-8"))
        ]
        assert offenders == [], (
            f"these files import the host application ({_HOST_PACKAGE!r}): {offenders}. "
            "The dependency belongs in the host's own adapter module, reaching this "
            "package through WorkspaceContext and the router."
        )


class TestZeroDependencies:
    """`pyproject.toml` declares `dependencies = []` — this is what makes that claim true.

    This layer is protocol and policy: giving it a backend dependency, or a MAF one, would
    make it the thing it exists to keep apart (see the module docstring). Nothing else pins
    that; a dependency added to a module without a matching `pyproject.toml` entry would
    still import fine in this workspace, because every other member is already on the path.
    """

    def test_the_module_imports_nothing_outside_the_standard_library(self):
        import ast
        import pathlib
        import sys

        import sandbox_router

        root = pathlib.Path(sandbox_router.__file__).parent  # type: ignore[arg-type]
        stdlib = set(sys.stdlib_module_names)
        offenders: list[str] = []
        for path in root.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        top = alias.name.split(".")[0]
                        if top != "__future__" and top not in stdlib:
                            offenders.append(f"{path.name}: import {alias.name}")
                elif isinstance(node, ast.ImportFrom):
                    if node.level > 0:
                        continue  # relative import — within this package, not a dependency
                    top = (node.module or "").split(".")[0]
                    if top and top != "__future__" and top not in stdlib:
                        offenders.append(f"{path.name}: from {node.module} import ...")
        assert offenders == [], (
            f"sandbox_router imports outside the standard library: {offenders}. "
            "Its entire reason to exist is zero dependencies — see pyproject.toml's "
            "`dependencies = []` and the module docstring."
        )
