"""Ensure a PR title's release semantics match the changed files.

The check compares changed Python files after removing module, class and function docstrings.
Comments and formatting therefore do not count as behavior, while executable statements, literals,
annotations and defaults do. Non-documentation paths are treated as executable because this check
cannot infer behavior from arbitrary formats such as TOML or workflow YAML.
"""

from __future__ import annotations

import ast
import re
import subprocess
import sys
from pathlib import Path

_SUBJECT = re.compile(r"^(?P<type>[a-z]+)(?:\([^)]*\))?(?P<breaking>!)?:")
_BEHAVIOR_TYPES = frozenset({"feat", "fix", "perf", "revert"})
_DOCUMENTATION_TYPES = frozenset({"docs", "chore", "refactor", "test", "build", "ci"})
_DOCUMENTATION_SUFFIXES = frozenset({".md", ".markdown", ".rst", ".adoc"})
_DOCUMENTATION_NAMES = frozenset({"license", "copying", "notice"})
_TEST_DIR_NAMES = frozenset({"test", "tests"})


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
        package_names = {package for path in paths if (package := _package_for(path)) is not None}
        touched_packages.update(package_names)
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
    executable = bool(executable_paths)

    behavior_present = (
        bool(touched_packages) and not (touched_packages - executable_packages)
    ) or (not touched_packages and executable)
    if kind in _BEHAVIOR_TYPES and not behavior_present:
        return [
            f"{kind}: no executable change was found for the changed files",
            "retitle the pull request to match the changed files or include a behavior change",
        ]
    if kind in _DOCUMENTATION_TYPES and executable:
        return [
            f"{kind}: this title describes a non-behavioral change, but the diff changes executable code",
            "if the behavior change is intentional, retitle as feat:, fix:, perf:, or revert:; "
            "otherwise move the executable changes to a separate pull request",
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


def main(argv: list[str]) -> int:
    """Check the current checkout against ``base`` and ``title``."""
    if len(argv) != 3:
        print(f"usage: {argv[0]} <base-revision> <pull-request-title>", file=sys.stderr)
        return 2
    base, title = argv[1:]
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
