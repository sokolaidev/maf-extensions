"""The stat a backend hands the confinement check must not be answered inside its own guest.

`maf_sandbox.paths.refuse_symlinked_ancestors` runs over a stat the backend supplies, and that
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
missing one, so the list cannot outlive the backend it describes. Reaching for core's own
`stat_by_asking_the_guest` is read exactly like reaching the guest by hand: core spells the
probe so nobody invents a fourth one, and the posture that spelling carries is the same either
way. Both ways of reaching it count — the direct import, and the module form the repository's
own reach-by-name rule asks for.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import NamedTuple

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PACKAGES = REPO_ROOT / "packages"

#: The entry points that take a backend's stat as their first argument. All the same duty: each
#: `confine_resolve_guest_*_path` bundle is `refuse_symlinked_ancestors` plus what its method owes.
ENTRY_POINTS = (
    "refuse_symlinked_ancestors",
    "confine_resolve_guest_write_path",
    "confine_resolve_guest_read_path",
    "confine_resolve_guest_list_path",
    "confine_resolve_guest_delete_path",
    # Both spellings are exported, so a caller on either is matched.
    "refuse_symlinked_parents",
    "confine_guest_write_path",
)

#: How `maf_sandbox.paths` is spelled in an import. Absolute it is the dotted name; relative it
#: is the bare leaf, and *that* spelling names core only inside `maf-sandbox` — anywhere else it
#: names the importing package's own module. `maf_sandbox.paths` is also reach-by-name, so
#: `from maf_sandbox import paths` and `import maf_sandbox.paths` are ordinary spellings rather
#: than exotic ones, and a scanner reading only `from … import <name>` would clear a backend for
#: writing `paths.stat_by_asking_the_guest(…)`.
ENTRY_PACKAGE = "maf_sandbox"
ENTRY_MODULE_LEAF = "paths"
ENTRY_MODULE_DOTTED = "maf_sandbox.paths"

#: The distribution `maf_sandbox` lives in. A relative `from . import paths` names core only
#: inside it — anywhere else it names that package's own module, which is a different thing.
CORE_PACKAGE_DIR = "maf-sandbox"

#: The guest-side stat core ships for a backend whose engine answers nothing. Reaching for one
#: is what this reads — there is no argv to follow, and none is needed: the name is the
#: declaration. Both spellings count, since the module is reach-by-name.
GUEST_STAT_HELPERS = frozenset({"stat_by_asking_the_guest", "stat_by_asking_the_guest_as_root"})

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


def _names_entry_module(node: ast.ImportFrom, *, inside_core: bool) -> bool:
    """Whether a ``from … import`` names `maf_sandbox.paths` and not a package's own module.

    The relative form is the trap: `from .paths import …` parses with ``module == "paths"`` and
    ``level == 1``, which is core inside `maf-sandbox` and that backend's own helper anywhere
    else. Absolute, only the dotted name is core — nothing reaches core's submodule as a bare
    ``paths``.
    """
    if node.level:
        return inside_core and node.module == ENTRY_MODULE_LEAF
    return node.module == ENTRY_MODULE_DOTTED


def _entry_names(tree: ast.Module, *, inside_core: bool) -> frozenset[str]:
    """Every name this module can call an entry point by, from its own imports.

    Requiring the import is what keeps a local function that happens to share the name from
    being read as the shared one.
    """
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and _names_entry_module(node, inside_core=inside_core):
            names |= {
                alias.asname or alias.name for alias in node.names if alias.name in ENTRY_POINTS
            }
    return frozenset(names)


class Aliases(NamedTuple):
    """The names a module can reach `maf_sandbox.paths` by, and there are two kinds.

    ``module`` is bound straight to it — `from maf_sandbox import paths`, `from . import paths`,
    `import maf_sandbox.paths as p`. ``package`` is bound to `maf_sandbox`, which reaches it one
    attribute further along: plain `import maf_sandbox.paths` binds the package, and
    `import maf_sandbox as sandbox` renames it. Keeping the two apart is what makes
    `sandbox.paths.stat_by_asking_the_guest` readable, since the head of that attribute chain is
    a name nothing else here would recognise.
    """

    module: frozenset[str]
    package: frozenset[str]


def _aliases(tree: ast.Module, *, inside_core: bool) -> Aliases:
    """Every name this module reaches `maf_sandbox.paths` by, split by what it is bound to.

    ``inside_core`` is what makes the relative form safe to accept. `from . import paths` names
    core only within `maf_sandbox` itself; in a backend it names that package's own module, and
    reading it as core would fail the repository over a call on something unrelated.
    """
    module: set[str] = set()
    package: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (
            node.module == ENTRY_PACKAGE or (inside_core and node.module is None and node.level)
        ):
            module |= {
                alias.asname or alias.name
                for alias in node.names
                if alias.name == ENTRY_MODULE_LEAF
            }
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == ENTRY_MODULE_DOTTED:
                    # With ``as`` the submodule is bound; without it, the package is.
                    (module.add(alias.asname) if alias.asname else package.add(ENTRY_PACKAGE))
                elif alias.name == ENTRY_PACKAGE:
                    package.add(alias.asname or alias.name)
    return Aliases(frozenset(module), frozenset(package))


def _is_entry_module(node: ast.expr, aliases: Aliases) -> bool:
    """Whether ``node`` names `maf_sandbox.paths`, by either kind of alias."""
    if isinstance(node, ast.Name):
        return node.id in aliases.module
    return (
        isinstance(node, ast.Attribute)
        and node.attr == ENTRY_MODULE_LEAF
        and isinstance(node.value, ast.Name)
        and node.value.id in aliases.package
    )


def _guest_stat_uses(tree: ast.Module, aliases: Aliases, *, inside_core: bool) -> tuple[str, ...]:
    """The guest-side stats core ships that this module reaches, by their canonical names.

    An import is one way and an attribute access through the module is the other. The module
    import alone is *not* read as a use — `from maf_sandbox import paths` is how half this
    repository reaches the confinement helpers, and reading it as a declaration would flag
    every one of them.
    """
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and _names_entry_module(node, inside_core=inside_core):
            found |= {alias.name for alias in node.names if alias.name in GUEST_STAT_HELPERS}
        elif (
            isinstance(node, ast.Attribute)
            and node.attr in GUEST_STAT_HELPERS
            and _is_entry_module(node.value, aliases)
        ):
            found.add(node.attr)
    return tuple(sorted(found))


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


def _entry_calls(node: ast.AST, names: frozenset[str], aliases: Aliases) -> list[ast.Call]:
    """Every call to an entry point under ``node``, by either spelling that reaches one."""
    return [
        found
        for found in ast.walk(node)
        if isinstance(found, ast.Call)
        and found.args
        and (
            _callee_name(found.func) in names
            or (
                isinstance(found.func, ast.Attribute)
                and found.func.attr in ENTRY_POINTS
                and _is_entry_module(found.func.value, aliases)
            )
        )
    ]


class Sites:
    """What one scan of the tree found.

    ``unmodelled`` is the honest half: a site whose stat this cannot follow to a method — one
    passed as a bare name, built by a helper, or made outside any class. Reported rather than
    skipped, because a site cleared by never having been read is the failure this check exists
    to prevent.

    ``reaching`` has two sources and one meaning. A stat that walks to a command in the guest is
    one; a reach for core's own guest-side stat is the other, and it needs no walk because the
    name already says where the answer comes from.
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
        package = module.relative_to(PACKAGES).parts[0]
        inside_core = package == CORE_PACKAGE_DIR
        names = _entry_names(tree, inside_core=inside_core)
        aliases = _aliases(tree, inside_core=inside_core)
        helpers = _guest_stat_uses(tree, aliases, inside_core=inside_core)
        if not names and not any(aliases) and not helpers:
            continue
        where = module.relative_to(REPO_ROOT).as_posix()
        for helper in helpers:
            found.reaching.setdefault(package, []).append(f"{where}: imports {helper}")
        read: set[int] = set()
        for klass in (n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)):
            methods: dict[str, ast.AST] = {
                node.name: node
                for node in klass.body
                if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
            }
            for call in _entry_calls(klass, names, aliases):
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
            f"{where}:{call.lineno}"
            for call in _entry_calls(tree, names, aliases)
            if id(call) not in read
        ]
    return found


@pytest.fixture(scope="module")
def sites() -> Sites:
    return _sites()


@pytest.mark.parametrize(
    "source",
    [
        "from maf_sandbox.paths import stat_by_asking_the_guest",
        """from maf_sandbox import paths
paths.stat_by_asking_the_guest(run, guest, rel)""",
        """import maf_sandbox.paths
maf_sandbox.paths.stat_by_asking_the_guest_as_root(run, guest, rel)""",
        """import maf_sandbox.paths as p
p.stat_by_asking_the_guest(run, guest, rel)""",
        """import maf_sandbox as sandbox
sandbox.paths.stat_by_asking_the_guest(run, guest, rel)""",
        """from . import paths
paths.stat_by_asking_the_guest_as_root(run, guest, rel)""",
    ],
    ids=[
        "from-import",
        "module-attribute",
        "dotted",
        "aliased-module",
        "aliased-package",
        "relative",
    ],
)
def test_every_spelling_that_reaches_the_guest_side_stat_is_read(source: str) -> None:
    """`maf_sandbox.paths` is reach-by-name, so the module forms are ordinary rather than
    exotic. A scanner reading only `from ... import <name>` would clear a backend that wrote
    any of the other three, and the README requirement would go unenforced."""
    tree = ast.parse(source)
    assert _guest_stat_uses(tree, _aliases(tree, inside_core=True), inside_core=True)


def test_a_backends_own_relative_module_is_not_core() -> None:
    """`from . import paths` names core only inside `maf_sandbox`. In a backend it names that
    package's own module, and reading it as core would fail the repository over a call on
    something unrelated."""
    source = """from . import paths
paths.stat_by_asking_the_guest(ask, guest, rel)"""
    tree = ast.parse(source)
    assert not _guest_stat_uses(tree, _aliases(tree, inside_core=False), inside_core=False)
    assert _guest_stat_uses(tree, _aliases(tree, inside_core=True), inside_core=True)


def test_a_backends_own_relative_paths_module_is_not_core() -> None:
    """`from .paths import …` parses with ``module == "paths"`` and ``level == 1``, which is core
    inside `maf-sandbox` and the importing package's own helper anywhere else. Both the helper
    names and the entry points are read through the same predicate, so both are pinned here."""
    helper = "from .paths import stat_by_asking_the_guest"
    tree = ast.parse(helper)
    assert not _guest_stat_uses(tree, _aliases(tree, inside_core=False), inside_core=False)
    assert _guest_stat_uses(tree, _aliases(tree, inside_core=True), inside_core=True)

    entry = """from .paths import refuse_symlinked_ancestors
refuse_symlinked_ancestors(self._stat_guest, guest, work)"""
    tree = ast.parse(entry)
    assert not _entry_calls(
        tree, _entry_names(tree, inside_core=False), _aliases(tree, inside_core=False)
    )
    assert _entry_calls(
        tree, _entry_names(tree, inside_core=True), _aliases(tree, inside_core=True)
    )


def test_a_bare_absolute_paths_import_is_never_core() -> None:
    """Nothing reaches core's submodule as a top-level `paths`, so an absolute import spelling it
    that way names some other project's module."""
    tree = ast.parse("from paths import stat_by_asking_the_guest")
    for inside_core in (True, False):
        assert not _guest_stat_uses(
            tree, _aliases(tree, inside_core=inside_core), inside_core=inside_core
        )


def test_reaching_the_module_alone_is_not_a_declaration() -> None:
    """`from maf_sandbox import paths` is how a module reaches the confinement helpers at
    all. Read as a declaration it would flag every one of them, so the *use* is what counts.
    """
    source = """from maf_sandbox import paths
paths.refuse_symlinked_ancestors(stat, guest, work)"""
    tree = ast.parse(source)
    assert not _guest_stat_uses(tree, _aliases(tree, inside_core=True), inside_core=True)


def test_a_confinement_call_through_the_module_is_a_call_site_this_reads() -> None:
    source = """from maf_sandbox import paths
paths.refuse_symlinked_ancestors(self._stat_guest, guest, work)"""
    tree = ast.parse(source)
    assert _entry_calls(
        tree, _entry_names(tree, inside_core=True), _aliases(tree, inside_core=True)
    )


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
        "declare it in ANSWERED_INSIDE_THE_GUEST and say so in the package's README. Importing "
        "`maf_sandbox.paths.stat_by_asking_the_guest` counts: it is core's spelling of the same "
        "posture, offered so nobody invents another, and it is not a way to stop declaring one."
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
