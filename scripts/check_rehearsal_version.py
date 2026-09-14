"""Refuse a TestPyPI rehearsal of a version the upload destination already carries.

    python scripts/check_rehearsal_version.py <package> <version>

The dispatch reads the version from the package manifest unless an explicit version is given.
Unreleased source should name its intended release version so the compatibility gates measure
the intended core range. Released source can use the same number on a different upload index.

The workflow points both `UV_INDEX` and `UV_DEFAULT_INDEX` at the upload destination for this
check. A version on PyPI can still be rehearsed on TestPyPI; one already on TestPyPI cannot be
uploaded again. Dependency checks keep their PyPI fallback separately.

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
    """Refuse a rehearsal version already present on the configured upload index."""
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
            f"::error::{distribution} {taken[0]} is already on the upload index. "
            "That index refuses another upload under the same version. Name an unused "
            "version, such as a .postN or .devN, to rehearse again."
        )
        return 1
    newest = f"newest published is {published[0]}" if published else "nothing published yet"
    print(f"{distribution} {candidate} is available for upload ({newest})")
    return 0


def main(argv: list[str]) -> int:
    """CLI entry: refuse a rehearsal version the upload index already carries."""
    if len(argv) != 3:
        print(f"usage: {argv[0]} <package> <version>", file=sys.stderr)
        return 2
    return check(argv[1], argv[2])


if __name__ == "__main__":
    raise SystemExit(run_check(main, sys.argv))
