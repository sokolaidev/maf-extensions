"""Say whether every published dependent has been published since the core.

    python scripts/check_release_train_drained.py

Prints `train=drained` or `train=draining`, and one line per dependent that has not gone out
since the newest core. It decides nothing and gates nothing: a live check that goes red while
the train is draining is red about the order of the release train rather than about the code,
and that belongs in the run summary (#337, #512).

The signal is publication time, which is the criterion #512 names — a dependent's newest upload
against the core's. A dependency *floor* answers a different question and cannot stand in for
this one: `RELEASING.md` raises a floor only when a dependent's code needs the new version, so a
dependent may keep an older minimum forever by design, and a detector reading floors would call
that a drain that never ends.

A distribution PyPI reports no upload time for is unjudgeable here and is left out rather than
guessed at.
"""

from __future__ import annotations

import sys
from pathlib import Path

from check_published_dependents_admit import dependent_distributions
from pypi_index import fetch_simple, newest_upload, version

_CORE = "maf-sandbox"


def shown(release: tuple[int, ...]) -> str:
    """A release tuple as its dotted string."""
    return ".".join(str(part) for part in release)


def behind(uploads: dict[str, str | None], core_upload: str, core: tuple[int, ...]) -> list[str]:
    """One line per published dependent whose newest upload predates the core's.

    ISO 8601 UTC stamps from one index, so a string comparison is a chronological one.
    """
    out: list[str] = []
    for distribution, uploaded in sorted(uploads.items()):
        if uploaded is None or uploaded >= core_upload:
            continue
        out.append(
            f"{distribution} on PyPI was last published at {uploaded}, "
            f"before maf-sandbox {shown(core)} at {core_upload}"
        )
    return out


def main(argv: list[str]) -> int:
    """CLI entry: read the published core and every dependent's floor, and print the verdict."""
    if len(argv) != 1:
        print(f"usage: {argv[0]}", file=sys.stderr)
        return 2
    core_payload = fetch_simple(_CORE)
    if core_payload is None or not core_payload["versions"]:
        print(f"{_CORE} has never been published, so there is no train to drain", file=sys.stderr)
        return 2
    core = version(sorted(core_payload["versions"], key=version, reverse=True)[0])
    core_upload = newest_upload(core_payload)
    if core_upload is None:
        print(f"{_CORE} reports no upload time, so the train cannot be ordered", file=sys.stderr)
        return 2
    repo_root = Path(__file__).resolve().parent.parent
    uploads: dict[str, str | None] = {}
    for distribution in dependent_distributions(repo_root):
        payload = fetch_simple(distribution)
        uploads[distribution] = None if payload is None else newest_upload(payload)
    lagging = behind(uploads, core_upload, core)
    if not lagging:
        print("train=drained")
        print(f"every published dependent went out after maf-sandbox {shown(core)}")
        return 0
    print("train=draining")
    for line in lagging:
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
