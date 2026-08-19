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

#: The suites a package's tests have to call, and where they come from. FILES_OUT is gated on
#: implementing the pull surface (`_serving` below); the other three every backend owes — a
#: backend declaring none of them is not a backend.
SUITES = (
    "assert_files_out_conformance",
    "assert_files_in_conformance",
    "assert_exec_conformance",
    "assert_files_delete_conformance",
)
#: The measurement entry point that stands in for the FILES_DELETE assert when a backend
#: withholds the capability: same probes, no gate, no verdict — findings rather than promises.
MEASURE = "measure_files_delete_probes"
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


def _suite_keys(tree: ast.Module, suite: str) -> frozenset[str]:
    """Every way this module can name one suite, from its own imports — function-local included.

    Requiring the import is what separates the shared suite from a local function that happens
    to share its name.
    """
    keys: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module == SUITE_MODULE:
                keys |= {alias.asname or alias.name for alias in node.names if alias.name == suite}
            elif node.module == "maf_sandbox":
                keys |= {
                    f"{alias.asname or alias.name}.{suite}"
                    for alias in node.names
                    if alias.name == "conformance"
                }
        elif isinstance(node, ast.Import):
            keys |= {
                f"{alias.asname or alias.name}.{suite}"
                for alias in node.names
                if alias.name == SUITE_MODULE
            }
    return frozenset(keys)


def _calls_the_suite(tests: Path, suite: str) -> bool:
    """Whether a test module under ``tests`` imports one suite and calls it."""
    for module in tests.rglob("test_*.py"):
        tree = ast.parse(module.read_text(encoding="utf-8"))
        keys = _suite_keys(tree, suite)
        if keys and any(
            _callee_key(node.func) in keys for node in ast.walk(tree) if isinstance(node, ast.Call)
        ):
            return True
    return False


def _binding_text(annotation: ast.expr) -> str:
    """The annotation as source text, so `tuple[SandboxBackend, type[Sandbox]]` matches however it is spaced."""
    return ast.unparse(annotation)


def _iter_modules(src: Path, module_filter: str | None = None):
    """Every .py under ``src`` — filtered to one file when ``module_filter`` names it."""
    for module in sorted(src.rglob("*.py")):
        if ".venv" in module.parts:
            continue
        if module_filter is None or module.name == module_filter:
            yield module


def _carries_the_binding(src: Path, backend_class: str | None = None) -> str | None:
    """The file under ``src`` holding the static protocol binding, or ``None``.

    The binding is an ``AnnAssign`` annotated ``tuple[SandboxBackend, type[Sandbox]]`` under an
    ``if TYPE_CHECKING:`` — the annotation is the load-bearing part, and the name (``_``) is
    not, so the name is deliberately not matched. ``isinstance`` assertions do not count:
    ``runtime_checkable`` checks member presence only, and a narrowed signature or a missing
    method passes one while failing the build the binding fails.

    ``backend_class``, when given, additionally requires the assignment's value to construct
    or name that class — so a package with two backends is held to a binding for each, not to
    whichever single one happens to carry the annotation.
    """
    target = "tuple[SandboxBackend, type[Sandbox]]"
    for module in _iter_modules(src):
        tree = ast.parse(module.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not (isinstance(node, ast.If) and isinstance(node.test, ast.Name)):
                continue
            if node.test.id != "TYPE_CHECKING":
                continue
            for statement in node.body:
                if (
                    isinstance(statement, ast.AnnAssign)
                    and statement.annotation is not None
                    and _binding_text(statement.annotation) == target
                    and (backend_class is None or _names_backend(statement.value, backend_class))
                ):
                    return module.relative_to(REPO_ROOT).as_posix()
    return None


def _names_backend(value: ast.expr | None, backend_class: str) -> bool:
    """Whether a binding's value constructs or names ``backend_class``.

    The shipped shapes are a call — ``Backend(Config())`` — or a bare name; anything else is
    not judged, and counts as bound only if the class name appears in it.
    """
    if value is None:
        return False
    return backend_class in _binding_text(value)


def _backends(src: Path, module_filter: str | None = None) -> list[str]:
    """Classes under ``src`` that look like a ``SandboxBackend``: ``acquire`` plus a dispose.

    A structural read rather than an import — this file may not import the packages it audits,
    because a backend that fails to import is exactly the one it must still name. ``acquire``
    taking a ``SandboxKey`` and a ``SandboxSpec`` is the signature no other shape in these
    packages happens to share.
    """
    found: list[str] = []
    for module in _iter_modules(src, module_filter):
        tree = ast.parse(module.read_text(encoding="utf-8"))
        for klass in ast.walk(tree):
            if not isinstance(klass, ast.ClassDef):
                continue
            methods = {
                node.name: node
                for node in klass.body
                if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
            }
            acquire = methods.get("acquire")
            if acquire is None or len(acquire.args.args) < 3:
                continue
            acquire_names = {
                ast.unparse(arg.annotation) for arg in acquire.args.args[1:] if arg.annotation
            }
            if not {"SandboxKey", "SandboxSpec"} <= acquire_names:
                continue
            if any(name in methods for name in ("dispose", "dispose_scope")):
                found.append(klass.name)
    return found


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
#: Discovered structurally rather than listed: `acquire(SandboxKey, SandboxSpec)` plus a
#: dispose. A seventh backend is a backend on the commit that adds it (#450).
SANDBOX_BACKEND_PACKAGES = [path for path in BACKEND_PACKAGES if _backends(path / "src")]


@pytest.mark.parametrize("package", BACKEND_PACKAGES, ids=lambda path: path.name)
def test_a_backend_that_serves_the_pull_surface_answers_the_suite(package: Path):
    serving = _serving(package / "src")
    if not serving:
        pytest.skip(f"{package.name} implements no pull surface")
    where = "; ".join(f"{method} in {', '.join(sorted(set(at)))}" for method, at in serving.items())
    assert _calls_the_suite(package / "tests", SUITES[0]), (
        f"{package.name} implements the pull surface — {where} — and nothing in its "
        f"tests imports {SUITES[0]} from {SUITE_MODULE} and calls it. Two backends written against "
        "the prose alone shipped the same confinement escape (#142); the probes are what that "
        "cost bought. Fill in a `ConformanceSubject` — `PosixGuestSubject` if the guest is Linux "
        "and has `ln` — and await the suite against a real instance."
    )


@pytest.mark.parametrize("package", SANDBOX_BACKEND_PACKAGES, ids=lambda path: path.name)
def test_every_backend_answers_the_suites_it_cannot_opt_out_of(package: Path):
    """FILES_IN, EXEC and FILES_DELETE: the capabilities every sandbox backend declares or refuses.

    FILES_OUT is gated on serving the pull surface; these three are not, because a backend that
    declares none of them is not a backend. FILES_DELETE admits two answers: the assert for a
    backend that declares the capability, and `measure_files_delete_probes` for one that
    withholds it — the measurement is how a withheld capability can ever be evidenced into or
    out of declaration (#450: a gate nothing can run against an undeclared mechanism is a gate
    that never opens). The wiring check proves a call is written either way, which is all it
    can prove; what the call found is each suite's own business.
    """
    for suite in SUITES[1:]:
        assert _calls_the_suite(package / "tests", suite) or (
            suite == SUITES[3] and _calls_the_suite(package / "tests", MEASURE)
        ), (
            f"nothing in {package.name}'s tests imports {suite} from {SUITE_MODULE} and calls "
            "it. A backend declaring none of the capabilities a suite probes is not a backend, "
            "and one that withholds one answers it with an asserted skip or a measurement — "
            "but neither is checkable from a call that was never written. #450 is what this "
            "silence cost."
        )


@pytest.mark.parametrize("package", SANDBOX_BACKEND_PACKAGES, ids=lambda path: path.name)
def test_a_backend_carries_the_static_protocol_binding(package: Path):
    """The `tuple[SandboxBackend, type[Sandbox]]` annotation, present rather than remembered.

    `runtime_checkable` tests member presence, so an `isinstance` assertion passes while a
    signature narrows or a method goes missing — the annotation is what fails the build
    instead. wslc carried it alone on main while docker and acas stayed green and
    non-conforming, which is the near-miss #450 records: the binding is a convention nothing
    enforced until this test.

    Every discovered backend class, not one per package: a package holding two backends is
    bound when each of them is named in a binding, so the second one cannot ride in unbound.
    """
    backends = _backends(package / "src")
    assert backends, f"{package.name} was discovered as a backend package but has no backends"
    for backend_class in backends:
        where = _carries_the_binding(package / "src", backend_class)
        assert where is not None, (
            f"no module under {package.name}/src carries the static conformance binding for "
            f"{backend_class} — an `AnnAssign` of `tuple[SandboxBackend, type[Sandbox]]` under "
            "`if TYPE_CHECKING:` naming that backend and its sandbox. Without it, adding a "
            "method to the protocol leaves this package silently non-conforming with a fully "
            "green build. Paste the binding wslc's `_backend.py` carries, naming this "
            "package's own backend and sandbox classes."
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
    structural = {
        package.name: _backends(package / "src")
        for package in BACKEND_PACKAGES
        if _backends(package / "src")
    }
    assert {"maf-sandbox-acas", "maf-sandbox-docker", "maf-sandbox-wslc"} <= set(structural), (
        f"expected the three backend packages to be discovered structurally; found "
        f"{sorted(structural)}. The binding rule and the no-opt-out rule key off this "
        "discovery, so a miss here makes both vacuous while still green."
    )


def test_the_package_that_ships_the_suite_answers_it_too():
    """The fake is the specimen every kind's tests run against, so it answers the probes too."""
    for suite in SUITES:
        assert _calls_the_suite(PACKAGES / CORE / "tests", suite) or (
            suite == SUITES[3] and _calls_the_suite(PACKAGES / CORE / "tests", MEASURE)
        ), (
            f"nothing in maf-sandbox's own tests calls {suite}. The package that ships the "
            "suite is the specimen every kind's tests run against — removing its call to one "
            "of them leaves that suite unexercised by the only tests that always run."
        )


def test_the_core_package_carries_the_binding_too():
    """`maf_sandbox.testing` implements the protocol; the binding holds it to its own package.

    It is the specimen every kind's tests run against, so a protocol method it stops satisfying
    is a defect every kind's suite reports as a fake problem rather than a core one. Discovery
    is scoped to the module: core also holds `SandboxRouter`, which structurally resembles a
    backend (`acquire` plus a dispose) but is the caller of them, not one.
    """
    src = PACKAGES / CORE / "src"
    backends = _backends(src, module_filter="testing.py")
    assert backends, "core's testing backend is no longer discovered as a backend"
    for backend_class in backends:
        assert _carries_the_binding(src, backend_class) is not None, (
            f"no module under maf-sandbox/src carries the static conformance binding for "
            f"{backend_class}. `maf_sandbox.testing`'s backend and sandbox should hold it, the "
            "same annotation every backend package carries."
        )


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
