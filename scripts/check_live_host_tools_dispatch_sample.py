"""Assert that a live `samples/15_acas_codeact_host_tools` run really dispatched, and measured.

The live workflow installs the *published* wheels, runs the sample and pipes its output here.

    python samples/15_acas_codeact_host_tools/agent.py | tee out.txt
    python scripts/check_live_host_tools_dispatch_sample.py out.txt   # or: ... | python …

**What is asserted and what is only recorded is the whole design of this check.**

A model stands between the library and stdout twice over, so the two routes cannot be held to
the same standard:

- The **dispatch** route must reach the exact total. That is not a claim about the model: the
  sum was computed by a Python interpreter from prices the host handed it, so anything other
  than exact means the program did not run, did not call out, or did not use what came back.
- The **direct** route is *recorded, never required*. It is a model doing arithmetic in its
  head. It has been wrong every time it has been run, and a check that depended on it staying
  wrong would be asserting a model stays bad at addition — which is not this repository's
  claim to make, and would go red on the day it stops being true.

Every line read must carry the `[measured]` tag (#314). A model that writes
``[measured] direct route: reply carries 218.15: True`` into its prose is answering for the
host, and the sample's `quoted()` prefixes any such line with `> ` — so a tagged line at the
left margin is the sample's own. Lines are matched anchored to guard that.

Exits non-zero listing every reason it failed.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

#: The tag the sample puts on every line it measured, and the guard against a model writing one.
_TAG = "[measured]"

#: The two SKU-bearing routes, spelled as the sample spells them.
_DISPATCH = "dispatch route"
_DIRECT = "direct route"

#: How many distinct SKUs the order names. The program has to ask for every one of them, since
#: no price is reachable any other way — fewer means it guessed one.
_SKUS = 3

_F = re.MULTILINE

#: `dispatches: 3 across 3 SKU(s)`. The count may exceed the SKU count — a model is free to
#: write a program that asks twice — so only the distinct figure is pinned.
_DISPATCHES = re.compile(rf"^\s*{re.escape(_TAG)}\s+dispatches:\s+(\d+)\s+across\s+(\d+)\s+SKU", _F)

#: `<route>: N call(s), X.XXs, N tokens`. Wall clock and tokens are recorded, not bounded:
#: they are what the sample exists to publish, and a threshold here would turn a measurement
#: into a pass mark on somebody else's control plane.
_COST = re.compile(
    rf"^\s*{re.escape(_TAG)}\s+(dispatch route|direct route):\s+(\d+)\s+call\(s\),\s+"
    r"([\d.]+)s,\s+(\d+|None)\s+tokens",
    _F,
)

#: `<route>: reply carries 218.15: True`
_CARRIES = re.compile(
    rf"^\s*{re.escape(_TAG)}\s+(dispatch route|direct route):\s+reply carries\s+"
    r"([\d.]+):\s+(True|False)",
    _F,
)

#: Act 4 restates both verdicts. Restating is the point: the two have to agree, or one of them
#: was written by something other than the run.
_VERDICT = re.compile(
    rf"^\s*{re.escape(_TAG)}\s+(dispatch|direct) route reached the exact total:\s+(True|False)",
    _F,
)

#: `round trip: N gap(s), min X.XXs, median X.XXs, max X.XXs`
_ROUND_TRIP = re.compile(
    rf"^\s*{re.escape(_TAG)}\s+round trip:\s+(\d+)\s+gap\(s\),\s+min\s+([\d.]+)s,\s+"
    r"median\s+([\d.]+)s,\s+max\s+([\d.]+)s",
    _F,
)

#: The footer. One sandbox, deleted rather than left to the lifecycle timers — this one is
#: billable, so a sample that leaks it costs money quietly.
_DISPOSED = re.compile(rf"^\s*{re.escape(_TAG)}\s+Disposed\s+(\d+)\s+sandbox\(es\)\.", _F)


def _once[M](matches: list[M], what: str) -> tuple[M | None, list[str]]:
    """Exactly one, or refuse.

    A second line of the same shape is not resolved in favour of either. Whichever the sample
    printed, the other came from somewhere else, and a check that picked one would be choosing
    which of two disagreeing sources to believe.
    """
    if not matches:
        return None, [f"no tagged '{what}' line — the sample did not report it"]
    if len(matches) > 1:
        return None, [
            f"'{what}' appears {len(matches)} times, so none of them can be trusted — the "
            "sample prints it once per run"
        ]
    return matches[0], []


def _assess_dispatch_happened(output: str) -> list[str]:
    """Every SKU was asked for, which is the only way a price reaches the program."""
    match, failures = _once(_DISPATCHES.findall(output), "dispatches")
    if match is None:
        return failures
    total, distinct = int(match[0]), int(match[1])
    if distinct != _SKUS:
        failures.append(
            f"the program asked for {distinct} distinct SKU(s) and the order names {_SKUS} — a "
            "price it did not ask for is a price it invented, and no other road reaches one"
        )
    if total < distinct:
        failures.append(f"{total} dispatch(es) covering {distinct} SKU(s) is not arithmetic")
    if total == 0:
        failures.append("nothing was dispatched at all, so the sample measured a road not taken")
    return failures


def _assess_both_routes_reported(output: str) -> tuple[dict[str, bool], list[str]]:
    """Both routes report a cost and a verdict, and the verdicts are internally consistent."""
    failures: list[str] = []
    carried: dict[str, bool] = {}

    for route in (_DISPATCH, _DIRECT):
        cost, problems = _once([m for m in _COST.findall(output) if m[0] == route], f"{route} cost")
        failures.extend(problems)
        if cost is not None and int(cost[1]) == 0:
            failures.append(f"{route} reports 0 calls, so it never reached the function at all")

        says, problems = _once([m for m in _CARRIES.findall(output) if m[0] == route], route)
        failures.extend(problems)
        if says is not None:
            carried[route] = says[2] == "True"

    # Act 4 restates both. Disagreement means one of the four lines did not come from this run.
    verdicts = dict(_VERDICT.findall(output))
    for route, exact in carried.items():
        restated = verdicts.get(route.split()[0])
        if restated is None:
            failures.append(f"act 4 never restated the {route} verdict")
        elif (restated == "True") != exact:
            failures.append(
                f"the {route} is reported as {exact} where it is measured and {restated} where "
                "it is summarised — the two lines describe one run and disagree"
            )
    return carried, failures


def _assess_the_program_computed_it(carried: dict[str, bool]) -> list[str]:
    """The one hard assertion: a program that ran and called out reaches the exact total."""
    if carried.get(_DISPATCH) is False:
        return [
            "the dispatched reply does not carry the exact total — the prices came from the "
            "host and the sum came from an interpreter, so this is not a model getting "
            "arithmetic wrong. Either the program did not use what it was told, or it never ran"
        ]
    return []


def _assess_round_trip(output: str) -> list[str]:
    """A measurement was published, and its three figures are ordered."""
    match, failures = _once(_ROUND_TRIP.findall(output), "round trip")
    if match is None:
        return failures
    gaps, low, mid, high = int(match[0]), float(match[1]), float(match[2]), float(match[3])
    if gaps < 1:
        failures.append("the round-trip line reports no gaps, so nothing was measured")
    if not low <= mid <= high:
        failures.append(
            f"min {low}s, median {mid}s and max {high}s are not ordered — whatever produced "
            "these did not produce them from one set of samples"
        )
    return failures


def _assess_the_sandbox_went_away(output: str) -> list[str]:
    """Billable, so this one is not a formality."""
    match, failures = _once(_DISPOSED.findall(output), "Disposed")
    if match is None:
        return failures
    if int(match) != 1:
        failures.append(
            f"{match} sandbox(es) disposed where the sample acquires one — a sandbox this "
            "sample leaves behind bills until the lifecycle timers reach it"
        )
    return failures


def assess(output: str) -> list[str]:
    """Every reason the run does not show a real, measured dispatch."""
    carried, failures = _assess_both_routes_reported(output)
    return [
        *_assess_dispatch_happened(output),
        *failures,
        *_assess_the_program_computed_it(carried),
        *_assess_round_trip(output),
        *_assess_the_sandbox_went_away(output),
    ]


def main(argv: list[str]) -> int:
    """CLI entry: read the sample output from a file or stdin, run ``assess``, print OK or FAIL."""
    if len(argv) > 2:
        print(f"usage: {argv[0]} [output-file]  (reads stdin if omitted)", file=sys.stderr)
        return 2
    output = (
        sys.stdin.read()
        if len(argv) == 1 or argv[1] == "-"
        else Path(argv[1]).read_text(encoding="utf-8")
    )

    failures = assess(output)
    if failures:
        print("FAIL: the dispatch sample did not show a program calling back out:", file=sys.stderr)
        for reason in failures:
            print(f"  - {reason}", file=sys.stderr)
        return 1

    # Named rather than silent: the direct route is the half this check does not enforce, so
    # the reader is told which way it went on this run instead of inferring it from a green.
    direct = "reached it too" if carried_direct(output) else "did not reach it"
    print(f"OK  the program dispatched, computed the exact total, and the direct route {direct}")
    return 0


def carried_direct(output: str) -> bool:
    """Whether the direct route happened to land on the total. Reported, never required."""
    return any(route == _DIRECT and exact == "True" for route, _, exact in _CARRIES.findall(output))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
