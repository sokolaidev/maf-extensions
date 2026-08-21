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
_DOCUMENTATION_SUFFIXES = frozenset({".md", ".markdown", ".rst", ".txt", ".adoc"})
_DOCUMENTATION_NAMES = frozenset({"license", "copying", "notice"})


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


def is_documentation_path(path: str) -> bool:
    """Whether a changed non-Python path is conventionally documentation-only."""
    normalized = path.replace("\\", "/")
    name = Path(normalized).name.lower()
    return Path(name).suffix in _DOCUMENTATION_SUFFIXES or name in _DOCUMENTATION_NAMES


def title_type(title: str) -> str | None:
    """Return the Conventional Commit type, or ``None`` for a title this check cannot classify."""
    match = _SUBJECT.match(title.strip())
    return match.group("type") if match else None


def assess(
    title: str, changed_paths: list[str], changed_python: dict[str, tuple[str | None, str | None]]
) -> list[str]:
    """Return title/diff mismatches, or an empty list when the title matches the diff."""
    kind = title_type(title)
    if kind is None:
        return []

    executable = any(
        not is_documentation_path(path) for path in changed_paths if not path.endswith(".py")
    )
    executable |= any(python_changed(before, after) for before, after in changed_python.values())

    if kind in _BEHAVIOR_TYPES and not executable:
        return [
            f"{kind}: titles must include an executable change; this diff is documentation-only",
            "retitle the pull request as docs: or include a behavior change",
        ]
    if kind in _DOCUMENTATION_TYPES and executable:
        return [
            f"{kind}: titles must not hide executable changes in the diff",
            "retitle the pull request to describe the behavior change",
        ]
    return []


def _git(*args: str) -> str:
    return subprocess.check_output(["git", *args], text=True, encoding="utf-8").strip()


def _changed_python(base: str) -> dict[str, tuple[str | None, str | None]]:
    """Read changed Python files, preserving rename sources for the AST comparison."""
    result: dict[str, tuple[str | None, str | None]] = {}
    status = _git("diff", "--find-renames", "--name-status", f"{base}...HEAD")
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
            before = _git("show", f"{base}:{before_path}") if kind[0] != "A" else None
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
    paths = _git("diff", "--name-only", f"{base}...HEAD").splitlines()
    problems = assess(title, paths, _changed_python(base))
    for problem in problems:
        print(problem, file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
