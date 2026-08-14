"""A package that implements the pull surface must call `maf_sandbox.conformance`'s suite.

Two traps, both worth knowing before trusting this.

**It is a wiring check.** It proves a serving package *writes* a call to the suite it imported,
not that the call ran — skipping, disabling or shadowing that test defeats it, and review
rather than this file is where that gets caught (#142).

**Implementing the surface is the trigger, not declaring it.** A declaration has a dozen
spellings; a method with a body is a fact, and nothing serves ``FILES_OUT`` without one.
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

#: The function a package's tests have to call, and where it comes from.
SUITE = "assert_files_out_conformance"
SUITE_MODULE = "maf_sandbox.conformance"

#: What serving `FILES_OUT` takes. `list_dir` belongs to `FILES_LIST` and is not required here —
#: `maf-sandbox-docker` implements the first two and refuses the third, which is the capability
#: split doing its job rather than a gap.
PULL_SURFACE = ("stat_file", "read_file")

#: The package that ships the suite. Its backend is the in-process fake, exempt from the rule
#: below only because `test_the_package_that_ships_the_suite_answers_it_too` holds it to the
#: same probes by name.
CORE = "maf-sandbox"


def _implements(method: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """Whether this method does something, as opposed to declaring or refusing.

    Two bodies mean *not served*: a lone ``raise``, which is how `samples/09` writes all three,
    and nothing at all — a ``...`` is a Protocol saying the method exists, and `maf_sandbox`'s
    own ``Sandbox`` is written that way. Anything else counts, a ``raise`` with a line of
    logging above it included: over-counting asks for a suite run that may not be needed, and
    under-counting lets a real surface past.
    """
    body = [
        statement
        for statement in method.body
        if not (isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Constant))
    ]
    if not body:
        return False
    return not (len(body) == 1 and isinstance(body[0], ast.Raise))


def _serving(root: Path) -> dict[str, list[str]]:
    """Where each pull-surface method is implemented under ``root`` — empty unless all of them are.

    Aggregated over the whole tree rather than per class. A base or a mixin holding
    ``stat_file`` while the subclass holds ``read_file`` serves the surface between them, and
    moving one method into a base is an ordinary refactor that must not be able to switch this
    off. The cost is over-counting — two unrelated classes with one method each read as serving
    — which asks for a suite run that may not be needed, and that is the direction to be wrong
    in.
    """
    found: dict[str, list[str]] = {}
    for module in sorted(root.rglob("*.py")):
        if ".venv" in module.parts:
            continue
        tree = ast.parse(module.read_text(encoding="utf-8"))
        for klass in ast.walk(tree):
            if not isinstance(klass, ast.ClassDef):
                continue
            for node in klass.body:
                if (
                    isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
                    and node.name in PULL_SURFACE
                    and _implements(node)
                ):
                    where = f"{klass.name} ({module.relative_to(REPO_ROOT).as_posix()})"
                    found.setdefault(node.name, []).append(where)
    return found if set(found) >= set(PULL_SURFACE) else {}


def _callee_key(callee: ast.expr) -> str | None:
    """How a call names what it calls: ``suite``, ``conformance.suite``, ``a.b.c``."""
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
    """Every way this module can name the suite, from its own imports — function-local included.

    Requiring the import is what separates the shared suite from a local function that happens
    to share its name.
    """
    keys: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module == SUITE_MODULE:
                keys |= {alias.asname or alias.name for alias in node.names if alias.name == SUITE}
            elif node.module == "maf_sandbox":
                keys |= {
                    f"{alias.asname or alias.name}.{SUITE}"
                    for alias in node.names
                    if alias.name == "conformance"
                }
        elif isinstance(node, ast.Import):
            keys |= {
                f"{alias.asname or alias.name}.{SUITE}"
                for alias in node.names
                if alias.name == SUITE_MODULE
            }
    return frozenset(keys)


def _calls_the_suite(tests: Path) -> bool:
    """Whether a test module under ``tests`` imports the suite and calls it."""
    for module in tests.rglob("test_*.py"):
        tree = ast.parse(module.read_text(encoding="utf-8"))
        keys = _suite_keys(tree)
        if keys and any(
            _callee_key(node.func) in keys for node in ast.walk(tree) if isinstance(node, ast.Call)
        ):
            return True
    return False


def _canonical(distribution: str) -> str:
    """PEP 503 normalisation: ``maf_sandbox_docker`` and ``maf-sandbox-docker`` are one name."""
    return re.sub(r"[-_.]+", "-", distribution).lower()


def _metadata(package: Path) -> dict:
    return tomllib.loads((package / "pyproject.toml").read_text(encoding="utf-8"))["project"]


def _requires(package: Path) -> frozenset[str]:
    """The distributions this package depends on, canonicalised."""
    return frozenset(
        _canonical(match.group(0))
        for requirement in _metadata(package).get("dependencies", [])
        if (match := re.match(r"[A-Za-z0-9._-]+", requirement))
    )


PACKAGE_DIRS = sorted(path.parent for path in PACKAGES.glob("*/pyproject.toml"))
BACKEND_PACKAGES = [path for path in PACKAGE_DIRS if path.name != CORE]


@pytest.mark.parametrize("package", BACKEND_PACKAGES, ids=lambda path: path.name)
def test_a_backend_that_serves_the_pull_surface_answers_the_suite(package: Path):
    serving = _serving(package / "src")
    if not serving:
        pytest.skip(f"{package.name} implements no pull surface")
    where = "; ".join(f"{method} in {', '.join(sorted(set(at)))}" for method, at in serving.items())
    assert _calls_the_suite(package / "tests"), (
        f"{package.name} implements the pull surface — {where} — and nothing in its "
        f"tests imports {SUITE} from {SUITE_MODULE} and calls it. Two backends written against "
        "the prose alone shipped the same confinement escape (#142); the probes are what that "
        "cost bought. Fill in a `ConformanceSubject` — `PosixGuestSubject` if the guest is Linux "
        "and has `ln` — and await the suite against a real instance."
    )


def test_the_guard_still_finds_the_backends_that_exist():
    """A rule matching nothing passes every time; this is what stops that going unnoticed."""
    serving = {package.name for package in BACKEND_PACKAGES if _serving(package / "src")}
    assert {"maf-sandbox-acas", "maf-sandbox-docker"} <= serving, (
        f"expected the two backends that serve FILES_OUT to be found; found {sorted(serving)}. "
        "Either one stopped implementing the pull surface — in which case say so here — or the "
        "discovery stopped seeing it, which would make every other test in this file vacuous "
        "while still green."
    )


def test_the_package_that_ships_the_suite_answers_it_too():
    """The fake is the specimen every kind's tests run against, so it answers the probes too."""
    assert _calls_the_suite(PACKAGES / CORE / "tests")


def test_no_package_builds_on_a_sibling_that_serves_the_pull_surface():
    """A tripwire for the one case reading source cannot decide: a backend inheriting its surface.

    A subclass whose ``stat_file`` and ``read_file`` live on a base in another distribution
    implements nothing locally, so nothing here sees it. Resolving that means resolving imports
    across distributions. What is checkable is the dependency such inheritance requires, and no
    package here has one. **A base from outside this workspace stays uncovered** — not by the
    probes either, since nothing would ask for them. Review is the backstop, and saying so is
    better than implying otherwise.
    """
    # Core is excluded: every package depends on it, and the surface it implements is
    # `maf_sandbox.testing`'s in-process fake — a test double nothing inherits a backend from.
    by_distribution = {
        _canonical(_metadata(package)["name"]): package
        for package in PACKAGE_DIRS
        if package.name != CORE
    }
    offenders = {
        package.name: sorted(
            name
            for name in _requires(package)
            if name in by_distribution and _serving(by_distribution[name] / "src")
        )
        for package in BACKEND_PACKAGES
    }
    building_on = {name: on for name, on in offenders.items() if on}
    assert not building_on, (
        f"{building_on} depends on a sibling that serves the pull surface, so it may inherit "
        "that surface without implementing it — the one shape this file cannot see. Hold it to "
        "the suite explicitly, and relax this test to require that rather than to forbid the "
        "dependency."
    )


def test_no_sample_implements_the_pull_surface():
    """A tripwire: `samples/09`'s backend is copyable, and refuses all three methods today."""
    serving = _serving(SAMPLES)
    where = "; ".join(f"{method} in {', '.join(sorted(set(at)))}" for method, at in serving.items())
    assert not serving, (
        f"a sample implements the pull surface — {where}. Hold it to `maf_sandbox.conformance` "
        "the way the packages are — from `tests/` at the root, where the sample's own tests "
        "live — and then relax this test to require that rather than to forbid the surface."
    )
