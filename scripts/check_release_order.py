"""Say what follows when a maf-sandbox release lands outside the dependents' ceilings.

    git diff --name-only <base>...HEAD | python scripts/check_release_order.py "<pull request title>"

Changed paths arrive on stdin, one per line. Three dots, so the comparison starts at the merge base: against a base that has moved on, two dots hands this every commit the base gained since and reports another pull request's release as this one's. It speaks only when the title would cut a new
*minor* of maf-sandbox, the change is attributed to that package by the files it touches, and
some dependent's ceiling excludes the result.

**It exits non-zero when it has something to say, and that is not a verdict on the
release.** A version outside those ceilings is permitted, and for a breaking release it is the
point: a break nothing already installed can reach is a break that hurts nobody, and each
dependent adopts on its own schedule. What checks whether the release is *sound* is
`check_core_against_dependents.py`, which runs every admitting published dependent's own suite
against the candidate core at the moment of release — not this.

What this refuses to do is let the consequence go unread. Between #628 and #657 it reported and
exited zero, which put the notice in the log of a green job; a notice nobody opens is one that
is not delivered. Failing is the only channel a pull request cannot ignore. **Merging over it
is the expected move for a deliberate breaking release** — the maintainer's override says the
consequence was read and accepted, which is the whole thing this asks for.
See `docs/release-compatibility.md`.

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


def fetch_simple(distribution: str) -> dict | None:
    """``distribution``'s PEP 691 simple document, or None if it was never released.

    The simple index is fresher than the CDN-cached top-level JSON document, and it is what
    ``uv`` resolves from. A 404 means never released; any other HTTP error is fatal.
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
    return payload


def fetch_published_versions(distribution: str) -> list[str] | None:
    """The published versions of ``distribution``, newest-first, or None if never released.

    The ``versions`` array is standardized (PEP 700) but its order carries no meaning, so it is
    sorted here; it names versions only, and whether a release was yanked or what its
    ``requires_dist`` excludes lives in the per-version document, which a caller that cares
    must fetch.
    """
    payload = fetch_simple(distribution)
    if payload is None:
        return None
    return sorted(payload["versions"], key=version, reverse=True)


def newest_upload(payload: dict) -> str | None:
    """The most recent ``upload-time`` across a simple document's files, or None if it has none.

    PEP 700 made the field mandatory for new uploads and optional for old ones, so a
    distribution whose files all predate it answers None rather than a wrong minimum.
    """
    stamps = [file["upload-time"] for file in payload["files"] if file.get("upload-time")]
    return max(stamps) if stamps else None


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


def consequences(title: str, paths: list[str], repo_root: Path) -> list[str]:
    """What follows from this title's version against the dependents' ceilings, or nothing."""
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
        "that is allowed, and for a breaking release it is the point — a version nothing "
        "already installed can reach is one that breaks nobody. Two things follow either way: "
        "no published dependent resolves it until its own ceiling widens and it republishes, "
        "and the live samples resolve their dependents from PyPI, so they will exercise the "
        "core below this one and the dispatch will say it skipped. If you meant it to be "
        "reachable, take the widening offer the release opens.",
    ]


def main(argv: list[str]) -> int:
    """CLI entry: read changed paths from stdin and the title from argv, and print what follows.

    Non-zero when there is a consequence to read, zero when there is none. Not a verdict on the
    release — the gates that measure decide that — but the only channel a pull request cannot
    scroll past. A deliberate breaking release is merged over this, deliberately.
    """
    if len(argv) != 2:
        print(f"usage: {argv[0]} <pull-request-title>", file=sys.stderr)
        return 2
    paths = [line.strip() for line in sys.stdin.read().splitlines() if line.strip()]
    notices = consequences(argv[1], paths, Path(__file__).resolve().parent.parent)
    for notice in notices:
        print(notice)
    if not notices:
        print("release order: every dependent's ceiling already admits what this would release")
        return 0
    print(
        "merge over this once the consequence above is read and accepted — that is what a "
        "deliberate breaking release looks like here."
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
