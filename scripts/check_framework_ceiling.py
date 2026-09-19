"""Say whether an agent-framework release sits above a ceiling this repository declares.

    python scripts/check_framework_ceiling.py

The complement of `check_locked_framework.py`, which asks whether `uv.lock` holds the newest
release the declared ranges **admit** and re-resolves inside them to find out. A ceiling below
the next minor puts a published release out of that question's reach (#1315), and the two reds
want different work: a lockfile refresh there, an adoption here.

Its own entry point because that script's contract is that `uv` decides what is newest admitted
and that nothing reaches the network. This reads the index.
"""

from __future__ import annotations

import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

from check_locked_framework import FRAMEWORK
from check_published_dependents_admit import ceiling_of
from pypi_index import (
    admits,
    epoch,
    fetch_published_versions,
    fetch_version_document,
    is_prerelease,
    run_check,
    version,
)

_ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Finding:
    """One declared ceiling, and what the index turned out to be holding above it."""

    distribution: str
    ceiling: tuple[int, ...]
    declared_by: tuple[str, ...]
    #: The newest announceable release above the ceiling, or None when nothing is above it.
    announced: str | None
    #: Releases above it that only one endpoint carries yet, newest first. A yanked release is
    #: not one of these: its state is settled and it will never become announceable.
    unconfirmed: tuple[str, ...]

    def bound(self) -> str:
        """The ceiling as the `<Y` a manifest writes."""
        return "<" + ".".join(str(part) for part in self.ceiling)


def declared_ceilings(repo_root: Path) -> dict[str, dict[tuple[int, ...], tuple[str, ...]]]:
    """Each framework distribution's declared `<` bounds, with the manifests declaring them.

    Grouped by bound rather than reduced to the lowest, because an adoption staged over two
    minors leaves the packages on different ceilings for a while and a run that reported only
    the lowest would stop naming which packages are still behind.

    `[dependency-groups]` and `[project.optional-dependencies]` are read beside
    `[project.dependencies]`: the root's dev group is where `agent-framework-openai` is declared
    for the samples, and a bound there holds this repository exactly as a package's does. A
    sample's own PEP 723 block is not read — those declare the framework unbounded, so they
    bound nothing.
    """
    found: dict[str, dict[tuple[int, ...], list[str]]] = {}
    manifests = [repo_root / "pyproject.toml", *sorted(repo_root.glob("packages/*/pyproject.toml"))]
    for path in manifests:
        document = tomllib.loads(path.read_text(encoding="utf-8"))
        project = document.get("project", {})
        requirements = list(project.get("dependencies", []))
        for extra in project.get("optional-dependencies", {}).values():
            requirements += [entry for entry in extra if isinstance(entry, str)]
        for group in document.get("dependency-groups", {}).values():
            requirements += [entry for entry in group if isinstance(entry, str)]
        where = path.relative_to(repo_root).as_posix()
        for requirement in requirements:
            for distribution in FRAMEWORK:
                # One requirement at a time: `ceiling_of` answers with the first entry naming
                # the distribution, and a manifest declaring it twice at two bounds would
                # otherwise have the second one silently dropped.
                ceiling = ceiling_of([requirement], distribution)
                if ceiling is None:
                    continue
                declaring = found.setdefault(distribution, {}).setdefault(ceiling, [])
                if where not in declaring:
                    declaring.append(where)
    return {
        distribution: {ceiling: tuple(where) for ceiling, where in bounds.items()}
        for distribution, bounds in found.items()
    }


#: What a second endpoint can say about a release, and the three answers are acted on
#: differently: only PUBLISHED is announceable, only UNCONFIRMED may clear on a re-run.
PUBLISHED, YANKED, UNCONFIRMED = "published", "yanked", "unconfirmed"


def confirmation(distribution: str, released: str) -> str:
    """What ``released``'s own version document says about it.

    The simple index is the fresher of the two endpoints and is what `uv` resolves from, so it
    is what finds a candidate; this is what makes the finding reproducible. A release only the
    simple index carries is UNCONFIRMED — it has just published, and the next run announces it.

    A withdrawn one is YANKED rather than UNCONFIRMED, and the caller must keep them apart: no
    resolver takes a yanked release, so it holds nothing back, and calling it unconfirmed would
    promise a re-run that clears a state which never changes.
    """
    payload = fetch_version_document(distribution, released)
    if payload is None:
        return UNCONFIRMED
    return YANKED if payload["info"].get("yanked") else PUBLISHED


def above(
    distribution: str, ceiling: tuple[int, ...], published: list[str]
) -> tuple[str | None, tuple[str, ...]]:
    """The newest announceable release of ``distribution`` above ``ceiling``, and what is not yet.

    ``published`` is newest-first, so the first release the ceiling *admits* ends the walk:
    everything below it is admitted too, and nothing further down can be above the bound.
    """
    unconfirmed: list[str] = []
    for released in published:
        if is_prerelease(released):
            continue
        # A later epoch is above every ceiling a manifest writes, because PEP 440 compares
        # epoch first and a bound without one is epoch 0. Asked before `admits`, which reads
        # the release segment alone and would place `2!0.1` under `<2` — and since an epoch
        # sorts newest, that one release would end the walk and silence the distribution.
        if epoch(released) == 0 and admits(version(released), ceiling):
            return None, tuple(unconfirmed)
        answer = confirmation(distribution, released)
        if answer == PUBLISHED:
            return released, tuple(unconfirmed)
        if answer == UNCONFIRMED:
            unconfirmed.append(released)
    return None, tuple(unconfirmed)


def assess(
    ceilings: dict[str, dict[tuple[int, ...], tuple[str, ...]]],
    published: dict[str, list[str]],
) -> list[Finding]:
    """Place every declared ceiling against what its distribution publishes.

    A distribution on two ceilings is walked once per ceiling, and the walks overlap only above
    the higher one: everything between the two ends the higher walk at its first `admits`,
    before a document is asked for. So the reads this repeats are the ones a release above
    *every* declared ceiling costs, which is a handful and not worth a cache.
    """
    findings: list[Finding] = []
    for distribution in sorted(ceilings):
        for ceiling, declared_by in sorted(ceilings[distribution].items()):
            announced, unconfirmed = above(distribution, ceiling, published[distribution])
            findings.append(Finding(distribution, ceiling, declared_by, announced, unconfirmed))
    return findings


def report(findings: list[Finding]) -> str:
    """The run summary, whichever way it went."""
    rows = "\n".join(
        f"| `{finding.distribution}` | `{finding.bound()}` | {finding.announced} | "
        f"{', '.join(f'`{where}`' for where in finding.declared_by)} |"
        for finding in findings
        if finding.announced
    )
    notes = "\n".join(
        f"- `{finding.distribution}` {released} is on the simple index but its own version "
        "document is not served yet. A just-published release reaches one endpoint before the "
        "other, so this run holds rather than announcing something a re-run might not find."
        for finding in findings
        for released in finding.unconfirmed
    )
    if not rows:
        # Named rather than summarised: a green run has to say what it placed, or a tree that
        # declared its way out of every ceiling would read exactly like one that is current.
        placed = ", ".join(
            f"`{finding.distribution}` `{finding.bound()}`"
            for finding in sorted(findings, key=lambda f: (f.distribution, f.ceiling))
        )
        current = (
            f"No agent-framework release sits above the ceilings this repository declares: {placed}.\n"
            if placed
            else "This repository declares no agent-framework ceiling, so no release can be out "
            "of the re-resolve's reach — that question covers the whole range on its own.\n"
        )
        return f"{current}\n{notes}\n" if notes else current
    return (
        "**The declared ranges are behind the index.** agent-framework has released above a "
        "ceiling declared here, so nothing a re-resolve can reach will ever mention it — and "
        "Dependabot, which rewrites pins rather than ranges, proposes nothing either.\n"
        "\n"
        "| Distribution | Declared ceiling | Published above it | Declared by |\n"
        "| --- | --- | --- | --- |\n"
        f"{rows}\n"
        "\n"
        "**This is an adoption, not a lockfile refresh.** Widening the ceiling costs a release "
        "in every package that declares it, and whatever the new minor moved has to be "
        "re-measured first — the ceilings above were put there by that measurement. The "
        "tracking issue this run opens is where that work starts; `docs/maintainers.md` "
        "§ *What a red lockfile-drift run means* carries what one costs.\n"
        + (f"\n{notes}\n" if notes else "")
    )


def annotation(findings: list[Finding]) -> str:
    """One line for the checks page, naming the release rather than only its existence."""
    listed = "; ".join(
        f"{finding.distribution} {finding.announced} is above {finding.bound()}"
        for finding in findings
        if finding.announced
    )
    return (
        f"::error::agent-framework has released above a ceiling declared here: {listed}. The "
        "declared ranges exclude it, so the lockfile drift question cannot reach it and "
        "Dependabot proposes nothing. Adopting it is a floor raise and a release across every "
        "package that declares it; the run summary carries the detail."
    )


def main(argv: list[str]) -> int:
    """CLI entry: place every declared ceiling against what the index publishes."""
    if len(argv) != 1:
        print(f"usage: {argv[0]}", file=sys.stderr)
        return 2
    ceilings = declared_ceilings(_ROOT)
    published: dict[str, list[str]] = {}
    for distribution in sorted(ceilings):
        # Refused rather than passed over. A distribution the searched index does not carry
        # leaves every ceiling on it unplaced, and a run that reported nothing found would be
        # green for having measured nothing — which is the one outcome this must not have.
        if not (released := fetch_published_versions(distribution)):
            print(
                f"::error::the index searched carries no {distribution}, so the ceilings this "
                "repository declares for it were placed against nothing. This is not a verdict "
                "on any version — check UV_INDEX and UV_DEFAULT_INDEX.",
                file=sys.stderr,
            )
            return 2
        published[distribution] = released
    findings = assess(ceilings, published)
    print(report(findings))
    if not any(finding.announced for finding in findings):
        return 0
    print(annotation(findings), file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(run_check(main, sys.argv))
