"""Refuse two literals sitting side by side inside a list, tuple or set literal.

    python scripts/check_implicit_concatenation.py

Python joins adjacent literals, so a comma left out between two strings in a collection is not
an error: the list is one element shorter and the program runs. Nothing else local reports it —
ruff has no rule for the form that spans lines, and pyright sees a well-typed list. CodeQL's
``py/implicit-string-concatenation-in-list`` does, on GitHub's default setup, after the push,
on a check that gates nothing.

What a finding asks for is the comma, or parentheses around the parts when one value was meant.
Parentheses are also what that query exempts, so a site this check passes is one the bot stays
quiet about.

Scope is the collection literals, because that is where a comma separates peers. An argument
list is out: this repository writes hundreds of messages across two lines inside a call, and a
check that reported those would be turned off within the week. A dict is out because a comma
lost between two pairs puts a second ``:`` in one, which does not parse. It is wider than the
query in two places, each the same ambiguity the query is about: a tuple or a set literal, and
a collection whose only element is the concatenation — which is what a two-element list becomes
when its one comma goes missing.
"""

from __future__ import annotations

import ast
import bisect
import io
import subprocess
import sys
import tokenize
from pathlib import Path
from typing import NamedTuple

#: The literals whose elements a comma separates, and so the ones a missing comma shortens.
_COLLECTIONS = (ast.List, ast.Tuple, ast.Set)

#: What each of them is called in a report.
_NAMES = {ast.List: "list", ast.Tuple: "tuple", ast.Set: "set"}

#: Tokens carrying no syntax, dropped before looking at what sits either side of an element.
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


class Concatenation(NamedTuple):
    """One collection element written as several adjacent literals."""

    line: int
    parts: int
    collection: str
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


def _literal_starts(tokens: list[tokenize.TokenInfo]) -> list[_Position]:
    """Where each literal begins — one position per part of a concatenation.

    A replacement field opens a nested scope whose own literals are parts of nothing, so
    ``f"{mapping['key']}"`` is one literal and not two.
    """
    starts: list[_Position] = []
    depth = 0
    for token in tokens:
        if token.type == tokenize.FSTRING_START:
            if depth == 0:
                starts.append(token.start)
            depth += 1
        elif token.type == tokenize.FSTRING_END:
            depth -= 1
        elif token.type == tokenize.STRING and depth == 0:
            starts.append(token.start)
    return starts


def _character_position(lines: list[str], position: _Position) -> _Position:
    """An `ast` position in the tokenizer's coordinates, whose columns count characters.

    `ast` counts a column in UTF-8 bytes, so on a line holding any non-ASCII text the two
    disagree and a span compared across them covers the wrong source.
    """
    row, column = position
    return row, len(lines[row - 1].encode("utf-8")[:column].decode("utf-8"))


def _wrapped_in_parentheses(
    significant: list[tokenize.TokenInfo],
    token_starts: list[_Position],
    span: tuple[_Position, _Position],
) -> bool:
    """Whether `(` and `)` sit either side of the span, the author saying this is one value.

    A tuple's own `(` cannot be read as that pair: an element reaching the tuple's `)` would
    make a one-element tuple, which needs a trailing comma, so the token after it is a `,`.
    """
    start, end = span
    opening = bisect.bisect_left(token_starts, start) - 1
    closing = bisect.bisect_left(token_starts, end)
    if opening < 0 or closing >= len(significant):
        return False
    return significant[opening].string == "(" and significant[closing].string == ")"


def concatenations(source: str) -> list[Concatenation]:
    """Every collection element built from more than one literal, parenthesized or not.

    Raises :exc:`SyntaxError` on source that does not parse, which the caller reports: a file
    skipped for being unreadable is a file this check does not cover.
    """
    tree = ast.parse(source)
    tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
    lines = io.StringIO(source).readlines()
    literals = _literal_starts(tokens)
    significant = [token for token in tokens if token.type not in _NOISE]
    token_starts = [token.start for token in significant]

    found: list[Concatenation] = []
    for node in ast.walk(tree):
        if not isinstance(node, _COLLECTIONS):
            continue
        for element in node.elts:
            if not isinstance(element, (ast.Constant, ast.JoinedStr)):
                continue
            if isinstance(element, ast.Constant) and not isinstance(element.value, (str, bytes)):
                continue
            start = _character_position(lines, (element.lineno, element.col_offset))
            end = _character_position(
                lines, (element.end_lineno or element.lineno, element.end_col_offset or 0)
            )
            parts = bisect.bisect_left(literals, end) - bisect.bisect_left(literals, start)
            if parts < 2:
                continue
            found.append(
                Concatenation(
                    line=element.lineno,
                    parts=parts,
                    collection=_NAMES[type(node)],
                    parenthesized=_wrapped_in_parentheses(significant, token_starts, (start, end)),
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
