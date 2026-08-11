"""Refuse to publish a maf-sandbox the already-published dependents would exclude.

    python scripts/check_published_dependents_admit.py <version>

Reads each dependent's `requires_dist` from PyPI and refuses if its ceiling excludes the
version about to go out. That state is the publication window: PyPI metadata is immutable, so
a widened ceiling only reaches a user when that dependent is *published*, and until then the
index does not resolve as a set. RELEASING.md orders the releases so this never happens; this
is what makes the order enforced rather than remembered.

A dependent that is not on PyPI yet is skipped — a first release has nothing to contradict.
A network failure is fatal rather than skipped: passing because PyPI could not be reached is
the one outcome that would make this check worthless.

Constraints are parsed order-independently. PyPI normalises `maf-sandbox>=0.6.0,<0.7` to
`maf-sandbox<0.7,>=0.6.0`, and a parser written against the shape the tree uses finds nothing
in the shape the index returns — passing every time, for the wrong reason.
"""

from __future__ import annotations

import json
import re
import sys
import tomllib
import urllib.error
import urllib.request
from pathlib import Path

from check_release_order import admits, version

_CORE = "maf-sandbox"
#: A distribution name and its optional extras, at the head of a requirement.
_LEADING_NAME = re.compile(r"^[A-Za-z0-9._-]+(?:\[[^\]]*\])?")
_TIMEOUT_SECONDS = 30


def _requirement_name(requirement: str) -> str:
    """The distribution a `requires_dist` entry constrains, without extras or markers."""
    head = requirement.split(";", 1)[0].strip()
    for index, char in enumerate(head):
        if char in "<>=!~[ (":
            return head[:index].strip()
    return head


def ceiling_of(requires_dist: list[str]) -> tuple[int, ...] | None:
    """The `<Y` bound the entry for maf-sandbox declares, or None if it names no upper bound.

    Order-independent by necessity, and `<=` is deliberately not a match: it bounds inclusively
    and means something this function would misreport.
    """
    for requirement in requires_dist:
        if _requirement_name(requirement) != _CORE:
            continue
        # The name is glued to the first clause, so strip it before splitting: `maf-sandbox<0.7`
        # does not start with `<`, and a parser that forgets this finds no ceiling and passes.
        head = requirement.split(";", 1)[0].strip()
        spec = _LEADING_NAME.sub("", head, count=1)
        for clause in spec.split(","):
            clause = clause.strip()
            if clause.startswith("<") and not clause.startswith("<="):
                return version(clause[1:].strip())
    return None


def dependent_distributions(repo_root: Path) -> list[str]:
    """Every package in this repository that depends on maf-sandbox, by distribution name."""
    found: list[str] = []
    for path in sorted(repo_root.glob("packages/*/pyproject.toml")):
        project = tomllib.loads(path.read_text("utf-8")).get("project", {})
        name = project.get("name")
        if not name or name == _CORE:
            continue
        if any(_requirement_name(d) == _CORE for d in project.get("dependencies", [])):
            found.append(name)
    return found


def refusals(
    published: dict[str, list[str] | None], released: tuple[int, ...]
) -> list[str]:
    """One line per published dependent whose ceiling excludes `released`."""
    shown = ".".join(str(part) for part in released)
    out: list[str] = []
    for distribution, requires_dist in sorted(published.items()):
        if requires_dist is None:
            continue
        ceiling = ceiling_of(requires_dist)
        if ceiling is not None and not admits(released, ceiling):
            bound = ".".join(str(part) for part in ceiling)
            out.append(
                f"{distribution} on PyPI declares maf-sandbox<{bound}, excluding {shown}"
            )
    return out


def fetch_requires_dist(distribution: str) -> list[str] | None:
    """The published `requires_dist`, or None if the distribution has never been released."""
    url = f"https://pypi.org/pypi/{distribution}/json"
    try:
        with urllib.request.urlopen(url, timeout=_TIMEOUT_SECONDS) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise
    return list(payload["info"].get("requires_dist") or [])


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(f"usage: {argv[0]} <version>", file=sys.stderr)
        return 2
    released = version(argv[1])
    repo_root = Path(__file__).resolve().parent.parent
    published = {
        distribution: fetch_requires_dist(distribution)
        for distribution in dependent_distributions(repo_root)
    }
    problems = refusals(published, released)
    if not problems:
        print(f"every published dependent admits maf-sandbox {argv[1]}")
        return 0
    for problem in problems:
        print(problem, file=sys.stderr)
    print(
        "\nPublishing now leaves the index unresolvable as a set until every one of those "
        "ships again. Release the dependents with the widened ceiling first: RELEASING.md, "
        "Release order.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
