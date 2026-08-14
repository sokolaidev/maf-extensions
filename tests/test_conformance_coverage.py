"""Serving `FILES_OUT` and answering the shared conformance suite are one decision, not two.

#142's finding was not that a backend got confinement wrong. It was that *two* backends, written
independently against the same sentence, got it wrong the same way — twice each, once for a
symlinked parent and again for the work directory's own ancestors. A rule two careful authors
read and misread is a rule stated somewhere that cannot enforce it.

What answers that is `maf_sandbox.conformance` (#214, #215): the attacks any backend serving
`FILES_OUT` must survive, planted through the backend's own public surface. Both shipped
backends run it. Nothing *makes* them — and "every author remembers" is the assumption #142
exists to retire.

So this is the binding. Declare the capability in a package under `packages/`, and that
package's tests must call the suite. It is a wiring check and says so: it proves the probes are
pointed at the backend, not that they ran on any particular machine — the docker leg needs a
real engine, and the acas leg answers a simulator built from live payloads because a pull
request cannot assume a subscription. What it removes is the failure this repository actually
had, which was not a probe that ran and passed weakly but a rule nobody was held to at all.

**It fails closed.** A declaration this file cannot read is a failure, not a pass. A guard that
goes quiet when the code it reads is refactored is worth less than no guard, because it also
stops anyone from noticing.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PACKAGES = REPO_ROOT / "packages"
SAMPLES = REPO_ROOT / "samples"

#: The name a package's tests have to call.
SUITE = "assert_files_out_conformance"

#: The package that *ships* the suite. Its only backend is `maf_sandbox.testing`'s fake, whose
#: capabilities are whatever its constructor was handed, so there is no declaration here to
#: read. Not a hole — `test_the_package_that_ships_the_suite_answers_it_too` holds the fake to
#: the same probes.
CORE = "maf-sandbox"

#: Names this file resolves without importing anything, pinned against the real value by
#: `test_the_default_this_file_assumes_is_the_default_core_ships`.
DEFAULT_CAPABILITY_NAMES = frozenset({"EXEC", "FILES_IN"})


def _capability_members(node: ast.AST) -> frozenset[str]:
    """Every ``Capability.X`` named anywhere under ``node``."""
    return frozenset(
        child.attr
        for child in ast.walk(node)
        if isinstance(child, ast.Attribute)
        and isinstance(child.value, ast.Name)
        and child.value.id == "Capability"
    )


def _module_constants(tree: ast.Module) -> dict[str, ast.expr]:
    """Module-level assignments, so ``return _CAPABILITIES`` reads as well as writing it out."""
    constants: dict[str, ast.expr] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    constants[target.id] = node.value
        elif (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.value
        ):
            constants[node.target.id] = node.value
    return constants


def _declared_capabilities(
    prop: ast.FunctionDef | ast.AsyncFunctionDef, constants: dict[str, ast.expr]
) -> frozenset[str] | None:
    """What a ``capabilities`` property declares, or ``None`` when this guard cannot tell.

    Three shapes are read, and the second and third are deliberate rather than generous: a
    backend keeping its set in a module constant, or returning core's ``DEFAULT_CAPABILITIES``,
    has declared exactly as much as one writing the frozenset inline, and a guard that a rename
    switches off is not one. Anything past that — a helper call, a constructor argument, a set
    assembled at runtime — answers ``None``, which fails rather than passes.
    """
    direct = _capability_members(prop)
    if direct:
        return direct
    for node in ast.walk(prop):
        if isinstance(node, ast.Name):
            if node.id == "DEFAULT_CAPABILITIES":
                return DEFAULT_CAPABILITY_NAMES
            if node.id in constants:
                indirect = _capability_members(constants[node.id])
                if indirect:
                    return indirect
    return None


def _method(
    klass: ast.ClassDef, name: str
) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    for node in klass.body:
        if (
            isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
            and node.name == name
        ):
            return node
    return None


def _is_property(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    return any(
        isinstance(decorator, ast.Name) and decorator.id == "property"
        for decorator in node.decorator_list
    )


def _backends(root: Path) -> list[tuple[str, str, frozenset[str] | None]]:
    """``(module, class, capabilities)`` for every backend under ``root``.

    Backend-shaped means ``acquire``, the one method the protocol cannot do without, or a
    ``capabilities`` property. Both, rather than the property alone, because **silence is a
    declaration too**: `capabilities` is optional and omitting it reads as
    ``DEFAULT_CAPABILITIES``, so a class discovered only by its property would let a backend
    disappear from this guard by deleting six lines.

    A class that says nothing and inherits from something is the case this file will not guess
    at — the answer could be in a base it does not follow — so it answers ``None`` and fails.
    """
    found: list[tuple[str, str, frozenset[str] | None]] = []
    for module in sorted(root.rglob("*.py")):
        if ".venv" in module.parts:
            continue
        tree = ast.parse(module.read_text(encoding="utf-8"))
        constants = _module_constants(tree)
        for klass in ast.walk(tree):
            if not isinstance(klass, ast.ClassDef):
                continue
            prop = _method(klass, "capabilities")
            prop = prop if prop is not None and _is_property(prop) else None
            if prop is None and _method(klass, "acquire") is None:
                continue
            if prop is not None:
                capabilities = _declared_capabilities(prop, constants)
            elif klass.bases:
                capabilities = None
            else:
                capabilities = DEFAULT_CAPABILITY_NAMES
            found.append(
                (module.relative_to(REPO_ROOT).as_posix(), klass.name, capabilities)
            )
    return found


def _calls_the_suite(tests: Path) -> bool:
    """Whether anything under ``tests`` *calls* the suite. A mention in prose does not count."""
    for module in tests.rglob("*.py"):
        tree = ast.parse(module.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            callee = node.func
            name = (
                callee.id
                if isinstance(callee, ast.Name)
                else callee.attr
                if isinstance(callee, ast.Attribute)
                else None
            )
            if name == SUITE:
                return True
    return False


PACKAGE_DIRS = sorted(path.parent for path in PACKAGES.glob("*/pyproject.toml"))
BACKEND_PACKAGES = [path for path in PACKAGE_DIRS if path.name != CORE]


def _serving(root: Path) -> list[str]:
    """``class (module)`` for everything under ``root`` that declares ``FILES_OUT``."""
    return [
        f"{klass} ({module})"
        for module, klass, capabilities in _backends(root)
        if capabilities and "FILES_OUT" in capabilities
    ]


@pytest.mark.parametrize("package", BACKEND_PACKAGES, ids=lambda path: path.name)
def test_a_backend_that_serves_files_out_answers_the_suite(package: Path):
    serving = _serving(package / "src")
    if not serving:
        pytest.skip(f"{package.name} declares no FILES_OUT backend")
    assert _calls_the_suite(package / "tests"), (
        f"{package.name} declares FILES_OUT in {', '.join(serving)} and nothing in its tests "
        f"calls {SUITE}. Two backends written against the prose alone shipped the same "
        "confinement escape (#142); the probes are what that cost bought. Fill in a "
        "`ConformanceSubject` — `PosixGuestSubject` if the guest is Linux and has `ln` — and "
        "await the suite against a real instance."
    )


@pytest.mark.parametrize("package", BACKEND_PACKAGES, ids=lambda path: path.name)
def test_this_guard_can_read_every_backend_it_walks(package: Path):
    """Fail closed. An unreadable declaration is the one way this check goes quiet by itself."""
    unreadable = [
        f"{klass} ({module})"
        for module, klass, capabilities in _backends(package / "src")
        if capabilities is None
    ]
    assert not unreadable, (
        f"this guard cannot tell what {', '.join(unreadable)} declares, so it can no longer "
        "tell whether the conformance suite is required of it. It reads `Capability.X` members "
        "named in the `capabilities` property, in a module-level constant that property "
        "returns, or `DEFAULT_CAPABILITIES`; and it reads a backend with no property at all as "
        "the default set, unless the class has a base whose declaration it cannot follow. "
        "Teach it the new shape rather than leaving it to pass on silence."
    )


def test_the_guard_still_finds_the_backends_that_exist():
    """A rule matching nothing passes every time; this is what stops that going unnoticed."""
    serving = {
        package.name for package in BACKEND_PACKAGES if _serving(package / "src")
    }
    assert {"maf-sandbox-acas", "maf-sandbox-docker"} <= serving, (
        f"expected the two backends that serve FILES_OUT to be found; found {sorted(serving)}. "
        "Either one of them stopped declaring the capability — in which case say so here — or "
        "the discovery stopped seeing it, which would make every other test in this file "
        "vacuous while still green."
    )


def test_the_default_this_file_assumes_is_the_default_core_ships():
    """The one value read by name rather than from the source it is defined in."""
    from maf_sandbox import DEFAULT_CAPABILITIES

    assert {member.name for member in DEFAULT_CAPABILITIES} == DEFAULT_CAPABILITY_NAMES
    assert "FILES_OUT" not in DEFAULT_CAPABILITY_NAMES, (
        "if the default ever grows FILES_OUT, silence stops meaning 'no pull surface' and "
        "every backend that declares nothing needs the suite."
    )


def test_the_package_that_ships_the_suite_answers_it_too():
    """`maf-sandbox` is exempt above because the fake's capabilities are a constructor argument.

    That exemption would be a hole if the fake were not itself held to the probes: it is the
    specimen every kind's tests run against, so a fake that quietly read through a link would
    make the whole suite of suites agree with the bug.
    """
    assert _calls_the_suite(PACKAGES / CORE / "tests")


def test_no_sample_backend_serves_files_out():
    """A tripwire, not a prohibition.

    Samples are where a backend author starts reading, and `samples/09` already implements
    enough of one to be copied. It declares `DEFAULT_CAPABILITIES` and raises
    `NotImplementedError` across the whole pull surface, so there is nothing to confine and
    nothing to hold to the probes. The day that changes this fails — and a sample serving
    `FILES_OUT` while demonstrating none of the confinement it requires is the one shape of
    sample this repository should not ship.
    """
    serving = _serving(SAMPLES)
    assert not serving, (
        f"{', '.join(serving)} declares FILES_OUT. Hold it to `maf_sandbox.conformance` the way "
        "the packages are — from `tests/` at the root, where the sample's own tests live — and "
        "then relax this test to require that rather than to forbid the declaration."
    )
