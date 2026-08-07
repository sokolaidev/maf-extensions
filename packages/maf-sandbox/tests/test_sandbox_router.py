"""Tests for the sandbox router.

The router has exactly two jobs, and both are tested here rather than inferred:

- picking a backend from configuration;
- refusing a backend weaker than a VM boundary when the host says it is deployed.

The second is a security property. A security review put execution in a VM-isolated sandbox
because a shared-kernel container sits next to the host's credentials, and the security
posture doc's threat-model rows now rest on that — so "the router would refuse" needs to be
a test, not a comment.
"""

from __future__ import annotations

import asyncio

import pytest

from maf_sandbox import (
    Isolation,
    NoSandboxBackend,
    SandboxBackend,
    SandboxKey,
    SandboxPurger,
    SandboxRouter,
    SandboxSpec,
)
from maf_sandbox.testing import InProcessSandboxBackend

_KEY = SandboxKey(scope="scope-a", thread_id="thread-1", agent_dir="devops-engineer")
_SPEC = SandboxSpec(kind="test")


class _ExplodingBackend(InProcessSandboxBackend):
    async def dispose_scope(self, scope: str, thread_id: str) -> int:
        raise RuntimeError("service unavailable")

    async def dispose(self, key) -> None:
        raise RuntimeError("service unavailable")


class TestProtocolConformance:
    def test_a_fake_backend_satisfies_the_protocol(self):
        """If this fails the fakes below have drifted from the contract they stand in for."""
        assert isinstance(InProcessSandboxBackend(), SandboxBackend)


class TestSelection:
    def test_no_backends_means_not_enabled(self):
        router = SandboxRouter([])
        assert router.enabled is False
        assert router.backend is None

    def test_defaults_to_the_first_registered_backend(self):
        first, second = (
            InProcessSandboxBackend(name="first"),
            InProcessSandboxBackend(name="second"),
        )
        router = SandboxRouter([first, second])
        assert router.backend is first
        assert router.enabled is True

    def test_selects_by_name(self):
        first, second = (
            InProcessSandboxBackend(name="first"),
            InProcessSandboxBackend(name="second"),
        )
        assert SandboxRouter([first, second], selected="second").backend is second

    def test_unknown_name_raises_with_the_registered_ones_named(self):
        with pytest.raises(NoSandboxBackend, match="registered: aca, fake"):
            SandboxRouter(
                [InProcessSandboxBackend(name="fake"), InProcessSandboxBackend(name="aca")],
                selected="docker",
            )

    def test_acquire_without_a_backend_raises(self):
        with pytest.raises(NoSandboxBackend):
            asyncio.run(SandboxRouter([]).acquire(_KEY, _SPEC))

    def test_acquire_delegates_to_the_selected_backend(self):
        backend = InProcessSandboxBackend()
        sandbox = asyncio.run(SandboxRouter([backend]).acquire(_KEY, _SPEC))
        assert backend.keys == [_KEY]
        assert sandbox is backend.sandbox


class TestDeployedIsolationRule:
    """`SANDBOX_BACKEND=docker` + deployed must fail closed — a hard constraint on the router."""

    def test_vm_isolation_is_permitted_when_deployed(self):
        router = SandboxRouter([InProcessSandboxBackend(isolation=Isolation.VM)], deployed=True)
        assert router.enabled is True

    @pytest.mark.parametrize("isolation", [Isolation.CONTAINER, Isolation.PROCESS])
    def test_weaker_isolation_is_refused_when_deployed(self, isolation):
        with pytest.raises(PermissionError, match="not permitted in a deployed environment"):
            SandboxRouter(
                [InProcessSandboxBackend(name="docker", isolation=isolation)], deployed=True
            )

    def test_the_refusal_happens_at_construction_not_at_first_use(self):
        """A misconfigured deployment must not start with the feature apparently enabled."""
        with pytest.raises(PermissionError):
            SandboxRouter(
                [InProcessSandboxBackend(name="docker", isolation=Isolation.CONTAINER)],
                deployed=True,
            )

    def test_weaker_isolation_is_fine_locally(self):
        router = SandboxRouter(
            [InProcessSandboxBackend(name="docker", isolation=Isolation.CONTAINER)], deployed=False
        )
        assert router.enabled is True

    def test_it_refuses_rather_than_falling_back_to_a_stronger_backend(self):
        """Falling back would hide the misconfiguration — the whole reason this is an error."""
        docker = InProcessSandboxBackend(name="docker", isolation=Isolation.CONTAINER)
        aca = InProcessSandboxBackend(name="aca", isolation=Isolation.VM)
        with pytest.raises(PermissionError):
            SandboxRouter([docker, aca], deployed=True, selected="docker")

    def test_an_unselected_weak_backend_does_not_poison_a_valid_selection(self):
        docker = InProcessSandboxBackend(name="docker", isolation=Isolation.CONTAINER)
        aca = InProcessSandboxBackend(name="aca", isolation=Isolation.VM)
        assert SandboxRouter([aca, docker], deployed=True, selected="aca").backend is aca


class TestPurge:
    def test_dispose_scope_asks_every_backend_not_only_the_selected_one(self):
        """A conversation may have been served while a different backend was configured."""
        first, second = (
            InProcessSandboxBackend(name="first"),
            InProcessSandboxBackend(name="second"),
        )
        total = asyncio.run(SandboxRouter([first, second]).dispose_scope("scope-a", "thread-1"))
        assert total == 2
        assert first.purged == second.purged == [("scope-a", "thread-1")]

    def test_a_failing_backend_does_not_stop_the_others(self):
        good = InProcessSandboxBackend(name="good")
        total = asyncio.run(
            SandboxRouter([_ExplodingBackend(name="bad"), good]).dispose_scope(
                "scope-a", "thread-1"
            )
        )
        assert total == 1
        assert good.purged == [("scope-a", "thread-1")]

    def test_dispose_never_raises(self):
        asyncio.run(SandboxRouter([_ExplodingBackend()]).dispose(_KEY))

    def test_purger_is_duck_typed_on_purge_scoped_thread(self):
        """The host awaits this without importing the class, so the name is the contract."""
        backend = InProcessSandboxBackend()
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


#: The modules the stdlib-only claim covers, named one by one.  An **allowlist**, not a
#: denylist of exemptions: a module added to this package is outside the claim until someone
#: writes it in here, so widening the scan is a line in a diff rather than something that
#: happens by omission.  `TestModuleInventory` pins the complement, so a new module cannot be
#: quietly neither.
_PROTOCOL_MODULES = frozenset({"_error_detail", "_protocol", "_purger", "_router", "testing"})

#: The modules deliberately OUTSIDE the stdlib-only claim.  `maf` is the MAF-glue
#: module, the one place `agent_framework` may be imported — see `TestMafIsTheOnlyMafImporter`
#: for the other half of that rule.  `__init__` is here because it is not protocol either: it
#: re-exports and carries the experimental notice.  Both are still covered by
#: `TestOnlyDeclaredDependencies` below and by the `agent_framework` boundary below; what
#: they are exempt from is only the "standard library and nothing else" claim.
_NON_PROTOCOL_MODULES = frozenset({"__init__", "maf"})

#: The one module allowed to import `agent_framework`.
_MAF_GLUE_MODULE = "maf"


def _package_modules():
    """Every module in the installed `maf_sandbox`, as `{stem: path}`."""
    import pathlib

    import maf_sandbox

    root = pathlib.Path(maf_sandbox.__file__).parent  # type: ignore[arg-type]
    return {path.stem: path for path in root.rglob("*.py")}


def _imported_top_levels(path):
    """The absolute top-level module names imported by the file at `path`."""
    import ast

    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level > 0:
                continue  # relative import — within this package, not a dependency
            top = (node.module or "").split(".")[0]
            if top:
                names.append(top)
    return names


class TestModuleInventory:
    """Every module is either in the stdlib-only claim or explicitly outside it.

    Without this, the allowlist above would decay into a snapshot: a module added later would
    simply never be scanned, and the zero-dependency guarantee would erode silently rather
    than fail. Adding a module has to be a decision — this is where it is recorded.
    """

    def test_every_module_is_classified(self):
        assert set(_package_modules()) == _PROTOCOL_MODULES | _NON_PROTOCOL_MODULES, (
            "a module was added to or removed from maf_sandbox without deciding whether the "
            "stdlib-only claim covers it. Put it in _PROTOCOL_MODULES (and keep it importing "
            "nothing but the standard library) or in _NON_PROTOCOL_MODULES (and say why here)."
        )


class TestZeroDependencies:
    """The protocol modules import nothing but the standard library.

    This layer is protocol and policy: giving THOSE modules a backend dependency, or a MAF
    one, would make them the thing they exist to keep apart (see the package docstring).
    Nothing else pins it; a dependency added to a module without a matching `pyproject.toml`
    entry would still import fine in this workspace, because every other member is already on
    the path.

    The claim is scoped to :data:`_PROTOCOL_MODULES` rather than to the whole distribution:
    the dist now declares `agent-framework-core` for `maf_sandbox.maf`, and a scan that kept
    asserting "nothing here imports anything" would have had to be deleted outright — trading
    a precise, still-true invariant for none at all.
    """

    def test_the_protocol_modules_import_nothing_outside_the_standard_library(self):
        import sys

        stdlib = set(sys.stdlib_module_names)
        offenders: list[str] = []
        for stem, path in sorted(_package_modules().items()):
            if stem not in _PROTOCOL_MODULES:
                continue
            offenders.extend(
                f"{path.name}: import {name}"
                for name in _imported_top_levels(path)
                if name != "__future__" and name not in stdlib
            )
        assert offenders == [], (
            f"maf_sandbox's protocol modules import outside the standard library: "
            f"{offenders}. Their entire reason to exist is zero dependencies — see the "
            "package docstring and pyproject.toml's dependency comment."
        )


class TestMafIsTheOnlyMafImporter:
    """`agent_framework` may be imported by `maf_sandbox.maf` and by nothing else.

    The distribution depends on MAF for that one glue module. The rule keeps that dependency
    from spreading: a protocol module that reached for `agent_framework` would tie the
    vocabulary a backend and a workload share to the framework a *host* happens to use, which
    is the coupling this whole split exists to prevent — and it would do so invisibly, since
    every module here imports fine in a workspace where MAF is installed anyway.
    """

    def test_only_the_glue_module_imports_agent_framework(self):
        offenders = sorted(
            f"{stem} ({path.name})"
            for stem, path in _package_modules().items()
            if stem != _MAF_GLUE_MODULE
            and any(name == "agent_framework" for name in _imported_top_levels(path))
        )
        assert offenders == [], (
            f"these maf_sandbox modules import agent_framework: {offenders}. Only "
            f"{_MAF_GLUE_MODULE!r} may — everything else is the backend-neutral vocabulary a "
            "provider and a workload share, and it must not require the host's framework."
        )

    def test_the_glue_module_really_is_the_importer(self):
        """A boundary nobody is on the far side of would pass by accident forever."""
        glue = _package_modules()[_MAF_GLUE_MODULE]
        assert "agent_framework" in _imported_top_levels(glue), (
            "maf.py no longer imports agent_framework, so the test above proves nothing. "
            "If the glue moved, point _MAF_GLUE_MODULE at its new home."
        )

    def test_importing_the_package_does_not_reach_the_glue(self):
        """`import maf_sandbox` must stay framework-free, which means `__init__` stays clean.

        The glue module's own `agent_framework` import is lazy (inside `sandboxed_tool`, not
        at module top), so importing `.maf` would not itself pull the framework in. The guard
        exists anyway to keep `import maf_sandbox` from pulling in the glue module, full stop
        — a consumer speaking only the protocol should not import code written against a host
        framework it may not use.
        """
        import ast

        source = _package_modules()["__init__"].read_text(encoding="utf-8")
        relative_targets: set[str | None] = set()
        for node in ast.walk(ast.parse(source)):
            if not (isinstance(node, ast.ImportFrom) and node.level > 0):
                continue
            if node.module is not None:
                relative_targets.add(node.module)
            else:
                # `from . import maf` — the target names live in the aliases, not `.module`.
                relative_targets.update(alias.name for alias in node.names)
        assert _MAF_GLUE_MODULE not in relative_targets, (
            f"maf_sandbox/__init__.py imports .{_MAF_GLUE_MODULE}, which makes "
            "`import maf_sandbox` import agent_framework. Consumers reach the glue by name: "
            f"`from maf_sandbox.{_MAF_GLUE_MODULE} import ...`."
        )


# ---------------------------------------------------------------------------
# Dependency discipline — every import must be traceable to a reason
# ---------------------------------------------------------------------------

#: A requirement string's distribution name is not always its import name: `pip install
#: agent-framework-core` puts `agent_framework` on the path, and `azure-identity` and
#: `azure-containerapps-sandbox` both extend the single `azure` namespace package rather
#: than each owning a top-level name of their own. Anything not listed here is assumed to
#: import under its distribution name with hyphens turned to underscores — true of every
#: dependency any of the three maf-sandbox* packages declares today. A dependency where
#: that guess is wrong fails the test below with a readable "imports X" message, which is
#: the right place to notice a new exception belongs here.
_DISTRIBUTION_TO_IMPORT_NAME = {
    "agent-framework-core": "agent_framework",
    "maf-sandbox": "maf_sandbox",
    "azure-identity": "azure",
    "azure-containerapps-sandbox": "azure",
}


def _declared_import_names():
    """The import names `pyproject.toml` licenses `maf_sandbox` to reach for, or `None`.

    `None` means there is no `pyproject.toml` next to the installed package — an
    sdist/wheel-only install with no source tree alongside it — and the caller must skip
    rather than let an empty dependency list pass the scan below vacuously.
    """
    import pathlib
    import re
    import tomllib

    import maf_sandbox

    root = pathlib.Path(maf_sandbox.__file__).parents[2]  # type: ignore[arg-type]
    pyproject_path = root / "pyproject.toml"
    if not pyproject_path.is_file():
        return None

    with pyproject_path.open("rb") as fh:
        requirements = tomllib.load(fh)["project"]["dependencies"]

    names: set[str] = set()
    for requirement in requirements:
        match = re.match(r"[A-Za-z0-9][A-Za-z0-9._-]*", requirement)
        assert match is not None, f"unparseable dependency requirement: {requirement!r}"
        distribution = match.group(0)
        names.add(_DISTRIBUTION_TO_IMPORT_NAME.get(distribution, distribution.replace("-", "_")))
    return names


class TestOnlyDeclaredDependencies:
    """Every module here imports only the standard library, itself, or a declared dependency.

    This is the invariant that replaced ``TestNoHostDependency`` (a source scan for the name
    of the private application these packages were extracted from, back when this package
    lived inside it). That name was one instance of a broader risk: a module reaching for
    anything not on *this package's own* dependency list. Nothing else here would notice —
    the workspace running this suite has every sibling package, and everything a host
    application needs, already importable, so a stray import resolves fine in this
    environment regardless of what it names. The first sign of trouble is a downstream
    consumer who installs the published wheel alone, and what they get is an
    ``ImportError`` with no test pointing at the cause.

    Reading ``pyproject.toml`` at test time, rather than hard-coding the allowed names, is
    what keeps this from becoming a second list to update by hand alongside the first: the
    two would drift, and a stale allowlist is a test that passes for the wrong reason.
    """

    def test_sources_exist(self):
        """Guards the scan below against silently finding nothing."""
        assert len(_package_modules()) >= 7

    def test_every_module_only_imports_what_it_is_declared_to_need(self):
        import sys

        declared = _declared_import_names()
        if declared is None:
            pytest.skip(
                "pyproject.toml is not next to the installed maf_sandbox package — this "
                "check only runs against a source checkout, not an installed-only wheel"
            )

        allowed = set(sys.stdlib_module_names) | declared | {"maf_sandbox"}
        offenders = [
            f"{path.name}: import {name}"
            for _, path in sorted(_package_modules().items())
            for name in _imported_top_levels(path)
            if name not in allowed
        ]
        assert offenders == [], (
            f"these maf_sandbox modules import something outside the standard library, the "
            f"package itself, and pyproject.toml's declared dependencies: {offenders}. "
            "Either the import is a mistake, or the dependency belongs in pyproject.toml."
        )
