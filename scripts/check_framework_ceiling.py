"""Say whether an agent-framework release sits above the ceiling this repository declares.

    python scripts/check_framework_ceiling.py

`check_locked_framework.py` asks whether `uv.lock` holds the newest release the declared ranges
**admit**, and re-resolves inside those ranges to find out. A ceiling below the next minor puts
the answer out of that question's reach: while every package caps below a minor, nothing a
re-resolve can reach goes past the one under it, so the run is green and the release nobody has
adopted is announced by nothing (#1315). This asks the other half — whether the index carries a
release the declared ranges exclude.

**The two reds call for different work**, which is why this is a separate entry point rather
than another branch of that one. *The lock is behind the range* is one `uv lock` command. *The
range is behind the index* is an adoption: a floor raised in every package that declares it, a
release each, and a re-measurement of whatever the new minor moved. Keeping them apart also
keeps that script's contract — `uv` decides what is newest *admitted*, and nothing reaches the
network — which asking what is newest *published* would break in both halves.

**A red has to survive a CDN-cached index.** A version that has just published is visible to one
endpoint minutes before another, so a release is announced only once the simple index *and* that
version's own document both carry it. That costs at most one run — the schedule is monthly and
there is a dispatch — and it buys a red a maintainer can reproduce rather than one that clears
itself. The release held back is still named in the summary, so the run says what it saw.

Pre-releases are not announced: `uv` does not select one for a range that did not ask for it, so
a `1.20.0rc1` above a `<1.20` ceiling is not a release anyone here would resolve. A yanked one is
not announced either, one step further along the same sentence.

The ceilings come from the `pyproject.toml` manifests — the root and every package — which is
where this repository makes its promise to adopters and what `uv.lock` resolves from. The
samples declare the framework unbounded in their PEP 723 blocks, so they bound nothing and are
deliberately not read here.
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
    #: The newest settled release above the ceiling, or None when nothing is above it.
    announced: str | None
    #: Releases above it that only one endpoint carries yet, newest first.
    unsettled: tuple[str, ...]

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
    for the samples, and a bound there holds this repository exactly as a package's does.
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


def settled(distribution: str, released: str) -> bool:
    """Whether a second endpoint agrees ``released`` is published and has not been yanked.

    The simple index is the fresher of the two and is what `uv` resolves from, so it is what
    finds a candidate; the per-version document is what makes the finding reproducible. A
    release only the simple index carries has just published, and the next run announces it.
    """
    payload = fetch_version_document(distribution, released)
    return payload is not None and not payload["info"].get("yanked")


def above(
    distribution: str, ceiling: tuple[int, ...], published: list[str]
) -> tuple[str | None, tuple[str, ...]]:
    """The newest settled release of ``distribution`` above ``ceiling``, and what is unsettled.

    ``published`` is newest-first, so the first release the ceiling *admits* ends the walk:
    everything below it is admitted too, and nothing further down can be above the bound.
    """
    unsettled: list[str] = []
    for released in published:
        if is_prerelease(released):
            continue
        if admits(version(released), ceiling):
            return None, tuple(unsettled)
        if settled(distribution, released):
            return released, tuple(unsettled)
        unsettled.append(released)
    return None, tuple(unsettled)


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
            announced, unsettled = above(distribution, ceiling, published[distribution])
            findings.append(Finding(distribution, ceiling, declared_by, announced, unsettled))
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
        for released in finding.unsettled
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
