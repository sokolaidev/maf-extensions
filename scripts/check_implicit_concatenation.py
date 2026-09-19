"""Refuse two literals sitting side by side inside a list, tuple or set literal.

    python scripts/check_implicit_concatenation.py

Python joins adjacent literals, so a comma left out between two strings in a collection is not
an error: the list is one element shorter and the program runs. Nothing local reports it — ruff
has no rule for the form that spans lines, and pyright sees a well-typed list — which leaves
CodeQL's ``py/implicit-string-concatenation-in-list``, on a check that does not gate. So what a
finding here asks for is the comma, or parentheses around the parts when one value was meant;
parentheses are what that query exempts too.

In scope: a run of adjacent `str`, `bytes` or f-string literals inside an element of a `[…]`,
`(…)` or `{…}` display. The element need not *be* those literals — a run at the end of a `+`
chain, or behind a method call, is the same ambiguity. Out of scope: a run whose nearest
comma-separated container is something else. An argument list is what that qualification is
for, since this repository writes hundreds of messages across two lines inside a call, and a
dict is out because a comma lost between two pairs puts a second ``:`` in one and does not
parse.

One shape is beyond any rule here: a two-element tuple whose only comma is the missing one.
``("left" "right")`` is not a tuple, it is a parenthesized string — character for character the
form this check asks for when one value was meant — so no reading of the source can separate
the two. ``("left" "right",)`` keeps its trailing comma and is reported, and an annotated
target fails pyright; an unannotated one is caught by nothing.
"""

from __future__ import annotations

import ast
import io
import subprocess
import sys
import tokenize
from pathlib import Path
from typing import NamedTuple

#: The displays whose parts a comma separates, and so the ones a missing comma shortens.
_COLLECTIONS = (ast.List, ast.Tuple, ast.Set)

#: What each of them is called in a report.
_NAMES = {ast.List: "list", ast.Tuple: "tuple", ast.Set: "set"}

#: Tokens carrying no syntax, dropped before anything reads what sits beside a literal.
_NOISE = frozenset(
    {
        tokenize.COMMENT,
        tokenize.NL,
        tokenize.NEWLINE,
        tokenize.INDENT,
        tokenize.DEDENT,
    }
)

_Position = tuple[int, int]
_Span = tuple[_Position, _Position]


class Concatenation(NamedTuple):
    """One collection element written as several adjacent literals."""

    line: int
    parts: int
    collection: str
    parenthesized: bool


class _Run(NamedTuple):
    """Literals written side by side, which Python joins into one value."""

    span: _Span
    parts: int
    parenthesized: bool


def repo_root() -> Path:
    """The tree that is scanned, its own function so a test can point the check elsewhere."""
    return Path(__file__).resolve().parent.parent


def tracked_python(root: Path) -> list[Path]:
    """The Python files git tracks, so an untracked scratch file is never reported."""
    listed = subprocess.run(
        ["git", "ls-files", "-z", "*.py"],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    )
    return [root / name for name in listed.stdout.split("\0") if name]


def _fstring_end(tokens: list[tokenize.TokenInfo], start: int) -> int:
    """The index of the `FSTRING_END` closing the f-string that opens at ``start``."""
    depth = 0
    for index in range(start, len(tokens)):
        if tokens[index].type == tokenize.FSTRING_START:
            depth += 1
        elif tokens[index].type == tokenize.FSTRING_END:
            depth -= 1
            if depth == 0:
                return index
    return len(tokens) - 1


def _runs(tokens: list[tokenize.TokenInfo]) -> list[_Run]:
    """Every place two or more literals are written side by side, innermost ones included.

    An f-string counts as one literal here, and its replacement fields are scanned separately:
    a list written inside ``f"{…}"`` can lose a comma the same way any other can, while the
    literals in ``f"{mapping['key']}"`` are parts of nothing.
    """
    found: list[_Run] = []
    run: list[_Span] = []
    opened = 0
    index = 0

    def close(after: int) -> None:
        if len(run) > 1:
            behind = tokens[opened - 1] if opened else None
            ahead = tokens[after] if after < len(tokens) else None
            found.append(
                _Run(
                    span=(run[0][0], run[-1][1]),
                    parts=len(run),
                    parenthesized=(
                        behind is not None
                        and behind.string == "("
                        and ahead is not None
                        and ahead.string == ")"
                    ),
                )
            )
        run.clear()

    while index < len(tokens):
        token = tokens[index]
        if token.type == tokenize.FSTRING_START:
            closing = _fstring_end(tokens, index)
            found.extend(_runs(tokens[index + 1 : closing]))
            opened = index if not run else opened
            run.append((token.start, tokens[closing].end))
            index = closing + 1
            continue
        if token.type == tokenize.STRING:
            opened = index if not run else opened
            run.append((token.start, token.end))
            index += 1
            continue
        close(index)
        index += 1
    close(index)
    return found


def _comma_separated(node: ast.AST) -> list[ast.expr]:
    """The parts of ``node`` that a comma, or a replacement field's braces, holds apart.

    A run inside one of these belongs to that part, so the innermost one owning a run decides
    whether the run reads as a missing comma. Left out on purpose: `+`, a conditional, a
    comparison — none separates its operands with a comma, so a run under one of those still
    belongs to whatever encloses it.
    """
    if isinstance(node, _COLLECTIONS):
        return list(node.elts)
    if isinstance(node, ast.Dict):
        return [key for key in node.keys if key is not None] + list(node.values)
    if isinstance(node, ast.Call):
        return list(node.args) + [keyword.value for keyword in node.keywords]
    if isinstance(node, ast.Subscript):
        return [node.slice]
    if isinstance(node, ast.JoinedStr):
        return [value for value in node.values if isinstance(value, ast.FormattedValue)]
    if isinstance(node, (ast.ListComp, ast.SetComp, ast.GeneratorExp)):
        return [node.elt]
    if isinstance(node, ast.DictComp):
        return [node.key, node.value]
    if isinstance(node, ast.Lambda):
        return list(node.args.defaults) + [default for default in node.args.kw_defaults if default]
    return []


def _character_position(lines: list[str], position: _Position) -> _Position:
    """An `ast` position in the tokenizer's coordinates, whose columns count characters.

    `ast` counts a column in UTF-8 bytes, so on a line holding any non-ASCII text the two
    disagree and a span compared across them covers the wrong source.
    """
    row, column = position
    return row, len(lines[row - 1].encode("utf-8")[:column].decode("utf-8"))


def _span(lines: list[str], node: ast.expr) -> _Span:
    """Where ``node`` begins and ends, in the tokenizer's coordinates."""
    return (
        _character_position(lines, (node.lineno, node.col_offset)),
        _character_position(lines, (node.end_lineno or node.lineno, node.end_col_offset or 0)),
    )


def _owner(parts: list[tuple[_Span, ast.AST]], run: _Run) -> ast.AST | None:
    """The node whose own part holds ``run`` most closely, or None if nothing does."""
    owner: ast.AST | None = None
    narrowest: _Span | None = None
    for span, node in parts:
        if span[0] <= run.span[0] and run.span[1] <= span[1]:
            if narrowest is None or (span[0] >= narrowest[0] and span[1] <= narrowest[1]):
                owner, narrowest = node, span
    return owner


def concatenations(source: str) -> list[Concatenation]:
    """Every collection element built from more than one literal, parenthesized or not.

    Raises :exc:`SyntaxError` on source that does not parse, which the caller reports: a file
    skipped for being unreadable is a file this check does not cover.
    """
    tree = ast.parse(source)
    tokens = [
        token
        for token in tokenize.generate_tokens(io.StringIO(source).readline)
        if token.type not in _NOISE
    ]
    lines = io.StringIO(source).readlines()
    parts = [
        (_span(lines, part), node) for node in ast.walk(tree) for part in _comma_separated(node)
    ]

    found: list[Concatenation] = []
    for run in _runs(tokens):
        owner = _owner(parts, run)
        if not isinstance(owner, _COLLECTIONS):
            continue
        found.append(
            Concatenation(
                line=run.span[0][0],
                parts=run.parts,
                collection=_NAMES[type(owner)],
                parenthesized=run.parenthesized,
            )
        )
    return found


def findings(source: str, path: str) -> list[str]:
    """The `path:line: …` report for each concatenation that does not say it is one value."""
    return [
        f"{path}:{found.line}: {found.parts} adjacent literals make one {found.collection} "
        "element. Add the comma, or wrap them in parentheses to say one value was meant."
        for found in concatenations(source)
        if not found.parenthesized
    ]


def main(argv: list[str]) -> int:
    """CLI entry: report every collection element that reads as a missing comma, exit 1 if any."""
    if len(argv) != 1:
        print(f"usage: {argv[0]}", file=sys.stderr)
        return 2
    root = repo_root()
    problems: list[str] = []
    for path in tracked_python(root):
        name = path.relative_to(root).as_posix()
        source = path.read_text(encoding="utf-8")
        try:
            problems.extend(findings(source, name))
        except (SyntaxError, tokenize.TokenError) as error:
            problems.append(f"{name}: could not be read as Python, so nothing was checked: {error}")
    if not problems:
        print("no collection literal hides a missing comma")
        return 0
    for problem in sorted(problems):
        print(problem, file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
