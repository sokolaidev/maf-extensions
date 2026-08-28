"""The stat a backend hands the confinement check must not be answered inside its own guest.

`maf_sandbox.paths.refuse_symlinked_parents` runs over a stat the backend supplies, and that
stat has three requirements. Two are properties of the call — unconfined, no-follow — and a
wrong one shows up as a refusal that does not happen. The third is a property of *who answers*,
and it shows up as nothing at all: a guest that replaces `test` in its own image answers every
probe truthfully until the run it matters on.

Two traps, both worth knowing before trusting this.

**It is a wiring check.** It proves what the source writes, not what ran. A backend reaching
its guest through a name this does not model passes, and only a live probe against a guest with
no `test` would catch that — recorded as the upgrade on #729, not shipped here.

**A listing is a declaration, not a mute button.** A package in `ANSWERED_INSIDE_THE_GUEST`
must also say so in its own README, and an entry that stops being true fails as loudly as a
missing one, so the list cannot outlive the backend it describes.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PACKAGES = REPO_ROOT / "packages"

#: The two entry points that take a backend's stat as their first argument. Both are the same
#: duty: `confine_guest_write_path` is `refuse_symlinked_parents` plus the write-path refusals.
ENTRY_POINTS = ("refuse_symlinked_parents", "confine_guest_write_path")

#: Where those entry points live, absolute and relative — `maf_sandbox`'s own modules import
#: them as `.paths`.
ENTRY_MODULES = frozenset({"maf_sandbox.paths", "paths"})

#: How a backend spawns a command in its guest. `exec` and `_exec` are the method names; the
#: bare string is the CLI subcommand both process backends pass positionally — `docker exec`
#: and `wslc container exec`. Matched as an exact string so `create_subprocess_exec`, which is
#: how a *host* process is started, is not read as reaching the guest.
GUEST_COMMAND_NAMES = frozenset({"exec", "_exec"})
GUEST_COMMAND_ARGUMENT = "exec"

#: Packages whose stat is answered inside the guest, and the issue that decides what to do
#: about it. Being here is a declared posture: the README requirement below is what it costs.
ANSWERED_INSIDE_THE_GUEST = {
    "maf-sandbox-wslc": "#495 — the tar header answers no regular file and no link, so the "
    "entry type comes from `test` run in the container being confined",
}

#: What a README must contain for an entry above to be a declaration a reader can find.
DECLARED_IN_README = "inside the guest"

#: Packages that must contribute at least one call site. A scanner that silently stopped
#: matching would otherwise pass by finding nothing, which is the way this kind of check fails.
MUST_BE_SCANNED = frozenset(
    {"maf-sandbox", "maf-sandbox-acas", "maf-sandbox-docker", "maf-sandbox-wslc"}
)


def _callee_name(callee: ast.expr) -> str | None:
    """The last name in a call's callee: ``f``, ``self._f`` and ``a.b._f`` all give ``_f``."""
    if isinstance(callee, ast.Attribute):
        return callee.attr
    if isinstance(callee, ast.Name):
        return callee.id
    return None


def _entry_names(tree: ast.Module) -> frozenset[str]:
    """Every name this module can call an entry point by, from its own imports.

    Requiring the import is what keeps a local function that happens to share the name from
    being read as the shared one.
    """
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module in ENTRY_MODULES:
            names |= {
                alias.asname or alias.name for alias in node.names if alias.name in ENTRY_POINTS
            }
    return frozenset(names)


def _stat_method(argument: ast.expr) -> str | None:
    """The method name a call site hands over as its stat, or ``None`` when it cannot be read.

    Two shapes are resolved: ``self._stat_guest`` passed directly, and a lambda wrapping a call
    to one, which is how a backend adapts its own signature. Anything else is reported rather
    than skipped — a stat this cannot follow is one it cannot clear.
    """
    if isinstance(argument, ast.Attribute):
        return argument.attr
    if isinstance(argument, ast.Lambda):
        for node in ast.walk(argument.body):
            if isinstance(node, ast.Call):
                return _callee_name(node.func)
    return None


def _reaches_the_guest(start: str, methods: dict[str, ast.AST]) -> str | None:
    """How ``start`` reaches a command in the guest, or ``None`` when it does not.

    Breadth-first over calls on ``self`` within the same class, which is where a backend's own
    helpers live. It stops at anything else on purpose: a runner injected at construction —
    both process backends assign ``self._run`` — is not a method here, so the search ends at
    the call rather than descending into how a *host* subprocess is started.
    """
    seen: set[str] = set()
    queue = [start]
    while queue:
        name = queue.pop()
        if name in seen or name not in methods:
            continue
        seen.add(name)
        for node in ast.walk(methods[name]):
            if not isinstance(node, ast.Call):
                continue
            callee = _callee_name(node.func)
            if callee in GUEST_COMMAND_NAMES:
                return f"{name} calls {callee}"
            if any(
                isinstance(argument, ast.Constant) and argument.value == GUEST_COMMAND_ARGUMENT
                for argument in node.args
            ):
                return f'{name} passes "{GUEST_COMMAND_ARGUMENT}"'
            if isinstance(node.func, ast.Attribute) and _is_self(node.func.value) and callee:
                queue.append(callee)
    return None


def _is_self(node: ast.expr) -> bool:
    return isinstance(node, ast.Name) and node.id == "self"


def _entry_calls(node: ast.AST, names: frozenset[str]) -> list[ast.Call]:
    """Every call to an entry point under ``node``, by any of the names it is imported as."""
    return [
        found
        for found in ast.walk(node)
        if isinstance(found, ast.Call) and _callee_name(found.func) in names and found.args
    ]


class Sites:
    """What one scan of the tree found.

    ``unmodelled`` is the honest half: a site whose stat this cannot follow to a method — one
    passed as a bare name, built by a helper, or made outside any class. Reported rather than
    skipped, because a site cleared by never having been read is the failure this check exists
    to prevent.
    """

    def __init__(self) -> None:
        self.reaching: dict[str, list[str]] = {}
        self.scanned: set[str] = set()
        self.unmodelled: list[str] = []


def _sites() -> Sites:
    """Scan every package's source for call sites of the confinement check."""
    found = Sites()
    for module in sorted(PACKAGES.glob("*/src/**/*.py")):
        if ".venv" in module.parts:
            continue
        tree = ast.parse(module.read_text(encoding="utf-8"))
        names = _entry_names(tree)
        if not names:
            continue
        package = module.relative_to(PACKAGES).parts[0]
        where = module.relative_to(REPO_ROOT).as_posix()
        read: set[int] = set()
        for klass in (n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)):
            methods: dict[str, ast.AST] = {
                node.name: node
                for node in klass.body
                if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
            }
            for call in _entry_calls(klass, names):
                found.scanned.add(package)
                stat = _stat_method(call.args[0])
                if stat is None:
                    continue
                read.add(id(call))
                reached = _reaches_the_guest(stat, methods)
                if reached is not None:
                    found.reaching.setdefault(package, []).append(
                        f"{where}: {klass.name}.{reached}"
                    )
        found.unmodelled += [
            f"{where}:{call.lineno}" for call in _entry_calls(tree, names) if id(call) not in read
        ]
    return found


@pytest.fixture(scope="module")
def sites() -> Sites:
    return _sites()


def test_every_package_with_a_confinement_check_is_scanned(sites: Sites) -> None:
    assert MUST_BE_SCANNED <= sites.scanned, (
        f"found no confinement call site in {sorted(MUST_BE_SCANNED - sites.scanned)}. Either "
        "the package stopped confining, or this check stopped matching how it does — the second "
        "would make every assertion below pass by reaching nothing."
    )


def test_every_call_site_is_one_this_check_can_read(sites: Sites) -> None:
    assert not sites.unmodelled, (
        f"these call sites hand over a stat this cannot follow to a method: {sites.unmodelled}. "
        "Pass `self._stat_…` or a lambda calling it, or teach `_stat_method` the new shape — "
        "a site nothing reads is a site nothing clears."
    )


def test_no_undeclared_backend_answers_the_check_inside_its_guest(sites: Sites) -> None:
    undeclared = {
        package: how
        for package, how in sites.reaching.items()
        if package not in ANSWERED_INSIDE_THE_GUEST
    }
    assert not undeclared, (
        "the stat these packages hand the confinement check reaches a command in their own "
        f"guest, which the guest can replace: {undeclared}. Answer it out of the engine, or "
        "declare it in ANSWERED_INSIDE_THE_GUEST and say so in the package's README."
    )


def test_a_declaration_that_stopped_being_true_is_removed(sites: Sites) -> None:
    stale = sorted(set(ANSWERED_INSIDE_THE_GUEST) - set(sites.reaching))
    assert not stale, (
        f"{stale} no longer answers the check inside its guest. Remove the entry: a declaration "
        "kept past its reason reads as a residual that is still there."
    )


@pytest.mark.parametrize("package", sorted(ANSWERED_INSIDE_THE_GUEST))
def test_a_declared_backend_says_so_in_its_readme(package: str) -> None:
    readme = PACKAGES / package / "README.md"
    assert DECLARED_IN_README in readme.read_text(encoding="utf-8"), (
        f"{package} answers the confinement check inside its guest and its README does not say "
        f"so. A reader choosing this backend cannot find the residual anywhere else; the phrase "
        f"this looks for is {DECLARED_IN_README!r}."
    )
