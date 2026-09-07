"""Set the dependents' maf-sandbox range after a core release — both bounds, one edit.

    python scripts/set_dependents_range.py <released-version>
    python scripts/set_dependents_range.py --print-title <released-version>
    python scripts/set_dependents_range.py --samples <released-version>

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

**The ceiling** admits the released line and nothing above it: 0.35.0 gives `<0.36`.

It used to reach a minor further, so that the *next* core was admitted before it existed. At
0.x that is a compatibility claim about an unwritten release, and 11 of the first 16 core
minors declared breaking changes — so the claim was false more often than true, and the core
release is what paid for it. Admitting a version and being tested against it are the same
condition: `check_core_against_dependents.py` runs every published dependent whose ceiling
admits the candidate, so a breaking core was refused until each of them republished. Core
0.35.0 stalled that way on 2026-09-07, on two published `maf-sandbox-otel` versions whose fix
was already in the tree.

Admitting only what exists ends that, and it ends the hazard rather than the check: no
consumer can resolve a core beside a dependent published before it, so there is no untested
pairing left for that gate to refuse. The pairing is still proven, at the release that makes
it — `check_dependent_works_with_published_cores.py` runs the dependent's suite against every
published core its new range admits. What this costs is reach: a non-breaking core no longer
arrives for consumers until each dependent widens and republishes.

Both refusals from the scripts this replaces are kept. The ceiling only ever widens, so a
patch changes nothing. The floor is judged against the ceiling **as it was**, not as this run
leaves it — otherwise widening would authorise the very floor bump the old ceiling refused,
and a deliberately narrow ceiling would silently become an adoption.

**The samples' floor moves behind `--samples`**, and never in the same run. It used to ride
this edit, and it cannot: once the core is on the index and the dependents are not,
`check_samples_against_declared_core.py` resolves every block against a core no published
dependent admits, and each sample naming one goes unsatisfiable — which blocks the dependent
releases whose publishing is what would fix it. That took fourteen of fifteen samples down on
0.33.0 and seven live samples on 0.34.0, both times because the samples' hunk was merged with
the packages'. The release workflow passes no `--samples`, so the pull request it opens cannot
carry one; a maintainer runs it once the dependents have published, as a `chore:` of its own,
and `tests/test_sample_metadata.py` reds if that is forgotten for two releases. The flag keeps
the sixteen edits and their refusals in one place rather than in sixteen hand edits.

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

#: A sample's floor: a whole `#`-prefixed line of the PEP 723 block, holding that dependency
#: and nothing else. The rewrites below are `count=1`, and the samples carry paragraphs of
#: prose above their block, so the `#` and the two quotes are what keep a sentence quoting
#: `"maf-sandbox>=X"` from being the match that moves instead of the dependency. The
#: closing quote is a lookahead so the substitution replaces the version and nothing else,
#: and the line anchor makes any other layout unreadable rather than half-read — see
#: `_SAMPLE_BASE`, which is what turns unreadable into a stopped step.
_SAMPLE_FLOOR = re.compile(r'(?m)^(?P<lead>#[ \t]+"maf-sandbox>=)(?P<floor>\d+(?:\.\d+)*)(?=")')
#: Any dependency on the base distribution, anywhere on a `#`-prefixed line. Deliberately
#: looser than the pattern above: the two disagreeing is the signal that a sample declares
#: maf-sandbox in a shape this cannot edit, and that has to stop the run rather than skip it
#: silently. The lookahead is what keeps the sibling `maf-sandbox-acas` from answering for it.
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
    """The bound that admits ``released``'s line and nothing above it.

    One minor up: ``<0.36`` admits every 0.35 patch and excludes 0.36.0, which does not exist
    when this runs. See the module docstring for why it stopped reaching a minor further.
    """
    major, minor = (tuple(released) + (0, 0))[:2]
    return (major, minor + 1)


def target_sample_floor(released: tuple[int, ...]) -> tuple[int, ...]:
    """The floor every sample declares after ``released``: its minor, without its patch.

    Minor-only for two reasons. It is what the samples already spell, and it makes a patch
    release a no-op — a diff rewriting sixteen files to say what they already say costs a
    reviewer real attention and buys a claim nobody made.
    """
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


def plan(
    released_text: str, repo_root: Path, *, samples: bool = False
) -> list[tuple[Path, str, frozenset[str]]]:
    """What this would write, without writing it: ``(path, new text, bounds moved)``.

    Separate from :func:`run` so ``--print-title`` can name what is about to change without
    changing it, and so the workflow never re-derives the rule.

    ``samples`` switches the file set rather than adding to it, and it is the whole of the
    separation the module docstring describes: the release workflow never passes it, so the
    pull request it opens cannot carry a sample. The two sets are never planned together —
    one commit holding both is the shape that took the suite unsatisfiable twice.
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

    `fix:` is required rather than stylistic: `chore:` and `ci:` release nothing here, and
    both halves are only worth anything once *published* — the ceiling because a dependent
    that cannot resolve the new core is one nobody can adopt it through, the floor because a
    floor nobody can install is not a constraint. See RELEASING.md, Release order.

    The samples are the exception. A change is attributed to a package by the files it
    touches, and only `packages/*` is configured, so a samples-only commit cuts no release
    whatever type it carries — `fix:` there would be inert rather than harmful. It says
    `chore:` because that is what AGENTS.md prescribes for a touch outside a package, and
    because `chore:` releases nothing *by type* rather than by the accident of which paths
    happen to be configured today.
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
    """CLI entry: rewrite the dependents' ranges, or with ``--samples`` the samples' floors.

    ``--print-title`` prints the commit subject for what the same arguments would change,
    without changing it. The two file sets are never written by one invocation: the release
    workflow passes no ``--samples``, so the pull request it opens cannot carry one.
    """
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
