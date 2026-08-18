"""Assert that a live `samples/15_acas_codeact_host_tools` run really dispatched, and measured.

The live workflow installs the *published* wheels, runs the sample and pipes its output here.

    python samples/15_acas_codeact_host_tools/agent.py | tee out.txt
    python scripts/check_live_host_tools_dispatch_sample.py out.txt   # or: ... | python …

**What is asserted is chosen so that a model's mood cannot decide a release.** Both routes run
Python in the sandbox, so both are held to the same two things, and both are properties of
machinery rather than of prose:

- **The program printed the exact total.** Read from the framework's record of what
  `execute_code` returned, so an interpreter produced it. Not exact means the program did not
  run, did not get its prices, or ignored them.
- **What the model wrote into a tool call.** Dispatched, it must be nothing: the program is
  written before any dispatch happens, so there is no price to embed. Directly, it must be
  something: the values arrive as tool results and the only road into the program is for the
  model to write them into its source. That contrast is the sample's whole finding, and both
  halves of it are structural — neither depends on the model being clever or careless.

Wall clock and tokens are recorded and never bounded. They are what the sample exists to
publish, and a threshold would turn a measurement into a pass mark on somebody else's control
plane. What a model *said*, in prose, is never read at all.

Every line read must carry the `[measured]` tag at the left margin (#314). The sample's
`quoted()` prefixes any tagged line inside a model's reply with `> `, so prose that tries to
answer for the host is visibly not the host answering.

Exits non-zero listing every reason it failed.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

#: The tag the sample puts on every line it measured, and the guard against a model writing one.
_TAG = "[measured]"

#: The two roads to the same function, spelled as the sample spells them.
_DISPATCH = "dispatch route"
_DIRECT = "direct route"
_ROUTES = (_DISPATCH, _DIRECT)
_ANY_ROUTE = "|".join(re.escape(name) for name in _ROUTES)

#: Act 4 names the same two by their short form.
_SHORT = {"dispatched": _DISPATCH, "direct": _DIRECT}

#: How many distinct SKUs the order names. Every one has to be asked for, since no price is
#: reachable any other way — fewer means one was invented.
_SKUS = 3

#: What the sample prints when the model wrote no price into any tool call.
_NONE = "none"

_F = re.MULTILINE

#: `dispatches: 3 across 3 SKU(s)`. The count may exceed the SKU count — a model is free to
#: write a program that asks twice — so only the distinct figure is pinned.
_DISPATCHES = re.compile(rf"^\s*{re.escape(_TAG)}\s+dispatches:\s+(\d+)\s+across\s+(\d+)\s+SKU", _F)

#: `<route>: N lookup(s), N message(s), X.XXs, N tokens`
_COST = re.compile(
    rf"^\s*{re.escape(_TAG)}\s+({_ANY_ROUTE}):\s+(\d+)\s+lookup\(s\),\s+\d+\s+message\(s\),\s+"
    r"([\d.]+)s,\s+(\d+|None)\s+tokens",
    _F,
)

#: `<route>: the program printed 218.15: True`
_PRINTED = re.compile(
    rf"^\s*{re.escape(_TAG)}\s+({_ANY_ROUTE}):\s+the program printed\s+[\d.]+:\s+(True|False)",
    _F,
)

#: `<route>: prices the model wrote into code: none` — or a list of them.
_WROTE = re.compile(
    rf"^\s*{re.escape(_TAG)}\s+({_ANY_ROUTE}):\s+prices the model wrote into code:\s+(.+?)\s*$",
    _F,
)

#: `prices the model handled, dispatched: 0 of 3` — act 4 restating the line above.
_HANDLED = re.compile(
    rf"^\s*{re.escape(_TAG)}\s+prices the model handled,\s+(dispatched|direct):\s+(\d+)\s+of\s+\d+",
    _F,
)

#: `round trip: N gap(s), min X.XXs, median X.XXs, max X.XXs`
_ROUND_TRIP = re.compile(
    rf"^\s*{re.escape(_TAG)}\s+round trip:\s+(\d+)\s+gap\(s\),\s+min\s+([\d.]+)s,\s+"
    r"median\s+([\d.]+)s,\s+max\s+([\d.]+)s",
    _F,
)

#: The footer. Billable, so this one is not a formality.
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
    """Every SKU was asked for, which is the only way a price reaches the guest."""
    match, failures = _once(_DISPATCHES.findall(output), "dispatches")
    if match is None:
        return failures
    total, distinct = int(match[0]), int(match[1])
    if distinct != _SKUS:
        failures.append(
            f"the program asked for {distinct} distinct SKU(s) and the order names {_SKUS} — a "
            "price it did not ask for is a price it invented, and no other road reaches one"
        )
    if total == 0:
        failures.append("nothing was dispatched at all, so the sample measured a road not taken")
    return failures


def _assess_both_routes_ran(output: str) -> list[str]:
    """Each route reports a cost, and each one reached the function at least once."""
    failures: list[str] = []
    for route in _ROUTES:
        cost, problems = _once([m for m in _COST.findall(output) if m[0] == route], f"{route} cost")
        failures.extend(problems)
        if cost is not None and int(cost[1]) == 0:
            failures.append(f"{route} reports 0 lookups, so it never reached the function at all")
    return failures


def _assess_the_interpreter_computed_it(output: str) -> list[str]:
    """Both routes run Python, so both are held to what the interpreter printed."""
    failures: list[str] = []
    for route in _ROUTES:
        match, problems = _once(
            [m for m in _PRINTED.findall(output) if m[0] == route], f"{route} program printed"
        )
        failures.extend(problems)
        if match is not None and match[1] != "True":
            failures.append(
                f"the {route}'s program did not print the exact total — an interpreter computed "
                "it from prices the host supplied, so this is not a model getting arithmetic "
                "wrong. Either the program never ran, or it did not use what it was told"
            )
    return failures


def _assess_who_carried_the_prices(output: str) -> list[str]:
    """The finding, and both halves of it are structural.

    Dispatched: the program is written before a dispatch can happen, so a price cannot be in
    it. Directly: the values arrive as tool results and the only way into the program is for
    the model to write them there. Neither turns on how the model felt that morning.
    """
    failures: list[str] = []
    wrote: dict[str, str] = {}
    for route in _ROUTES:
        match, problems = _once(
            [m for m in _WROTE.findall(output) if m[0] == route], f"{route} prices written"
        )
        failures.extend(problems)
        if match is not None:
            wrote[route] = match[1].strip()

    if wrote.get(_DISPATCH, _NONE) != _NONE:
        failures.append(
            f"the dispatched route wrote {wrote[_DISPATCH]} into a tool call — the program is "
            "written before any dispatch happens, so a price cannot have reached it that way. "
            "Either the model was handed one somewhere it should not have been, or this line "
            "no longer measures what it says"
        )
    if wrote.get(_DIRECT) == _NONE:
        failures.append(
            "the direct route wrote no price into a tool call, so the contrast this sample "
            "exists to show did not happen — on that road the model has to carry each value "
            "into the program, and a run where it did not is not comparable"
        )

    # Act 4 restates both as counts. Disagreement means one of the four lines is not from this run.
    restated = _HANDLED.findall(output)
    for short, count in restated:
        route = _SHORT[short]
        if route not in wrote:
            continue
        listed = 0 if wrote[route] == _NONE else wrote[route].count("'") // 2
        if int(count) != listed:
            failures.append(
                f"act 4 says the model handled {count} price(s) on the {route} where the route "
                f"itself reported {listed} — the two lines describe one run and disagree"
            )
    if len(restated) != len(_ROUTES):
        failures.append("act 4 did not restate both routes' price counts")
    return failures


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
    """Billable, and one sandbox serves both routes."""
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
    return [
        *_assess_dispatch_happened(output),
        *_assess_both_routes_ran(output),
        *_assess_the_interpreter_computed_it(output),
        *_assess_who_carried_the_prices(output),
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

    print(
        "OK  both routes computed the exact total in the sandbox; the model carried every "
        "price on one road and none of them on the other"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
