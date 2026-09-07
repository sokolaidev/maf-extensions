"""Set the dependents' maf-sandbox range after a core release — both bounds, one edit.

    python scripts/set_dependents_range.py <released-version>
    python scripts/set_dependents_range.py --print-title <released-version>
    python scripts/set_dependents_range.py --samples <released-version>

Two bounds move after a core release, and they live in one string. This used to be two
scripts run by two steps opening two pull requests that rewrote the same line in the same five
files (#195): neither conflicted with `main`, so both looked mergeable, and whichever merged
second reverted the other. One writer, one pull request.

**The floor** moves to the released version, in every dependent a minor behind. With the ceiling
below, that leaves each dependent on exactly one core minor: the suite ships as a set rather than
carrying several core lines at once. Declining a hunk is still how a package opts out.

**The ceiling** admits the released line and nothing above it: 0.35.0 gives `<0.36`. It reached
a minor further until 0.35.0, when every published dependent admitting the unreleased next core
became a gate on it — `docs/release-compatibility.md` has the argument. It only ever widens, so
a patch changes nothing.

**The samples' floor moves only under `--samples`**, which switches the file set rather than
adding to it. Merged with the packages' hunk it takes the whole suite unsatisfiable whenever
the core reaches the index first (0.33.0, 0.34.0); the release workflow passes no flag, so what
it opens cannot carry a sample.

`--print-title` prints the subject for what the same arguments would change, so the workflow
does not derive the rule a second time.

Exits non-zero on a constraint it cannot read: editing by pattern silently no-ops when the
string drifts, and a release step that does nothing while looking healthy is worse than one
that stops.
"""

from __future__ import annotations

import re
import sys
import tomllib
from pathlib import Path

_CONSTRAINT = re.compile(r"maf-sandbox>=(\d+(?:\.\d+)*),<(\d+(?:\.\d+)*)")
#: The distribution name at the head of a dependency string, before any version operator.
_DIST_NAME = re.compile(r"[A-Za-z0-9._-]+")

#: A sample's floor, anchored to its own `#` line: the rewrite is `count=1`, and prose above
#: the block quoting `"maf-sandbox>=X"` would otherwise be the match that moves.
_SAMPLE_FLOOR = re.compile(r'(?m)^(?P<lead>#[ \t]+"maf-sandbox>=)(?P<floor>\d+(?:\.\d+)*)(?=")')
#: Looser, on purpose: the two disagreeing means a shape this cannot edit, which stops the run
#: rather than skipping it. The lookahead keeps `maf-sandbox-acas` from answering for the base.
_SAMPLE_BASE = re.compile(r'(?m)^#[ \t].*"maf-sandbox(?![-A-Za-z0-9_.])[^"]*"')

FLOOR = "floor"
CEILING = "ceiling"
SAMPLE_FLOOR = "sample floor"


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
    """The bound admitting ``released``'s line: ``<0.36`` takes every 0.35 patch, not 0.36.0."""
    major, minor = (tuple(released) + (0, 0))[:2]
    return (major, minor + 1)


def target_sample_floor(released: tuple[int, ...]) -> tuple[int, ...]:
    """The floor every sample declares: ``released``'s minor, so a patch rewrites nothing."""
    major, minor = (tuple(released) + (0, 0))[:2]
    return (major, minor)


def set_sample_floor(text: str, released: tuple[int, ...]) -> tuple[str, frozenset[str]]:
    """Rewrite a sample's PEP 723 maf-sandbox floor; return the text and whether it moved.

    Only ever upwards. A floor already at or above the release is left exactly as written,
    so re-running this over a tree it has already edited changes nothing.
    """
    match = _SAMPLE_FLOOR.search(text)
    if match is None:
        return text, frozenset()
    target = target_sample_floor(released)
    if target <= _version(match.group("floor"))[:2]:
        return text, frozenset()
    new_text = _SAMPLE_FLOOR.sub(
        lambda found: f"{found.group('lead')}{_text(target)}", text, count=1
    )
    return new_text, frozenset({SAMPLE_FLOOR})


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
    floor_text = match.group(1)
    if released[:2] > floor[:2]:
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


def plan(
    released_text: str, repo_root: Path, *, samples: bool = False
) -> list[tuple[Path, str, frozenset[str]]]:
    """What this would write, without writing it: ``(path, new text, bounds moved)``.

    Separate from :func:`run` so ``--print-title`` can name what is about to change without
    changing it. ``samples`` switches the file set rather than adding to it: the two are never
    planned together, which is the whole of the separation.
    """
    released = _version(released_text)
    planned: list[tuple[Path, str, frozenset[str]]] = []
    if samples:
        for path in sorted(repo_root.glob("samples/*/agent.py")):
            text = path.read_text("utf-8")
            if _SAMPLE_BASE.search(text) is None:
                continue
            if _SAMPLE_FLOOR.search(text) is None:
                raise SystemExit(
                    f"{path}: depends on maf-sandbox but not as a 'maf-sandbox>=X' floor on its "
                    "own line of the PEP 723 block; this script cannot edit it, and failing "
                    "beats silently skipping it."
                )
            new_text, moved = set_sample_floor(text, released)
            if moved:
                planned.append((path, new_text, moved))
        return planned
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


def run(released_text: str, repo_root: Path, *, samples: bool = False) -> list[Path]:
    """Apply :func:`plan` under ``repo_root``; return the files changed. May exit."""
    changed: list[Path] = []
    for path, new_text, _ in plan(released_text, repo_root, samples=samples):
        path.write_text(new_text, "utf-8")
        changed.append(path)
    return changed


def title(released_text: str, moved: frozenset[str]) -> str:
    """The commit subject for the bounds that moved, or ``""`` when none did.

    `fix:` is load-bearing: `chore:` and `ci:` release nothing, and an unpublished range is
    worth nothing. The samples are `chore:` because nothing under `samples/` is packaged.
    """
    released = _version(released_text)
    admitted = target_ceiling(released)
    line = f"{admitted[0]}.{admitted[1] - 1}"
    if moved == frozenset({SAMPLE_FLOOR}):
        samples = _text(target_sample_floor(released))
        return f"chore: require maf-sandbox {samples} in every sample's declared floor"
    if moved == frozenset({FLOOR, CEILING}):
        return (
            f"fix: require maf-sandbox {released_text} in the dependents, and admit the {line} line"
        )
    if moved == frozenset({CEILING}):
        return f"fix: admit the maf-sandbox {line} line in the dependents' range"
    if moved == frozenset({FLOOR}):
        return f"fix: require maf-sandbox {released_text} in the packages that use it"
    return ""


def main(argv: list[str]) -> int:
    """CLI entry: rewrite the dependents' ranges, or with ``--samples`` the samples' floors."""
    repo_root = Path(__file__).resolve().parent.parent
    rest = [argument for argument in argv[1:] if argument != "--samples"]
    samples = "--samples" in argv[1:]
    if len(rest) == 2 and rest[0] == "--print-title":
        moved: set[str] = set()
        for _, _, bounds in plan(rest[1], repo_root, samples=samples):
            moved |= bounds
        print(title(rest[1], frozenset(moved)))
        return 0
    if len(rest) != 1:
        print(
            f"usage: {argv[0]} [--print-title] [--samples] <released-version>",
            file=sys.stderr,
        )
        return 2
    planned = plan(rest[0], repo_root, samples=samples)
    if not planned:
        subject = "every sample's floor" if samples else "every dependent's range"
        print(f"{subject} already covers this release; nothing to set")
        return 0
    for path, new_text, bounds in planned:
        path.write_text(new_text, "utf-8")
        print(f"  {path.parent.name}: {', '.join(sorted(bounds))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
