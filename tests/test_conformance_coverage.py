"""A package whose backend declares ``FILES_OUT`` must call `maf_sandbox.conformance`'s suite.

Declaring the capability and answering the probes are one decision; this is what makes them
one. Closes the gap #142 left: the suite exists and both backends run it, and nothing held them
to it.

The trap is in the reading, not the rule. Everything here fails closed — a declaration this
file cannot parse, or a suite call pytest would not collect, is a failure rather than a pass,
because a guard that goes quiet under a refactor also stops anyone noticing that it has.
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

#: The package that ships the suite. Its only backend is the fake, whose capabilities are a
#: constructor argument, so there is no declaration to read — and
#: `test_the_package_that_ships_the_suite_answers_it_too` keeps that exemption from being a hole.
CORE = "maf-sandbox"

#: Resolved by name rather than by import, and pinned to the real value by
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
    """Module names bound exactly once, unconditionally, at the top level.

    A name written more than once is not a constant and is dropped rather than resolved to
    whichever write this happened to see last — ``_CAPABILITIES = DEFAULT_CAPABILITIES``
    followed by a rebinding under an ``if`` is a runtime declaration wearing a constant's
    clothes, and reading the first half of it is how a backend declares ``FILES_OUT`` while
    this file records the default.
    """
    written: dict[str, list[ast.expr | None]] = {}

    def note(target: ast.expr, value: ast.expr | None, top_level: bool) -> None:
        if isinstance(target, ast.Name):
            written.setdefault(target.id, []).append(value if top_level else None)

    for node in ast.walk(tree):
        top_level = node in tree.body
        if isinstance(node, ast.Assign):
            for target in node.targets:
                note(target, node.value, top_level)
        elif isinstance(node, ast.AnnAssign) and node.value:
            note(node.target, node.value, top_level)
        elif isinstance(node, ast.AugAssign | ast.NamedExpr):
            # An augmented or walrus write says the name changes; either way it is not one
            # value this file can read off the page.
            note(node.target, None, top_level=False)

    return {
        name: values[0]
        for name, values in written.items()
        if len(values) == 1 and values[0] is not None
    }


def _capabilities_of(
    value: ast.expr, constants: dict[str, ast.expr]
) -> frozenset[str] | None:
    """What one declaring expression says, or ``None`` when this file cannot tell.

    Three shapes: the members written out, core's ``DEFAULT_CAPABILITIES``, and a module-level
    constant holding either. The indirections are read because a name is as much a declaration
    as a literal, and a guard a rename switches off is not one. Anything else — a call, a
    parameter, a set built at runtime — is unreadable, which fails.
    """
    members = _capability_members(value)
    if members:
        return members
    if isinstance(value, ast.Name):
        if value.id == "DEFAULT_CAPABILITIES":
            return DEFAULT_CAPABILITY_NAMES
        constant = constants.get(value.id)
        if constant is not None and not isinstance(constant, ast.Name):
            return _capabilities_of(constant, constants)
    return None


def _declaring_expressions(klass: ast.ClassDef) -> list[ast.expr]:
    """Every expression that declares this class's ``capabilities``.

    Not just a property: the router reads the attribute with ``getattr``
    (``_router.py``), so a class attribute and an assignment in ``__init__`` declare exactly as
    much. More than one is ambiguous rather than clever, and reads as unreadable below.
    """
    found: list[ast.expr] = []
    for node in ast.walk(klass):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                named = (
                    isinstance(target, ast.Name)
                    and target.id == "capabilities"
                    or isinstance(target, ast.Attribute)
                    and target.attr == "capabilities"
                )
                if named:
                    found.append(node.value)
        elif isinstance(node, ast.AnnAssign) and node.value:
            target = node.target
            if (
                isinstance(target, ast.Name)
                and target.id == "capabilities"
                or isinstance(target, ast.Attribute)
                and target.attr == "capabilities"
            ):
                found.append(node.value)
        elif (
            isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
            and node.name == "capabilities"
        ):
            # The return *expression*, not the body: a `Capability` mentioned in a log line or a
            # docstring example is not a declaration, and reading the whole body lets one hide
            # the other. Several returns is a shape this file will not guess between.
            returns = [
                child.value
                for child in ast.walk(node)
                if isinstance(child, ast.Return) and child.value is not None
            ]
            found.extend(returns if len(returns) == 1 else [ast.Constant(value=None)])
    return found


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


def _backends(root: Path) -> list[tuple[str, str, frozenset[str] | None]]:
    """``(module, class, capabilities)`` for every backend under ``root``, ``None`` if unreadable.

    Backend-shaped means ``acquire`` — the one method the protocol cannot do without — or a
    ``capabilities`` declaration. Both, because **silence is a declaration too**: the property
    is optional and omitting it reads as ``DEFAULT_CAPABILITIES``, so discovering backends by
    their declaration alone would let one leave this guard by deleting six lines.

    A class that declares nothing and inherits from something is not guessed at: the answer may
    be in a base this file does not follow, so it is unreadable.
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
            declarations = _declaring_expressions(klass)
            if not declarations and _method(klass, "acquire") is None:
                continue
            if len(declarations) == 1:
                capabilities = _capabilities_of(declarations[0], constants)
            elif declarations:
                capabilities = None
            elif klass.bases:
                capabilities = None
            else:
                capabilities = DEFAULT_CAPABILITY_NAMES
            found.append(
                (module.relative_to(REPO_ROOT).as_posix(), klass.name, capabilities)
            )
    return found


def _collected_calls(module: Path) -> frozenset[str]:
    """Every function called from inside a test pytest would collect in ``module``.

    Lexical containment in a collected test, not merely presence in the file. A call left
    behind in a helper whose test was renamed away still parses, and a guard that counts it
    goes green while the suite no longer runs — the failure mode of every grep-shaped check.
    """
    called: set[str] = set()

    def collectable_class(klass: ast.ClassDef) -> bool:
        """pytest's rule, both halves: named ``Test*``, and no constructor.

        The second half is not a detail — a `Test*` class that grows an ``__init__`` is warned
        about and **skipped**, so an initializer added for convenience silently stops the suite
        running while every name still reads like a test.
        """
        return klass.name.startswith("Test") and not any(
            isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
            and node.name in {"__init__", "__new__"}
            for node in klass.body
        )

    def visit(node: ast.AST, in_class: bool, inside_test: bool) -> None:
        for child in ast.iter_child_nodes(node):
            collected = inside_test
            if isinstance(child, ast.ClassDef):
                visit(child, in_class and collectable_class(child), inside_test)
                continue
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                # A `test*` function pytest reaches, or anything nested inside one — a helper
                # defined in a collected test runs when that test does.
                collected = inside_test or (child.name.startswith("test") and in_class)
            if isinstance(child, ast.Call) and collected:
                callee = child.func
                name = (
                    callee.id
                    if isinstance(callee, ast.Name)
                    else callee.attr
                    if isinstance(callee, ast.Attribute)
                    else None
                )
                if name:
                    called.add(name)
            visit(child, in_class, collected)

    # `in_class` starts true: a module-level `test*` function has no class to disqualify it.
    visit(ast.parse(module.read_text(encoding="utf-8")), True, False)
    return frozenset(called)


def _calls_the_suite(tests: Path) -> bool:
    return any(
        SUITE in _collected_calls(module)
        for module in tests.rglob("*.py")
        if module.name.startswith("test_") or module.name.endswith("_test.py")
    )


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
        f"{package.name} declares FILES_OUT in {', '.join(serving)} and no test pytest collects "
        f"calls {SUITE}. Two backends written against the prose alone shipped the same "
        "confinement escape (#142); the probes are what that cost bought. Fill in a "
        "`ConformanceSubject` — `PosixGuestSubject` if the guest is Linux and has `ln` — and "
        "await the suite against a real instance, from a `test_*` in a `test_*.py`."
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
        "tell whether the conformance suite is required of it. It reads one declaration — a "
        "`capabilities` property with a single return, a class attribute, or an assignment in "
        "`__init__` — naming `Capability` members, `DEFAULT_CAPABILITIES`, or a module-level "
        "constant holding either; and it reads a backend that declares nothing as the default "
        "set, unless it has a base whose declaration cannot be followed. Teach it the new shape "
        "rather than leaving it to pass on silence."
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
    from maf_sandbox import DEFAULT_CAPABILITIES

    assert {member.name for member in DEFAULT_CAPABILITIES} == DEFAULT_CAPABILITY_NAMES
    assert "FILES_OUT" not in DEFAULT_CAPABILITY_NAMES, (
        "if the default ever grows FILES_OUT, silence stops meaning 'no pull surface' and "
        "every backend that declares nothing needs the suite."
    )


def test_the_package_that_ships_the_suite_answers_it_too():
    """The fake is the specimen every kind's tests run against, so it answers the probes too."""
    assert _calls_the_suite(PACKAGES / CORE / "tests")


def test_no_sample_backend_serves_files_out():
    """A tripwire: `samples/09`'s backend is copyable, and refuses the whole pull surface today."""
    serving = _serving(SAMPLES)
    assert not serving, (
        f"{', '.join(serving)} declares FILES_OUT. Hold it to `maf_sandbox.conformance` the way "
        "the packages are — from `tests/` at the root, where the sample's own tests live — and "
        "then relax this test to require that rather than to forbid the declaration."
    )
