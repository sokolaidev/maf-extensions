"""Refuse a core change that would release a maf-sandbox the dependents cannot admit.

`RELEASING.md` sequences a maf-sandbox minor in four steps, and the first is widening the
dependents' ceilings in an ordinary pull request. Skipping it goes unnoticed, because nothing
turns red: the release publishes, `bump_dependents_floor.py` finds no dependent whose ceiling
admits the new version and changes nothing, and every dependent stays pinned to the previous
minor while the release step still reports success. This is step 1, enforced at the moment it
can still be acted on cheaply.

    git diff --name-only <base> HEAD | python scripts/check_release_order.py "<pull request title>"

Changed paths arrive on stdin, one per line. It refuses only when all three are true: the title
would release a new *minor* of maf-sandbox, the change is attributed to that package by the
files it touches, and some dependent's ceiling excludes the version that would result. A patch,
a title that releases nothing, and a change touching no core file all pass.

One shape it cannot see: `BREAKING CHANGE:` goes in the squash-commit box at merge time, so a
title with no `!` can still cut a minor. `tests/test_release_config.py` catches that later, on
the Release PR itself, where the version has stopped being a prediction.
"""

from __future__ import annotations

import re
import sys
import tomllib
from pathlib import Path

_CORE = "maf-sandbox"
_CORE_DIR = f"packages/{_CORE}/"
#: `type(optional scope)!:` — the leading half of a Conventional Commit subject.
_SUBJECT = re.compile(r"^(?P<type>[a-z]+)(?:\([^)]*\))?(?P<breaking>!)?:")
#: The `<Y` bound of a `maf-sandbox>=X,<Y` constraint.
_CEILING = re.compile(r"maf-sandbox>=\d+(?:\.\d+)*,<(\d+(?:\.\d+)*)")
#: The distribution name at the head of a dependency string, before any version operator.
_DIST_NAME = re.compile(r"[A-Za-z0-9._-]+")
#: Mirrors `changelog-sections` in release-please-config.json: the types that cut a release.
_PATCH_TYPES = frozenset({"fix", "perf", "revert", "docs"})


def _version(text: str) -> tuple[int, ...]:
    return tuple(int(part) for part in text.split("."))


def admits(version: tuple[int, ...], ceiling: tuple[int, ...]) -> bool:
    """Whether ``version`` is below the ``<ceiling`` bound, comparing at equal width."""
    width = max(len(version), len(ceiling))
    padded = version + (0,) * (width - len(version))
    return padded < ceiling + (0,) * (width - len(ceiling))


def next_version(current: tuple[int, ...], title: str) -> tuple[int, ...] | None:
    """The version this title would release for a package it is attributed to, if any.

    `bump-minor-pre-major` is on, so below 1.0.0 a breaking change is a minor like any `feat:`
    — which is the whole reason a ceiling can be crossed by something that is not a `feat:`.
    """
    match = _SUBJECT.match(title.strip())
    if match is None:
        return None
    major, minor, patch = (tuple(current) + (0, 0, 0))[:3]
    if match.group("breaking"):
        return (major + 1, 0, 0) if major else (major, minor + 1, 0)
    if match.group("type") == "feat":
        return (major, minor + 1, 0)
    if match.group("type") in _PATCH_TYPES:
        return (major, minor, patch + 1)
    return None


def touches_core(paths: list[str]) -> bool:
    """Whether any changed path is attributed to maf-sandbox itself.

    Attribution is by directory, the same way release-please does it — and the trailing
    separator matters, or every sibling under `packages/maf-sandbox-*` reads as the core.
    """
    return any(path.replace("\\", "/").startswith(_CORE_DIR) for path in paths)


def ceilings(repo_root: Path) -> dict[str, tuple[int, ...]]:
    """Each dependent's maf-sandbox ceiling, keyed by package directory name."""
    found: dict[str, tuple[int, ...]] = {}
    for path in sorted(repo_root.glob("packages/*/pyproject.toml")):
        project = tomllib.loads(path.read_text("utf-8")).get("project", {})
        if project.get("name") == _CORE:
            continue
        for dep in project.get("dependencies", []):
            name = _DIST_NAME.match(dep.strip())
            if name is None or name.group(0) != _CORE:
                continue
            bound = _CEILING.search(dep)
            if bound is not None:
                found[path.parent.name] = _version(bound.group(1))
    return found


def core_version(repo_root: Path) -> tuple[int, ...]:
    text = (repo_root / "packages" / _CORE / "pyproject.toml").read_text("utf-8")
    return _version(tomllib.loads(text)["project"]["version"])


def assess(title: str, paths: list[str], repo_root: Path) -> list[str]:
    """The reasons this change may not merge yet, or an empty list."""
    if not touches_core(paths):
        return []
    current = core_version(repo_root)
    proposed = next_version(current, title)
    if proposed is None:
        return []
    excluded = sorted(
        package
        for package, ceiling in ceilings(repo_root).items()
        if not admits(proposed, ceiling)
    )
    if not excluded:
        return []
    shown = ".".join(str(part) for part in proposed)
    return [
        f"this title releases maf-sandbox {shown}, which these still exclude: "
        f"{', '.join(excluded)}",
        "widen their ceilings in a separate pull request first: RELEASING.md, step 1 of a "
        "maf-sandbox release. Merging ahead of it publishes a version no dependent can adopt, "
        "and the post-release floor bump then has nothing to do rather than failing.",
    ]


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(f"usage: {argv[0]} <pull-request-title>", file=sys.stderr)
        return 2
    paths = [line.strip() for line in sys.stdin.read().splitlines() if line.strip()]
    problems = assess(argv[1], paths, Path(__file__).resolve().parent.parent)
    for problem in problems:
        print(problem, file=sys.stderr)
    if problems:
        return 1
    print("release order: nothing to widen before this merges")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
