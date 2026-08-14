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
import re
import tomllib
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
    value: ast.expr, constants: dict[str, ast.expr], seen: frozenset[str] = frozenset()
) -> frozenset[str] | None:
    """What one declaring expression says, or ``None`` when this file cannot tell.

    A grammar, matched **whole**, rather than a search for members inside it. Four shapes: a
    literal collection of ``Capability`` members, ``frozenset(...)`` or ``set(...)`` around
    one, a name — ``DEFAULT_CAPABILITIES`` or a module constant — and a union of those.

    Whole matters more than the shapes do. Reading the members out of an expression and
    ignoring the rest accepts ``frozenset({Capability.EXEC}) | _decided_at_runtime()`` as
    ``{EXEC}``, and a runtime half that adds ``FILES_OUT`` then declares a pull surface this
    file has already filed as absent. Anything the grammar does not cover answers ``None``.
    """
    if isinstance(value, ast.Set | ast.List | ast.Tuple):
        members = frozenset(
            element.attr
            for element in value.elts
            if isinstance(element, ast.Attribute)
            and isinstance(element.value, ast.Name)
            and element.value.id == "Capability"
        )
        return members if len(members) == len(value.elts) else None
    if isinstance(value, ast.Call):
        callee = value.func
        wrapper = isinstance(callee, ast.Name) and callee.id in {"frozenset", "set"}
        if not wrapper or value.keywords or len(value.args) > 1:
            return None
        return frozenset() if not value.args else _capabilities_of(value.args[0], constants, seen)
    if isinstance(value, ast.BinOp) and isinstance(value.op, ast.BitOr):
        left = _capabilities_of(value.left, constants, seen)
        right = _capabilities_of(value.right, constants, seen)
        return None if left is None or right is None else left | right
    if isinstance(value, ast.Name):
        if value.id == "DEFAULT_CAPABILITIES":
            return DEFAULT_CAPABILITY_NAMES
        constant = constants.get(value.id)
        if constant is not None and value.id not in seen:
            return _capabilities_of(constant, constants, seen | {value.id})
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
            isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == "capabilities"
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


def _method(klass: ast.ClassDef, name: str) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    for node in klass.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == name:
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
            found.append((module.relative_to(REPO_ROOT).as_posix(), klass.name, capabilities))
    return found


def _defines_a_constructor(
    klass: ast.ClassDef,
    by_name: dict[str, ast.ClassDef],
    seen: frozenset[str] = frozenset(),
) -> bool:
    """Whether pytest would see a constructor on ``klass`` — **inherited ones included**.

    pytest resolves the attribute rather than reading the class body, so a `Test*` class
    inheriting ``__init__`` from a base is warned about and skipped exactly like one defining
    it. A base this file cannot resolve in the same module answers *yes*: refusing to count a
    class whose bases are unknown loses coverage of that class, and guessing loses the guard.
    """
    if any(
        isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        and node.name in {"__init__", "__new__"}
        for node in klass.body
    ):
        return True
    for base in klass.bases:
        name = (
            base.id
            if isinstance(base, ast.Name)
            else base.attr
            if isinstance(base, ast.Attribute)
            else None
        )
        if name == "object":
            continue
        if name is None or name in seen:
            return True
        resolved = by_name.get(name)
        if resolved is None or _defines_a_constructor(resolved, by_name, seen | {name}):
            return True
    return False


def _opts_out(
    klass: ast.ClassDef, by_name: dict[str, ast.ClassDef], seen: frozenset[str] = frozenset()
) -> bool:
    """Whether ``__test__`` says *do not collect me* — resolved through bases, like pytest does.

    One line inside a class turns every test in it off while leaving all their names intact.
    """
    for node in klass.body:
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign):
            targets, value = [node.target], node.value
        else:
            continue
        for target in targets:
            if isinstance(target, ast.Name) and target.id == "__test__":
                # Anything this file cannot read as a truthy literal reads as an opt-out, which
                # loses the class rather than counting a test pytest may never run.
                return not (isinstance(value, ast.Constant) and bool(value.value))
    for base in klass.bases:
        name = base.id if isinstance(base, ast.Name) else None
        if (
            name
            and name in by_name
            and name not in seen
            and _opts_out(by_name[name], by_name, seen | {name})
        ):
            return True
    return False


def _collectable_class(klass: ast.ClassDef, by_name: dict[str, ast.ClassDef]) -> bool:
    """pytest's rule: named ``Test*``, no constructor it can see, and no ``__test__`` opt-out."""
    return (
        klass.name.startswith("Test")
        and not _defines_a_constructor(klass, by_name)
        and not _opts_out(klass, by_name)
    )


def _expects_a_raise(node: ast.AST) -> bool:
    """Whether ``node`` is a ``pytest.raises`` context or call.

    What happens under one did not finish. A suite invocation there is a *rejection* test —
    the refusal of a bad subject is exactly such a test, added on this branch — and counting
    it would let a package satisfy this guard while never running the probes at all.
    """
    calls = (
        [item.context_expr for item in node.items]
        if isinstance(node, ast.With | ast.AsyncWith)
        else [node]
        if isinstance(node, ast.Call)
        else []
    )
    return any(
        isinstance(call, ast.Call)
        and (
            (isinstance(call.func, ast.Attribute) and call.func.attr == "raises")
            # `from pytest import raises` is the same test spelled without the module, and
            # anything else named `raises` that this over-matches is a call excluded, which is
            # the safe direction to be wrong in.
            or (isinstance(call.func, ast.Name) and call.func.id == "raises")
        )
        for call in calls
    )


def _callee_key(callee: ast.expr) -> str | None:
    """How a call names what it calls: ``helper``, ``module.attr``, ``a.b.c`` — or nothing.

    A dotted callee keeps its whole path rather than its last segment, so
    ``self.assert_files_out_conformance()`` and the real import are different keys. Matching on
    the last segment is matching a spelling, and a spelling is not a binding.
    """
    parts: list[str] = []
    node: ast.expr = callee
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    parts.append(node.id)
    return ".".join(reversed(parts))


def _suite_keys(tree: ast.Module) -> frozenset[str]:
    """Every way *this module* can name the shared suite, from its own imports.

    Imports anywhere, function-local included: the acas suite imports inside the test that uses
    it. A module that never imports the suite has no way to call it, whatever it names its own
    functions — which is the point, since a local ``def assert_files_out_conformance`` was
    otherwise indistinguishable from the real one.
    """
    keys: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module == "maf_sandbox.conformance":
                keys |= {alias.asname or alias.name for alias in node.names if alias.name == SUITE}
            elif node.module == "maf_sandbox":
                keys |= {
                    f"{alias.asname or alias.name}.{SUITE}"
                    for alias in node.names
                    if alias.name == "conformance"
                }
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "maf_sandbox.conformance":
                    keys.add(f"{alias.asname or alias.name}.{SUITE}")
    shadowed = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
    }
    return frozenset(key for key in keys if key.split(".")[0] not in shadowed)


def _names_used(node: ast.AST) -> frozenset[str]:
    """Every name **called** in ``node``'s own body — not inside a nested def, not merely named.

    Three exclusions, each because the thing excluded did not run.

    A nested definition is only that: defining ``scenario`` does not run it. Its body is
    reached through :func:`_reachable_calls` if something executes it, and not otherwise.

    Anything under a ``pytest.raises`` did not finish, so it is a rejection test rather than an
    invocation.

    A bare *reference* is not a call: ``assert callable(assert_files_out_conformance)`` names
    the suite and runs none of it. Nothing is lost by insisting on the call — ``asyncio.run(
    scenario())`` invokes ``scenario`` and reads as a call already — and handing a helper to
    something that may or may not invoke it goes uncounted, which fails this guard rather than
    passing it.
    """
    used: set[str] = set()
    for child in ast.iter_child_nodes(node):
        if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef | ast.Lambda):
            continue
        if _expects_a_raise(child):
            continue
        if isinstance(child, ast.Call):
            key = _callee_key(child.func)
            if key:
                used.add(key)
        used |= _names_used(child)
    return frozenset(used)


def _reachable_calls(test: ast.FunctionDef | ast.AsyncFunctionDef) -> frozenset[str]:
    """What running ``test`` reaches: its own body, plus local helpers it actually executes."""
    helpers = {
        node.name: node
        for node in ast.walk(test)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node is not test
    }
    reached = set(_names_used(test))
    expanded: set[str] = set()
    while pending := {name for name in reached if name in helpers} - expanded:
        for name in pending:
            expanded.add(name)
            reached |= _names_used(helpers[name])
    return frozenset(reached)


def _collected_calls(tree: ast.Module) -> frozenset[str]:
    """Every callee a test pytest collects in this module reaches when it runs.

    Three conditions, and the guard is only as good as the weakest. **Collected**: a `test*`
    function, with `Test*`, no constructor and no ``__test__`` opt-out — both resolved through
    bases — for every class around it. **Reached**: called from the test's own body, or from a
    local helper the body executes, because a call in a nested function nobody invokes never
    runs. **Completed**: not under a ``pytest.raises``, which is where a call goes when it is
    written to fail.
    """
    by_name = {node.name: node for node in ast.walk(tree) if isinstance(node, ast.ClassDef)}
    called: set[str] = set()

    def visit(node: ast.AST, in_class: bool) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                visit(child, in_class and _collectable_class(child, by_name))
            elif isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                if child.name.startswith("test") and in_class:
                    called.update(_reachable_calls(child))
            else:
                visit(child, in_class)

    # `in_class` starts true: a module-level `test*` function has no class to disqualify it.
    visit(tree, True)
    return frozenset(called)


def _calls_the_suite(tests: Path) -> bool:
    """Whether a test pytest collects under ``tests`` calls the suite it imported from core."""
    for module in tests.rglob("*.py"):
        if not (module.name.startswith("test_") or module.name.endswith("_test.py")):
            continue
        tree = ast.parse(module.read_text(encoding="utf-8"))
        if _collected_calls(tree) & _suite_keys(tree):
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


def _metadata(package: Path) -> dict:
    return tomllib.loads((package / "pyproject.toml").read_text(encoding="utf-8"))["project"]


def _requires(package: Path) -> frozenset[str]:
    """The distributions this package depends on, by name."""
    return frozenset(
        match.group(0)
        for requirement in _metadata(package).get("dependencies", [])
        if (match := re.match(r"[A-Za-z0-9._-]+", requirement))
    )


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
    serving = {package.name for package in BACKEND_PACKAGES if _serving(package / "src")}
    assert {"maf-sandbox-acas", "maf-sandbox-docker"} <= serving, (
        f"expected the two backends that serve FILES_OUT to be found; found {sorted(serving)}. "
        "Either one of them stopped declaring the capability — in which case say so here — or "
        "the discovery stopped seeing it, which would make every other test in this file "
        "vacuous while still green."
    )


def test_no_package_builds_on_a_sibling_that_serves_files_out():
    """A tripwire for the one case discovery cannot read: a backend inheriting its whole surface.

    A class that inherits both ``acquire`` and its declaration from a base in *another*
    distribution is not backend-shaped to anything here — the subclass declares nothing and the
    base is not in this tree — so the package could serve ``FILES_OUT`` and never be asked for
    the suite. Reading it properly would mean resolving imports across distributions, which is
    an import away from being a type checker.

    What is checkable is the dependency that such inheritance requires: no package here depends
    on a sibling that serves ``FILES_OUT``, and the day one does, this says so. A base imported
    from *outside* the workspace stays beyond the guard, and the probes themselves are what
    catch that one.
    """
    by_distribution = {_metadata(package)["name"]: package for package in PACKAGE_DIRS}
    building_on = {
        package.name: sorted(
            name
            for name in _requires(package)
            if name in by_distribution and _serving(by_distribution[name] / "src")
        )
        for package in BACKEND_PACKAGES
    }
    offenders = {name: on for name, on in building_on.items() if on}
    assert not offenders, (
        f"{offenders} depends on a sibling that serves FILES_OUT, so it may inherit that "
        "surface without declaring it — the one shape backend discovery here cannot see. Hold "
        "it to the suite explicitly, and relax this test to require that rather than to forbid "
        "the dependency."
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
    """A tripwire: `samples/09`'s backend is copyable, and refuses the whole pull surface today.

    Readable first. `_serving` drops what it cannot parse, so asking only *does a sample declare
    FILES_OUT* lets an unreadable declaration answer no — the fail-open the package tests refuse
    and this one would otherwise allow.
    """
    unreadable = [
        f"{klass} ({module})"
        for module, klass, capabilities in _backends(SAMPLES)
        if capabilities is None
    ]
    assert not unreadable, (
        f"this guard cannot tell what {', '.join(unreadable)} declares, so it cannot tell "
        "whether a sample has grown a pull surface. Same rule as the packages: teach the "
        "grammar the new shape rather than leaving it to pass on silence."
    )
    serving = _serving(SAMPLES)
    assert not serving, (
        f"{', '.join(serving)} declares FILES_OUT. Hold it to `maf_sandbox.conformance` the way "
        "the packages are — from `tests/` at the root, where the sample's own tests live — and "
        "then relax this test to require that rather than to forbid the declaration."
    )
