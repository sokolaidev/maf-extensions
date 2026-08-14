"""Set the dependents' maf-sandbox range after a core release — both bounds, one edit.

    python scripts/set_dependents_range.py <released-version>
    python scripts/set_dependents_range.py --print-title <released-version>

Two bounds move after a core release, and they live in one string. This used to be two
scripts run by two steps opening two pull requests that rewrote the same line in the same five
files (#195): neither conflicted with `main`, so both looked mergeable, and whichever merged
second reverted the other. One writer, one pull request.

**The floor** moves to the released version, for a dependent whose ceiling admits it and whose
floor is a minor behind. That is a mechanical selection of candidates, not a detection of
adoption — ceilings are widened for everyone before a release so the published set stays
resolvable (RELEASING.md, Release order), so admitting a version says nothing about whether a
package's code needs it. Whether a floor should move is the reviewer's call on the pull
request this opens.

**The ceiling** moves two minors up, so the release after this one is admitted before it
exists: `<0.8` would exclude the 0.8.0 it is meant to admit.

Both refusals from the scripts this replaces are kept. The ceiling only ever widens, so a
patch changes nothing. The floor is judged against the ceiling **as it was**, not as this run
leaves it — otherwise widening would authorise the very floor bump the old ceiling refused,
and a deliberately narrow ceiling would silently become an adoption.

`--print-title` prints the commit subject for what this would change, without changing it, so
the workflow naming it in a commit and a pull request reads it from here rather than deriving
the rule a second time — and so the subject can name the bounds that actually moved.

Exits non-zero if a package that depends on maf-sandbox carries a constraint this cannot read:
editing by pattern silently no-ops when the string drifts, and a release step that quietly
does nothing while looking healthy is worse than one that stops.
"""

from __future__ import annotations

import re
import sys
import tomllib
from pathlib import Path

_CONSTRAINT = re.compile(r"maf-sandbox>=(\d+(?:\.\d+)*),<(\d+(?:\.\d+)*)")
#: The distribution name at the head of a dependency string, before any version operator.
_DIST_NAME = re.compile(r"[A-Za-z0-9._-]+")

FLOOR = "floor"
CEILING = "ceiling"


def _version(text: str) -> tuple[int, ...]:
    return tuple(int(part) for part in text.split("."))


def _text(version: tuple[int, ...]) -> str:
    return ".".join(str(part) for part in version)


def _admits(version: tuple[int, ...], ceiling: tuple[int, ...]) -> bool:
    """Whether ``version`` is below the ``<ceiling`` bound, comparing at equal width."""
    width = max(len(version), len(ceiling))
    return version + (0,) * (width - len(version)) < ceiling + (0,) * (width - len(ceiling))


def parse_constraint(constraint: str) -> tuple[tuple[int, ...], tuple[int, ...]] | None:
    """The ``(floor, ceiling)`` of a ``maf-sandbox>=X,<Y`` constraint, or ``None`` if not that."""
    match = _CONSTRAINT.search(constraint)
    if match is None:
        return None
    return _version(match.group(1)), _version(match.group(2))


def target_ceiling(released: tuple[int, ...]) -> tuple[int, ...]:
    """The bound that admits the next minor after ``released``.

    Two minors up, not one: a ceiling of ``<0.8`` excludes 0.8.0 itself, and admitting the
    next release is the whole point.
    """
    major, minor = (tuple(released) + (0, 0))[:2]
    return (major, minor + 2)


def set_range(text: str, released: tuple[int, ...]) -> tuple[str, frozenset[str]]:
    """Rewrite a pyproject's maf-sandbox range; return the text and which bounds moved.

    Each bound keeps its original spelling when it does not move, so widening a ceiling never
    reformats the floor beside it.
    """
    match = _CONSTRAINT.search(text)
    if match is None:
        return text, frozenset()
    floor, ceiling = _version(match.group(1)), _version(match.group(2))

    moved: set[str] = set()
    # Judged against the ceiling as it stands, before the widening below. See the module
    # docstring: the two used to run on separate checkouts of `main`, and this keeps that.
    floor_text = match.group(1)
    if _admits(released, ceiling) and released[:2] > floor[:2]:
        floor_text = _text(released)
        moved.add(FLOOR)

    ceiling_text = match.group(2)
    target = target_ceiling(released)
    if ceiling < target:
        ceiling_text = _text(target)
        moved.add(CEILING)

    if not moved:
        return text, frozenset()
    new_text = _CONSTRAINT.sub(f"maf-sandbox>={floor_text},<{ceiling_text}", text, count=1)
    return new_text, frozenset(moved)


def _base_dependency(dependencies: list[str]) -> str | None:
    """The dependency on the base ``maf-sandbox`` distribution exactly, or ``None``.

    Read from the parsed dependency string, not the file text, so it does not matter whether
    the pyproject quotes with ``"`` or ``'`` — and the exact name match keeps a dependency on
    the sibling ``maf-sandbox-acas`` from being taken for one on the base package.
    """
    for dep in dependencies:
        name = _DIST_NAME.match(dep.strip())
        if name is not None and name.group(0) == "maf-sandbox":
            return dep
    return None


def plan(released_text: str, repo_root: Path) -> list[tuple[Path, str, frozenset[str]]]:
    """What this would write, without writing it: ``(path, new text, bounds moved)``.

    Separate from :func:`run` so ``--print-title`` can name what is about to change without
    changing it, and so the workflow never re-derives the rule.
    """
    released = _version(released_text)
    planned: list[tuple[Path, str, frozenset[str]]] = []
    for path in sorted(repo_root.glob("packages/*/pyproject.toml")):
        text = path.read_text("utf-8")
        project = tomllib.loads(text).get("project", {})
        if project.get("name") == "maf-sandbox":
            continue
        base = _base_dependency(project.get("dependencies", []))
        if base is None:
            continue
        if parse_constraint(base) is None:
            raise SystemExit(
                f"{path}: depends on maf-sandbox but not as 'maf-sandbox>=X,<Y'; this script "
                "cannot edit it, and failing beats silently skipping a release-time step."
            )
        new_text, moved = set_range(text, released)
        if moved:
            planned.append((path, new_text, moved))
    return planned


def run(released_text: str, repo_root: Path) -> list[Path]:
    """Apply :func:`plan` under ``repo_root``; return the files changed. May exit."""
    changed: list[Path] = []
    for path, new_text, _ in plan(released_text, repo_root):
        path.write_text(new_text, "utf-8")
        changed.append(path)
    return changed


def title(released_text: str, moved: frozenset[str]) -> str:
    """The commit subject for the bounds that moved, or ``""`` when none did.

    `fix:` is required rather than stylistic: `chore:` and `ci:` release nothing here, and
    both halves are only worth anything once *published* — the ceiling because the next core
    release checks the index before it uploads, the floor because a floor nobody can install
    is not a constraint. See RELEASING.md, Release order.
    """
    released = _version(released_text)
    admitted = target_ceiling(released)
    admits = f"{admitted[0]}.{admitted[1] - 1}"
    if moved == frozenset({FLOOR, CEILING}):
        return (
            f"fix: require maf-sandbox {released_text} and admit {admits} in the dependents' range"
        )
    if moved == frozenset({CEILING}):
        return f"fix: admit maf-sandbox {admits} in the dependents' range"
    if moved == frozenset({FLOOR}):
        return f"fix: require maf-sandbox {released_text} in the packages that use it"
    return ""


def main(argv: list[str]) -> int:
    """CLI entry: with ``--print-title`` print the commit subject; otherwise apply ``plan`` to rewrite each dependent's range and print the bounds moved."""
    repo_root = Path(__file__).resolve().parent.parent
    if len(argv) == 3 and argv[1] == "--print-title":
        moved: set[str] = set()
        for _, _, bounds in plan(argv[2], repo_root):
            moved |= bounds
        print(title(argv[2], frozenset(moved)))
        return 0
    if len(argv) != 2:
        print(
            f"usage: {argv[0]} [--print-title] <released-version>",
            file=sys.stderr,
        )
        return 2
    planned = plan(argv[1], repo_root)
    if not planned:
        print("every dependent's range already covers this release; nothing to set")
        return 0
    for path, new_text, bounds in planned:
        path.write_text(new_text, "utf-8")
        print(f"  {path.parent.name}: {', '.join(sorted(bounds))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
