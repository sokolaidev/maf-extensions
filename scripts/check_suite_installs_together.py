"""Prove the suite still forms a set somebody can install, and say which set that is.

    python scripts/check_suite_installs_together.py [--dist-dir dist] [--whole-set] [--local-core]

``--local-core`` forces the core to the wheel in ``--dist-dir`` rather than letting the range
decide. A dependent whose floor waits on the release this branch cuts cannot resolve otherwise,
and the question here is whether the *set* combines, not whether a version number exists yet.
It is passed on a pull request and never before an upload.

The dependents are installed together, not one at a time: `samples/03` takes
`maf-sandbox-acas` and `maf-sandbox-codeact` beside the core, `samples/11` takes
`maf-sandbox-bicep` and `maf-sandbox-docker`. So a dependent sitting on an older core line is
fine on its own, and two dependents whose constraints stop overlapping are not — nothing can
put them in one environment.

Intersecting the ranges by hand would answer a narrower question than the one that matters.
These packages also carry `agent-framework-core` and the Azure libraries, and two of them can
become mutually uninstallable over those while their core ranges overlap perfectly. So the
resolver is asked instead, and asked to *install* rather than merely resolve: resolution is
metadata, installation is artifacts, and a set can resolve cleanly and still fail on a wheel
that is not there.

Two modes, because a release has two shapes:

- **per-candidate** (the default) — this checkout's wheel for one dependent, beside whatever
  the resolver picks for the others from the index. **The candidate's own combination is a
  warning.** The wheel is pinned here and nobody else pins it, so a combination this cannot
  find is one the resolver reaches by taking an older sibling. Publishing adds a version to the
  index; it cannot remove a combination that already resolved. What it does tell you is that
  the newest of everything does not combine yet, which is worth seeing and is not worth
  stopping a release for. **What does fail is the published family having no resolvable set at
  all** — not a claim about the candidate, which cannot repair it and cannot have caused it,
  but about the index, where two published dependents now exclude each other. The report names
  that pair, because a release blocked by it is blocked until one of the two publishes again.
- **whole-set** (`--whole-set`) — every wheel in the directory together, no published
  fallbacks. **This one fails**: a release whose own packages cannot share an environment is a
  range that moved past a sibling, and no later publish repairs it.

Both **report the set that resolved**, and the per-candidate warning additionally resolves the
family unpinned to show which versions do work together. "Latest of everything" and "had to go
back four versions" are both installable, and the difference is the drift worth seeing before
it becomes a support question.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from check_dependent_works_with_published_cores import declared_range
from check_published_dependents_admit import (
    ceiling_of,
    dependent_distributions,
    fetch_requires_dist,
)
from check_published_dependents_work import fetch_requires_dist_for_version
from pypi_index import admits, fetch_published_versions, run_check, version

#: uv paints its diagnostics, and the colour survives a pipe into a log or a job summary.
_ANSI = re.compile(r"\x1b\[[0-9;]*m")

_CORE = "maf-sandbox"

#: The core's own entry, never a dependent's: `maf-sandbox<0.23` matches, `maf-sandbox-acas` does not.
_CORE_ENTRY = re.compile(rf"{re.escape(_CORE)}(?![A-Za-z0-9._-])")
_ROOT = Path(__file__).resolve().parent.parent


def _python_in(environment: Path) -> Path:
    """The interpreter a `uv venv` at ``environment`` created."""
    windows = sys.platform == "win32"
    return environment / ("Scripts" if windows else "bin") / ("python.exe" if windows else "python")


def wheels_in(dist_dir: Path) -> dict[str, Path]:
    """Every `maf-sandbox*` wheel in ``dist_dir`` by distribution name, refusing duplicates."""
    found: dict[str, list[Path]] = {}
    for wheel in sorted(dist_dir.glob("maf_sandbox*.whl")):
        found.setdefault(wheel.name.split("-", 1)[0].replace("_", "-"), []).append(wheel)
    if ambiguous := {name: paths for name, paths in found.items() if len(paths) > 1}:
        listed = "; ".join(f"{n}: {', '.join(p.name for p in ps)}" for n, ps in ambiguous.items())
        raise SystemExit(f"more than one wheel for a distribution in {dist_dir} — {listed}")
    return {name: paths[0] for name, paths in found.items()}


def local_core(wheels: dict[str, Path]) -> Path | None:
    """The core wheel among ``wheels``, when one was built."""
    return wheels.get(_CORE)


def install(requirements: list[str], core: Path | None = None) -> tuple[bool, dict[str, str], str]:
    """Install ``requirements`` into a throwaway environment; return what landed in it."""
    with tempfile.TemporaryDirectory() as directory:
        environment = Path(directory) / "venv"
        created = subprocess.run(
            ["uv", "venv", str(environment)], capture_output=True, text=True, check=False
        )
        if created.returncode != 0:
            return False, {}, created.stderr.strip()
        python = _python_in(environment)
        pinned: list[str] = []
        if core is not None:
            # The core in `dist/` still carries its pre-bump version, so a dependent floored on
            # the release this branch cuts cannot resolve against it. Forced rather than
            # resolved: the question is whether the set combines, not whether that number is
            # on the index yet.
            override = Path(directory) / "override.txt"
            override.write_text(f"{_CORE} @ {core.as_uri()}\n", encoding="utf-8")
            pinned = ["--overrides", str(override)]
        installed = subprocess.run(
            ["uv", "pip", "install", "--python", str(python), *pinned, *requirements],
            capture_output=True,
            text=True,
            check=False,
        )
        if installed.returncode != 0:
            return False, {}, _ANSI.sub("", installed.stderr).strip()
        listed = subprocess.run(
            ["uv", "pip", "list", "--python", str(python), "--format", "json"],
            capture_output=True,
            text=True,
            check=False,
        )
        if listed.returncode != 0:
            return True, {}, ""
        resolved = {
            entry["name"]: entry["version"]
            for entry in json.loads(listed.stdout)
            if entry["name"].startswith(_CORE)
        }
        return True, resolved, ""


def latest_published() -> dict[str, str]:
    """The newest published version of the core and each dependent, for the drift report."""
    newest: dict[str, str] = {}
    for distribution in [_CORE, *dependent_distributions(_ROOT)]:
        published = fetch_published_versions(distribution)
        if published:
            newest[distribution] = sorted(published, key=version)[-1]
    return newest


def report(resolved: dict[str, str], newest: dict[str, str], indent: str = "    ") -> list[str]:
    """The resolved set, marking anything the resolver had to go back for."""
    lines = []
    for name in sorted(resolved):
        behind = (
            "  <- not the newest published"
            if name in newest and version(resolved[name]) < version(newest[name])
            else ""
        )
        lines.append(f"{indent}{name:<22} {resolved[name]}{behind}")
    return lines


def _dotted(parts: tuple[int, ...]) -> str:
    return ".".join(str(part) for part in parts)


def _core_requirement(requires_dist: list[str]) -> str | None:
    """The entry constraining the core, verbatim, so a report can name the real constraint."""
    for requirement in requires_dist:
        head = requirement.split(";", 1)[0].strip()
        if _CORE_ENTRY.match(head):
            return head
    return None


def published_core_ranges() -> dict[str, str]:
    """What each dependent's newest published version requires of the core."""
    ranges: dict[str, str] = {}
    for distribution in dependent_distributions(_ROOT):
        requires = fetch_requires_dist(distribution)
        if requires is None:
            continue
        if requirement := _core_requirement(requires):
            ranges[distribution] = requirement
    return ranges


#: A range as this module compares them: the `>=` floor, and the `<` ceiling when one is named.
_Range = tuple[tuple[int, ...], tuple[int, ...] | None]


def _floor_of(requirement: str) -> tuple[int, ...] | None:
    """The `>=X` bound an entry declares, or None if it names no lower one.

    The mirror of `ceiling_of`, which this module already leans on for the other end. A bare
    `>` is deliberately not a match: it excludes its own version, and reading it as `>=` would
    widen the reported range by exactly the release most likely to be in dispute.
    """
    spec = _CORE_ENTRY.sub("", requirement.split(";", 1)[0].strip(), count=1)
    for clause in spec.split(","):
        clause = clause.strip()
        if clause.startswith(">="):
            return version(clause[2:].strip())
    return None


def _disjoint(left: _Range, right: _Range) -> bool:
    """Whether two core ranges share no version at all."""
    left_floor, left_ceiling = left
    right_floor, right_ceiling = right
    if right_ceiling is not None and not admits(left_floor, right_ceiling):
        return True
    return left_ceiling is not None and not admits(right_floor, left_ceiling)


def conflicting_pairs(ranges: dict[str, _Range]) -> list[tuple[str, str]]:
    """Every pair among ``ranges`` that no single core version satisfies, sorted."""
    names = sorted(ranges)
    return [
        (left, right)
        for index, left in enumerate(names)
        for right in names[index + 1 :]
        if _disjoint(ranges[left], ranges[right])
    ]


def published_spans() -> dict[str, _Range]:
    """Per dependent, the widest core range any of its published versions declares.

    A pair of spans that do not overlap is a pair the resolver cannot fix by going back a
    version, which is what separates a real break from a family that merely needs an older
    sibling. A package with one published release floored above the rest is the shape with
    nothing to retreat to, and it holds every release that has to resolve the family until one
    of the two publishes again.

    Over-approximate on purpose: a span runs from the oldest floor to the newest ceiling and so
    papers over any hole between them, which makes overlapping spans no proof that two versions
    actually meet. Only the disjoint answer is read, and that one is exact. Reads every
    published version's metadata, so it belongs on the failing path only.
    """
    spans: dict[str, _Range] = {}
    for distribution in dependent_distributions(_ROOT):
        floors: list[tuple[int, ...]] = []
        ceilings: list[tuple[int, ...]] = []
        unbounded = False
        for released in fetch_published_versions(distribution) or []:
            requires = fetch_requires_dist_for_version(distribution, released)
            if requires is None or (requirement := _core_requirement(requires)) is None:
                continue
            floors.append(_floor_of(requirement) or (0,))
            if (ceiling := ceiling_of([requirement])) is None:
                unbounded = True
            else:
                ceilings.append(ceiling)
        if floors:
            spans[distribution] = (min(floors), None if unbounded else max(ceilings))
    return spans


def constraints(
    candidate: str, wheel: Path, published: dict[str, str]
) -> tuple[list[str], list[tuple[str, str]]]:
    """The core ranges in play, and every pair among them no core version satisfies.

    uv explains this by walking every historical version of every sibling — hundreds of lines,
    wrapped to a width it chooses and does not take from COLUMNS. When the core ranges are what
    decide it, naming the two that cannot meet says the same thing in one line.

    The pairs are looked for across the whole set rather than between the candidate and each
    sibling in turn. Two *published* siblings that exclude each other make every candidate
    unresolvable, and a candidate-only reading reports that as ranges which overlap and a
    conflict lying elsewhere — sending the reader to a resolver dump about a package that is
    not at fault.

    An empty list is still the honest half: these packages also carry `agent-framework-core`
    and the Azure libraries, and can collide there while every core range agrees.
    """
    floor, ceiling = declared_range(wheel)
    shown = {candidate: f"maf-sandbox>={_dotted(floor)},<{_dotted(ceiling)}"}
    ranges: dict[str, _Range] = {candidate: (floor, ceiling)}
    for distribution, requirement in published.items():
        if distribution == candidate:
            continue
        shown[distribution] = requirement
        ranges[distribution] = (_floor_of(requirement) or (0,), ceiling_of([requirement]))
    pairs = conflicting_pairs(ranges)
    lines = []
    for name in sorted(ranges):
        label = f"{name} (this checkout)" if name == candidate else f"{name} (published)"
        against = sorted({side for pair in pairs if name in pair for side in pair if side != name})
        note = f"   no core meets both this and {', '.join(against)}" if against else ""
        lines.append(f"    {label:<38} {shown[name]}{note}")
    return lines, pairs


def summarise(lines: list[str]) -> None:
    """Mirror the report into the job summary, so a warning is seen without opening the log."""
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")


def main(argv: list[str]) -> int:
    """CLI entry: install the suite as a set and print what the resolver chose."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dist-dir", type=Path, default=_ROOT / "dist")
    parser.add_argument("--whole-set", action="store_true")
    parser.add_argument("--local-core", action="store_true")
    parsed = parser.parse_args(argv[1:])

    wheels = wheels_in(parsed.dist_dir)
    if not wheels:
        print(f"no wheels in {parsed.dist_dir} — build them first", file=sys.stderr)
        return 2
    newest = latest_published()

    if parsed.whole_set:
        passed, resolved, error = install(
            [str(path) for path in wheels.values()],
            local_core(wheels) if parsed.local_core else None,
        )
        print(f"{'ok  ' if passed else 'FAIL'} whole set: {len(wheels)} wheel(s) together")
        print(
            "\n".join(report(resolved, newest)) if passed else error,
            file=sys.stderr if not passed else sys.stdout,
        )
        if not passed:
            print(
                "the release's own packages cannot be installed together — a range moved past a "
                "sibling rather than past the index",
                file=sys.stderr,
            )
            return 1
        return 0

    published = published_core_ranges()
    warned: list[str] = []
    for distribution, wheel in wheels.items():
        if distribution == _CORE:
            continue
        others = [name for name in wheels if name not in (distribution, _CORE)]
        passed, resolved, error = install([str(wheel), *others])
        if passed:
            print(f"ok   {distribution} beside the published others")
            print("\n".join(report(resolved, newest)))
            continue
        warned.append(distribution)
        lines, pairs = constraints(distribution, wheel, published)
        # A pair that does not include the candidate is a conflict between two published
        # siblings: no version of this package could have resolved beside them, so saying it
        # "does not combine" points the reader at the one artifact that is not at fault.
        elsewhere = [pair for pair in pairs if distribution not in pair]
        if elsewhere and not any(distribution in pair for pair in pairs):
            left, right = elsewhere[0]
            print(
                f"warn {distribution} is not the conflict — published {left} and {right} "
                "exclude each other, so nothing resolves beside them"
            )
        else:
            print(f"warn {distribution} does not combine with the newest published others")
        print("\n".join(lines))
        if not pairs:
            # The core ranges all overlap, so this is a collision somewhere else entirely and
            # the resolver's account is the only one there is. Whole, never a tail.
            print("\n    the core ranges overlap, so the conflict is elsewhere — uv's account:")
            print("\n".join(f"      {line}" for line in error.splitlines()))

    if not warned:
        print("OK  every candidate installs beside the published suite")
        return 0

    if not published:
        # The index named nothing, so there is no published set to resolve and no ground to call
        # one broken. `uv pip install` with no arguments would fail, and failing a release on an
        # unreachable index is the one outcome this mode exists to avoid.
        print("\nthe index named no published dependent — nothing to resolve the family against")
        return 0

    # Which versions *do* work together: the same question asked without pinning the candidate,
    # which is the only way anyone but this script installs the family.
    installable, resolved, error = install(sorted(published))
    lines = [
        "### The suite as a set",
        "",
        f"`{'`, `'.join(warned)}` cannot be installed beside the **newest** published siblings.",
        "",
        "This is a warning, not a failure. Publishing adds a version to the index; it never "
        "removes a combination that already resolved, and a consumer who does not pin gets the "
        "newest set that does.",
        "",
    ]
    if installable:
        lines += ["That set is:", "", "```"] + report(resolved, newest) + ["```"]
        print("\nthe family a consumer resolves today:")
        print("\n".join(report(resolved, newest)))
    else:
        # The gating outcome, so it says which two packages did it rather than leaving that to
        # be read out of a resolver walking every historical version of every sibling. Spans,
        # not newest ranges: a newest-range conflict is routine and the resolver goes back a
        # version for it, where a span conflict is the one nothing can be resolved around.
        conflicts = conflicting_pairs(published_spans())
        lines += ["No set of published versions resolves at all — that is a real break.", ""]
        if conflicts:
            lines += ["No published version of either side reaches the other's core line:", ""]
            lines += [f"- `{left}` against `{right}`" for left, right in conflicts]
            lines += [
                "",
                "Until one of each pair publishes a version admitting the other's core line, "
                "nothing installs both, and every release that has to resolve the family waits "
                "behind it. Where the narrower side is a new package whose only release floors "
                "above the rest, yanking that release restores the set until the others catch "
                "up.",
                "",
            ]
        lines += ["```", error, "```"]
        print("\nno set of published versions resolves at all:", file=sys.stderr)
        for left, right in conflicts:
            print(f"  no published {left} and {right} share a core version", file=sys.stderr)
        print(error, file=sys.stderr)
    summarise(lines)
    if not installable:
        return 1
    print(
        f"\nOK  {len(warned)} candidate(s) do not combine with the newest published siblings yet; "
        "the family still resolves and publishing cannot make it worse"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(run_check(main, sys.argv))
