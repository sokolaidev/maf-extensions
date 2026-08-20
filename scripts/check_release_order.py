"""Refuse a maf-sandbox change that would release a version the dependents cannot admit.

    git diff --name-only <base> HEAD | python scripts/check_release_order.py "<pull request title>"

Changed paths arrive on stdin, one per line. It refuses only when the title would cut a new
*minor* of maf-sandbox, the change is attributed to that package by the files it touches, and
some dependent's ceiling excludes the result — step 1 of a maf-sandbox release in RELEASING.md.

One shape it cannot see: `BREAKING CHANGE:` goes in the squash-commit box at merge time, so a
title with no `!` can still cut a minor. `tests/test_check_release_order.py` catches that on
the Release PR, where the version has stopped being a prediction.
"""

from __future__ import annotations

import json
import re
import sys
import tomllib
import urllib.error
import urllib.request
from pathlib import Path

_CORE = "maf-sandbox"
_CORE_DIR = f"packages/{_CORE}/"
_SUBJECT = re.compile(r"^(?P<type>[a-z]+)(?:\([^)]*\))?(?P<breaking>!)?:")
_CEILING = re.compile(r"maf-sandbox>=\d+(?:\.\d+)*,<(\d+(?:\.\d+)*)")
_DIST_NAME = re.compile(r"[A-Za-z0-9._-]+")
#: Mirrors `changelog-sections` in release-please-config.json: the types that cut a release.
_PATCH_TYPES = frozenset({"fix", "perf", "revert", "docs"})

_SIMPLE_ACCEPT = "application/vnd.pypi.simple.v1+json"
_TIMEOUT_SECONDS = 30


def version(text: str) -> tuple[int, ...]:
    """The dotted release as a tuple of ints."""
    return tuple(int(part) for part in text.split("."))


def admits(version: tuple[int, ...], ceiling: tuple[int, ...]) -> bool:
    """Whether ``version`` is below the ``<ceiling`` bound, comparing at equal width."""
    width = max(len(version), len(ceiling))
    padded = version + (0,) * (width - len(version))
    return padded < ceiling + (0,) * (width - len(ceiling))


def fetch_published_versions(distribution: str) -> list[str] | None:
    """The published versions of ``distribution``, newest-first, or None if never released.

    Versions come from PyPI's PEP 691 simple index, which is fresher than the CDN-cached
    top-level JSON document. The ``versions`` array is standardized (PEP 700) but its order
    carries no meaning, so it is sorted here; it names versions only, and whether a release
    was yanked or what its ``requires_dist`` excludes lives in the per-version document,
    which a caller that cares must fetch. A 404 means the distribution was never released;
    any other HTTP error is fatal.
    """
    url = f"https://pypi.org/simple/{distribution}/"
    request = urllib.request.Request(url, headers={"Accept": _SIMPLE_ACCEPT})
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT_SECONDS) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return None
        raise
    return sorted(payload["versions"], key=version, reverse=True)


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
                found[path.parent.name] = version(bound.group(1))
    return found


def core_version(repo_root: Path) -> tuple[int, ...]:
    """The current ``maf-sandbox`` version, read from its ``pyproject.toml``, as a tuple of ints."""
    text = (repo_root / "packages" / _CORE / "pyproject.toml").read_text("utf-8")
    return version(tomllib.loads(text)["project"]["version"])


def assess(title: str, paths: list[str], repo_root: Path) -> list[str]:
    """The reasons this change may not merge yet, or an empty list."""
    if not touches_core(paths):
        return []
    current = core_version(repo_root)
    proposed = next_version(current, title)
    if proposed is None:
        return []
    excluded = sorted(
        package for package, ceiling in ceilings(repo_root).items() if not admits(proposed, ceiling)
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
    """CLI entry: read changed paths from stdin and the PR title from argv, run ``assess``, and print any ordering problems."""
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
