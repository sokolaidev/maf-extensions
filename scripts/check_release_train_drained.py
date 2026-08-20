"""Say whether every published dependent has been built against the published core.

    python scripts/check_release_train_drained.py

Prints `train=drained` or `train=draining`, and one line per dependent still built against an
older core. It decides nothing: a live check that goes red while the train is draining is red
about the order of the release train rather than about the code, and that is a thing to say in
the run summary, not a reason to suppress the check. Gating the dispatch on a coarser signal is
what #337 removed, and #512 is why this is an annotation instead.

The signal is each published dependent's declared *floor* on maf-sandbox, not its ceiling. A
ceiling says which cores a wheel tolerates; a floor says which one it was built against, and
that is the question during a drain — every dependent admitted `maf-sandbox` 0.18.0 while none
of them had been rebuilt for it.

Compared at the minor, because a core patch strands nothing: a dependent declaring `>=0.18.0`
is caught up with a published 0.18.1. A dependent that declares no floor at all is unjudgeable
here and is left out rather than guessed at.
"""

from __future__ import annotations

import sys
from pathlib import Path

from check_published_dependents_admit import (
    dependent_distributions,
    fetch_requires_dist,
    floor_of,
)
from check_release_order import fetch_published_versions, version

_CORE = "maf-sandbox"


def shown(release: tuple[int, ...]) -> str:
    """A release tuple as its dotted string."""
    return ".".join(str(part) for part in release)


def behind(published: dict[str, list[str] | None], core: tuple[int, ...]) -> list[str]:
    """One line per published dependent whose floor predates the core's minor."""
    out: list[str] = []
    for distribution, requires_dist in sorted(published.items()):
        if requires_dist is None:
            continue
        floor = floor_of(requires_dist)
        if floor is None:
            continue
        if floor[:2] < core[:2]:
            out.append(
                f"{distribution} on PyPI declares maf-sandbox>={shown(floor)}, "
                f"which predates the published core {shown(core)}"
            )
    return out


def main(argv: list[str]) -> int:
    """CLI entry: read the published core and every dependent's floor, and print the verdict."""
    if len(argv) != 1:
        print(f"usage: {argv[0]}", file=sys.stderr)
        return 2
    releases = fetch_published_versions(_CORE)
    if not releases:
        print(f"{_CORE} has never been published, so there is no train to drain", file=sys.stderr)
        return 2
    core = version(releases[0])
    repo_root = Path(__file__).resolve().parent.parent
    published = {
        distribution: fetch_requires_dist(distribution)
        for distribution in dependent_distributions(repo_root)
    }
    lagging = behind(published, core)
    if not lagging:
        print("train=drained")
        print(f"every published dependent is built against maf-sandbox {shown(core)}")
        return 0
    print("train=draining")
    for line in lagging:
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
