"""Refuse a TestPyPI rehearsal of a version an index already carries.

    python scripts/check_rehearsal_version.py <package> <version>

`publish-packages.yml`'s dispatch reads the version out of `packages/<package>/pyproject.toml`
unless it is given one. While a release pull request is pending that number is the last released
one, so the run builds the accumulated unreleased source and labels it with a version that no
longer describes it — and `check_core_against_dependents.py` then selects every published
dependent whose ceiling admits it, which at a released number is all of them. The dispatch takes
a version so a rehearsal can say what it is rehearsing, including a `Release-As:` version, which
nothing in the tree carries until release-please has opened the pull request (#1120).

This refuses a candidate an index this run resolves from already carries — uv's own `UV_INDEX`
and `UV_DEFAULT_INDEX`, which for a rehearsal are TestPyPI and PyPI. One refusal covers both:
a version PyPI carries is a release, and a version TestPyPI carries cannot be uploaded again,
which `Publish` would otherwise discover after the whole gate has run.

It also refuses a version that is not spelled canonically. The workflow stamps the manifest with
`uv version`, which normalises what it is given — `v1.2.3` is written as `1.2.3` — so anything
else reaches the artifacts under a name the run's own filename assertions do not expect.
"""

from __future__ import annotations

import re
import sys
import tomllib
from pathlib import Path

from pypi_index import fetch_published_versions, run_check, sort_key, version

#: PEP 440's canonical spelling — what `uv version` writes, and what `uv build` names files with.
_CANONICAL = re.compile(r"^\d+(?:\.\d+)*(?:(?:a|b|rc)\d+)?(?:\.post\d+)?(?:\.dev\d+)?$")

_ROOT = Path(__file__).resolve().parent.parent


def manifest(package: str) -> Path:
    """The manifest of the package directory a dispatch names."""
    return _ROOT / "packages" / package / "pyproject.toml"


def identity(text: str) -> tuple[object, ...]:
    """A version's identity for equality, under which `0.38` and `0.38.0` are the same version.

    PEP 440 reads an absent release component as zero, so an index carrying `0.38.0` refuses an
    upload named `0.38`. Trailing zeros are dropped so both spellings answer the same key.
    """
    key = sort_key(text)
    trimmed = list(version(text))
    while len(trimmed) > 1 and trimmed[-1] == 0:
        trimmed.pop()
    return (key[0], tuple(trimmed), *key[2:])


def uncanonical(candidate: str) -> str | None:
    """Why `candidate` cannot be built under, or None if it can."""
    if _CANONICAL.match(candidate):
        return None
    return (
        f"{candidate!r} is not a canonical PEP 440 version, and the artifacts would be named "
        "something else — write it as 0.38.0, 0.38.0rc1, 0.38.0.post1 or 0.38.0.dev1"
    )


def check(package: str, candidate: str) -> int:
    """Refuse a rehearsal version that an index this run resolves from already carries."""
    reason = uncanonical(candidate)
    if reason:
        print(f"::error::{reason}")
        return 1
    distribution = tomllib.loads(manifest(package).read_text("utf-8"))["project"]["name"]
    published = fetch_published_versions(distribution) or []
    wanted = identity(candidate)
    taken = [release for release in published if identity(release) == wanted]
    if taken:
        print(
            f"::error::{distribution} {taken[0]} is already on an index this run resolves from. "
            "Rehearsing a number that is taken builds this ref's source under it, and the upload "
            "would be refused. Name the version this release will carry, or a .postN or .devN "
            "past the taken one to rehearse the same number again."
        )
        return 1
    newest = f"newest published is {published[0]}" if published else "nothing published yet"
    print(f"{distribution} {candidate} is unpublished ({newest})")
    return 0


def main(argv: list[str]) -> int:
    """CLI entry: refuse a rehearsal version an index already carries."""
    if len(argv) != 3:
        print(f"usage: {argv[0]} <package> <version>", file=sys.stderr)
        return 2
    return check(argv[1], argv[2])


if __name__ == "__main__":
    raise SystemExit(run_check(main, sys.argv))
