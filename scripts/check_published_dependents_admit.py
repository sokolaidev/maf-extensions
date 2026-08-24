"""Say which published dependents exclude the maf-sandbox about to go out.

    python scripts/check_published_dependents_admit.py <version>

Resolves each dependent's newest version from PyPI's simple index, reads its `requires_dist`
from the per-version document, and reports every ceiling that excludes the version about to
be released.

**It used to refuse that, and no longer does** — the same inversion #633 made to
`check_release_order.py`, which is this check at pull-request time rather than at publish.
Refusing here is what ordered every core release behind its dependents': a core the ceilings
exclude could not go out until all five had shipped again, whether or not anything was
actually broken. A dependent whose ceiling excludes the candidate *cannot reach it*, which for
a breaking release is the point rather than the problem.

What refuses on evidence is `check_core_against_dependents.py`, which runs every admitting
published dependent's own suite against the candidate. A ceiling that excludes it takes that
dependent out of reach; an unbounded ceiling is treated as at-risk and tested. Either way the
question is answered by running something rather than by reading a bound.

The consequence is still worth printing, because it is not obvious: nothing published resolves
the new core until each dependent widens and republishes, so the live samples exercise the core
below it and the dispatch reports a skip. See `docs/release-compatibility.md`.

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

from check_release_order import admits, fetch_published_versions, version

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


def exclusions(published: dict[str, list[str] | None], released: tuple[int, ...]) -> list[str]:
    """One line per published dependent whose ceiling excludes `released`."""
    shown = ".".join(str(part) for part in released)
    out: list[str] = []
    for distribution, requires_dist in sorted(published.items()):
        if requires_dist is None:
            continue
        ceiling = ceiling_of(requires_dist)
        if ceiling is not None and not admits(released, ceiling):
            bound = ".".join(str(part) for part in ceiling)
            out.append(f"{distribution} on PyPI declares maf-sandbox<{bound}, excluding {shown}")
    return out


def _requires_dist_for_version(distribution: str, version_str: str) -> list[str] | None:
    """One version's ``requires_dist``, or None if that version is gone or yanked."""
    url = f"https://pypi.org/pypi/{distribution}/{version_str}/json"
    try:
        with urllib.request.urlopen(url, timeout=_TIMEOUT_SECONDS) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise
    info = payload["info"]
    if info.get("yanked"):
        return None
    return list(info.get("requires_dist") or [])


def fetch_requires_dist(distribution: str) -> list[str] | None:
    """The ``requires_dist`` of the newest non-yanked published version, or None if never released.

    The version list comes from the PEP 691 simple index and each candidate's requirements come
    from its own per-version document. The newest yanked release is skipped: no unpinned
    resolution selects it, so its ceiling is not the one a new release must satisfy — the next
    non-yanked version's is. A per-version 404 (an empty or pulled release) is skipped the same
    way.
    """
    versions = fetch_published_versions(distribution)
    if versions is None:
        return None
    for version_str in versions:
        requires = _requires_dist_for_version(distribution, version_str)
        if requires is not None:
            return requires
    return None


def main(argv: list[str]) -> int:
    """CLI entry: report which published dependents exclude the candidate. Always zero.

    A ceiling that excludes the candidate is a statement about reach, not a fault: it puts that
    dependent beyond the release rather than breaking it. What refuses is the gate that runs the
    dependents' suites against the candidate.
    """
    if len(argv) != 2:
        print(f"usage: {argv[0]} <version>", file=sys.stderr)
        return 2
    released = version(argv[1])
    repo_root = Path(__file__).resolve().parent.parent
    published = {
        distribution: fetch_requires_dist(distribution)
        for distribution in dependent_distributions(repo_root)
    }
    excluded = exclusions(published, released)
    if not excluded:
        print(f"every published dependent admits maf-sandbox {argv[1]}")
        return 0
    for line in excluded:
        print(line)
    print(
        "\nNothing already published reaches this version, which for a breaking release is the "
        "point: each dependent adopts when its own ceiling widens and it republishes. What "
        "follows meanwhile — the live samples resolve their dependents from PyPI, so they "
        "exercise the core below this one and the dispatch reports a skip; and a caller pinning "
        "this version beside a dependent gets an unsatisfiable set, where an unpinned install "
        "resolves to the core below it and works. To make it reachable now, publish the "
        "dependents' widened ceilings first: RELEASING.md, Release order."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
