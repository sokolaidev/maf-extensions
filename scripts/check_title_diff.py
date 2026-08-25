"""Ensure a PR title's release semantics match the changed files.

The check compares changed Python files after removing module, class and function docstrings.
Comments and formatting therefore do not count as behavior, while executable statements, literals,
annotations and defaults do. Non-documentation paths are treated as executable because this check
cannot infer behavior from arbitrary formats such as TOML or workflow YAML.

That last rule is why release-please's own pull requests are exempt. A Release PR is a version
bump and a regenerated changelog — `pyproject.toml`, `uv.lock` and the manifest — which this
check cannot read as anything but executable, against a `chore(main): release …` title nobody
chose and nobody may edit.

The exemption is identity, never the shape of the diff: the same diff under the same title,
pushed by a person, is still refused. Identity is three facts and needs all three — see
`is_generated_release`. It is not a security boundary and cannot be one, since this workflow
runs the pull request's own copy of this file; it is there so the exemption cannot be claimed
by a branch name alone, which would leave nothing in the diff for a reviewer to notice.
"""

from __future__ import annotations

import argparse
import ast
import re
import subprocess
import sys
from pathlib import Path

_SUBJECT = re.compile(r"^(?P<type>[a-z]+)(?:\([^)]*\))?(?P<breaking>!)?:")
_VALID_TYPES = ("feat", "fix", "perf", "revert", "docs", "refactor", "test", "build", "ci", "chore")
_BEHAVIOR_TYPES = frozenset({"feat", "fix", "perf", "revert"})
_DOCUMENTATION_TYPES = frozenset({"docs", "chore", "refactor", "test", "build", "ci"})
_DOCUMENTATION_SUFFIXES = frozenset({".md", ".markdown", ".rst", ".adoc"})
_DOCUMENTATION_NAMES = frozenset({"license", "copying", "notice"})
_TEST_DIR_NAMES = frozenset({"test", "tests"})

#: The three facts that together identify a Release PR, none of which is sufficient alone.
#:
#: The **branch namespace** is what separates a Release PR from the *range* pull request —
#: `chore/maf-sandbox-range-…`, which moves dependency bounds, a behavior change RELEASING.md
#: requires be titled `fix:` and this check is what holds it there. The same bot opens both, so
#: the author cannot tell them apart and only the branch can. It is the full namespace
#: release-please generates rather than a shorter lead-in: `release-please--anything` is not a
#: name it produces, and treating one as generated exempts a pull request nobody generated.
#:
#: The **repository** is what a fork cannot forge. A branch name is chosen by whoever pushes it,
#: so the branch alone would let any fork exempt itself by naming its branch well — invisibly,
#: since nothing about the choice appears in the diff a reviewer reads.
#:
#: The **author** is what a collaborator pushing such a branch to this repository cannot supply.
_RELEASE_BRANCH_PREFIX = "release-please--branches--"
_RELEASE_AUTHOR = "github-actions[bot]"


class _RemoveDocstrings(ast.NodeTransformer):
    """Remove only docstrings from module, class and function bodies."""

    def visit_Module(self, node: ast.Module) -> ast.Module:
        self.generic_visit(node)
        node.body = _without_docstring(node.body)
        return node

    def visit_ClassDef(self, node: ast.ClassDef) -> ast.ClassDef:
        self.generic_visit(node)
        node.body = _without_docstring(node.body)
        return node

    def visit_FunctionDef(self, node: ast.FunctionDef) -> ast.FunctionDef:
        self.generic_visit(node)
        node.body = _without_docstring(node.body)
        return node

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> ast.AsyncFunctionDef:
        self.generic_visit(node)
        node.body = _without_docstring(node.body)
        return node


def _without_docstring(body: list[ast.stmt]) -> list[ast.stmt]:
    if body and _is_docstring(body[0]):
        return body[1:]
    return body


def _is_docstring(statement: ast.stmt) -> bool:
    return (
        isinstance(statement, ast.Expr)
        and isinstance(statement.value, ast.Constant)
        and isinstance(statement.value.value, str)
    )


def normalized_python(source: str) -> str:
    """Return a stable AST representation with module, class and function docstrings removed."""
    tree = _RemoveDocstrings().visit(ast.parse(source))
    ast.fix_missing_locations(tree)
    return ast.dump(tree, annotate_fields=True, include_attributes=False)


def python_changed(before: str | None, after: str | None) -> bool:
    """Whether the executable AST differs between two Python file contents."""
    before_normalized = normalized_python(before) if before is not None else ""
    after_normalized = normalized_python(after) if after is not None else ""
    return before_normalized != after_normalized


def is_test_path(path: str) -> bool:
    """Whether a path belongs to a test directory and does not ship as package behavior."""
    return any(part.lower() in _TEST_DIR_NAMES for part in path.replace("\\", "/").split("/"))


def is_documentation_path(path: str) -> bool:
    """Whether a changed path is conventionally documentation-only."""
    normalized = path.replace("\\", "/")
    parts = normalized.lower().split("/")
    name = parts[-1]
    return (
        "docs" in parts
        or name in {"readme", "changelog"}
        or name.startswith(("readme.", "changelog."))
        or Path(name).suffix in _DOCUMENTATION_SUFFIXES
        or name in _DOCUMENTATION_NAMES
    )


def is_package_test_path(path: str) -> bool:
    """Whether ``path`` is a package's own test tree — the paths release attribution excuses.

    Narrower than :func:`is_test_path` on purpose: it matches only ``packages/<name>/tests/``,
    which is what each package excludes in ``release-please-config.json``. A ``tests``
    directory nested anywhere else — inside ``src/``, say — ships, attributes, and releases.
    """
    parts = path.replace("\\", "/").split("/")
    return len(parts) > 3 and parts[0] == "packages" and parts[2] == "tests"


def is_non_behavior_path(path: str) -> bool:
    """Whether a changed path is a test or documentation rather than shipped behavior."""
    return is_test_path(path) or is_documentation_path(path)


def _package_for(path: str) -> str | None:
    parts = path.replace("\\", "/").split("/")
    return parts[1] if len(parts) > 2 and parts[:1] == ["packages"] else None


def title_type(title: str) -> str | None:
    """Return the Conventional Commit type, or ``None`` for a title this check cannot classify."""
    match = _SUBJECT.match(title.strip())
    return match.group("type") if match else None


def assess(
    title: str,
    changed_paths: list[str],
    changed_python: dict[str, tuple[str | None, str | None]],
    changed_path_pairs: list[tuple[str, ...]] | None = None,
    copied_sources: set[str] | None = None,
) -> list[str]:
    """Return title/diff mismatches, or an empty list when the title matches the diff."""
    kind = title_type(title)
    if kind is None:
        return []

    pairs = changed_path_pairs or [(path,) for path in changed_paths]
    executable_paths: set[str] = set()
    touched_packages: set[str] = set()
    executable_packages: set[str] = set()
    for paths in pairs:
        # The destination decides, because release-please reads a commit's files by their
        # current path: a rename's old location is attributed to nobody, so counting it here
        # would refuse a title over a package that will not be released.
        destination = paths[-1]
        # `is_package_test_path`, not `is_test_path`: this predicate has to match
        # `exclude-paths` in release-please-config.json exactly, and a wider one excuses a
        # package here that the changelog still attributes to.
        if (
            not is_package_test_path(destination)
            and (package := _package_for(destination)) is not None
        ):
            touched_packages.add(package)
        executable_paths_in_pair = (
            paths[1:] if copied_sources and paths and paths[0] in copied_sources else paths
        )
        for path in executable_paths_in_pair:
            if path.endswith(".py"):
                if len(paths) == 2 and paths[0] != paths[1] and not is_test_path(path):
                    executable_paths.add(path)
            elif not is_non_behavior_path(path):
                executable_paths.add(path)
    for path, (before, after) in changed_python.items():
        if not is_test_path(path) and (
            before is None or after is None or python_changed(before, after)
        ):
            executable_paths.add(path)
    executable_packages.update(
        package for path in executable_paths if (package := _package_for(path)) is not None
    )
    product_executable = bool(executable_packages)

    behavior_present = bool(touched_packages) and not (touched_packages - executable_packages)
    if kind in _BEHAVIOR_TYPES and not behavior_present:
        return [
            f"{kind}: no executable change was found for the changed files",
            "retitle the pull request to match the changed files or include a behavior change",
        ]
    if kind in _DOCUMENTATION_TYPES and product_executable:
        valid_prefixes = ", ".join(
            f"{title_type}{scope}{breaking}:"
            for title_type in _VALID_TYPES
            for scope in ("", "(...)")
            for breaking in ("", "!")
        )
        return [
            f"{kind}: this title describes a non-behavioral change, but the diff changes executable code",
            "valid title prefixes are: "
            + valid_prefixes
            + "; if the behavior change is intentional, use a behavior type; otherwise move the "
            "executable changes to a separate pull request",
        ]
    return []


def _git(*args: str) -> str:
    return subprocess.check_output(["git", *args], text=True, encoding="utf-8").strip()


def _changed_path_pairs(status: str) -> list[tuple[str, ...]]:
    """Return the paths represented by each changed-file status entry."""
    pairs: list[tuple[str, ...]] = []
    for line in status.splitlines():
        fields = line.split("\t")
        kind, paths = fields[0], fields[1:]
        if kind.startswith(("R", "C")) and len(paths) == 2:
            pairs.append((paths[0], paths[1]))
        elif len(paths) == 1:
            pairs.append((paths[0],))
    return pairs


def _changed_python(
    base: str, status: str | None = None
) -> dict[str, tuple[str | None, str | None]]:
    """Read changed Python files, preserving rename sources for the AST comparison."""
    result: dict[str, tuple[str | None, str | None]] = {}
    status = status or _git("diff", "--find-renames", "--name-status", f"{base}...HEAD")
    for line in status.splitlines():
        fields = line.split("\t")
        kind, paths = fields[0], fields[1:]
        if kind.startswith(("R", "C")) and len(paths) == 2:
            before_path, after_path = paths
        elif len(paths) == 1:
            before_path = after_path = paths[0]
        else:
            continue
        if not after_path.endswith(".py"):
            continue
        try:
            before = (
                _git("show", f"{base}:{before_path}")
                if kind[0] != "A" and before_path.endswith(".py")
                else None
            )
        except subprocess.CalledProcessError:
            before = None
        try:
            after = Path(after_path).read_text(encoding="utf-8")
        except FileNotFoundError:
            after = None
        result[after_path] = (before, after)
    return result


def is_generated_release(head_ref: str, head_repo: str, base_repo: str, author: str) -> bool:
    """Whether these four facts identify a Release PR release-please opened.

    There is no author to hold the title to on one: release-please writes it, and editing it
    desynchronises the changelog it generates from the version it stamps.

    All four must hold, and each rules out a different way of claiming to be one. Any empty
    argument therefore fails closed, which is what makes a caller that stops passing them get
    the check back rather than a blanket exemption.
    """
    return bool(
        head_ref.startswith(_RELEASE_BRANCH_PREFIX)
        and head_repo
        and head_repo == base_repo
        and author == _RELEASE_AUTHOR
    )


def main(argv: list[str]) -> int:
    """Check the current checkout against ``base`` and ``title``.

    The four pull request facts are optional and named rather than positional, because each is
    a security-relevant input and a value silently landing in the wrong slot is the failure a
    positional list of four invites. Omitting any of them checks rather than skips: a caller
    that stops passing them gets this check back, which is the safe direction to fail in.
    """
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("base")
    parser.add_argument("title")
    parser.add_argument("--head-ref", default="")
    parser.add_argument("--head-repo", default="")
    parser.add_argument("--base-repo", default="")
    parser.add_argument("--author", default="")
    try:
        options = parser.parse_args(argv[1:])
    except SystemExit as requested:
        # argparse exits 0 for `--help` and 2 for a bad argument, and its code is carried out
        # rather than flattened: answering "how do I call this" with a failure is a lie about
        # the thing the caller just asked. A non-integer code is argparse printing a message,
        # which is the bad-argument case.
        return requested.code if isinstance(requested.code, int) else 2
    base, title = options.base, options.title
    if is_generated_release(options.head_ref, options.head_repo, options.base_repo, options.author):
        print(f"{options.head_ref}: opened by release-please; its title is not an author's choice")
        return 0
    status = _git("diff", "--find-renames", "--name-status", f"{base}...HEAD")
    path_pairs = _changed_path_pairs(status)
    copied_sources = {
        fields[1]
        for line in status.splitlines()
        for fields in [line.split("\t")]
        if fields[0].startswith("C") and len(fields) == 3
    }
    paths = [path for pair in path_pairs for path in pair[-1:]]
    problems = assess(title, paths, _changed_python(base, status), path_pairs, copied_sources)
    for problem in problems:
        print(problem, file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
