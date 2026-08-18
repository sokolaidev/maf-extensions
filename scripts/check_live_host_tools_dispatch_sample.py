"""Assert that a live `samples/15_acas_codeact_host_tools` run really dispatched, and measured.

The live workflow installs the *published* wheels, runs the sample and pipes its output here.

    python samples/15_acas_codeact_host_tools/agent.py | tee out.txt
    python scripts/check_live_host_tools_dispatch_sample.py out.txt   # or: ... | python …

**What is asserted is chosen so a model's mood cannot decide a release.** Both routes run
Python in the sandbox and both walk the same four stages, so what is enforced is either an
interpreter's output or a structural property of the two roads:

- **Both programs printed both state totals.** Read from the framework's record of what
  `execute_code` returned, so an interpreter produced them.
- **Direct needed more model round trips than dispatch.** Direct batches within a stage and
  never across one, so it pays per stage; dispatch resolves the whole walk inside one program.
- **Who carried the sales figures.** None on the dispatched route — the program is written
  before any dispatch can answer, so a figure cannot be in it — and all of them on the direct
  route, where the values arrive as tool results and the only road into the program is for the
  model to write them into its source.
- **The runs left their transport files behind.** #302 asks for the per-run subdirectory and
  its cleanup; the directory is real and the cleanup does not exist (#438), so the count going
  *up* is the honest thing to assert. A served call leaves three files — the claimed id, the
  request and the answer — so the answered subset is what says how much traffic there was.

Wall clock, tokens and lookup counts are recorded and never bounded. They are what the sample
exists to publish, and a threshold would turn a measurement into a pass mark on somebody
else's control plane. What a model *said*, in prose, is never read.

Every line read must carry the `[measured]` tag at the left margin (#314). The sample's
`quoted()` prefixes any tagged line inside a model's reply with `> `, so prose that tries to
answer for the host is visibly not the host answering.

Exits non-zero listing every reason it failed.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any

_TAG = "[measured]"
_DISPATCH = "dispatch route"
_DIRECT = "direct route"
_ROUTES = (_DISPATCH, _DIRECT)
_ANY_ROUTE = "|".join(re.escape(name) for name in _ROUTES)

#: Act 4 names the same two by their short form.
_SHORT = {"dispatched": _DISPATCH, "direct": _DIRECT}

#: How many states the summary covers, and so how many totals each program must print.
_STATES = 2

#: The walk's stages. Direct pays a model round trip for each, which is the whole comparison,
#: so a run where the shape collapsed is a run that measured something else.
_STAGES = 4

_F = re.MULTILINE


def _tagged(pattern: str) -> re.Pattern[str]:
    """Anchored at the left margin, because that is what a model cannot forge."""
    return re.compile(rf"^\s*{re.escape(_TAG)}\s+{pattern}", _F)


_CAP = _tagged(r"dispatch cap for the run:\s+(\d+)\s+\(the walk needs\s+(\d+)[^,]*,\s+(\d+)")
_TRIPS = _tagged(rf"({_ANY_ROUTE}):\s+(\d+)\s+lookup\(s\) over\s+(\d+)\s+model round trip\(s\)")
_SHAPE = _tagged(rf"({_ANY_ROUTE}):\s+tool calls per model round trip:\s+\[([^\]]*)\]")
_COST = _tagged(rf"({_ANY_ROUTE}):\s+([\d.]+)s,\s+(\d+)\s+tokens")
_TOTALS = _tagged(rf"({_ANY_ROUTE}):\s+state totals the program printed:\s+(\d+)\s+of\s+(\d+)")
_WROTE = _tagged(
    rf"({_ANY_ROUTE}):\s+sales figures the model wrote into code:\s+(\d+)\s+of\s+(\d+)"
)
_HANDLED = _tagged(r"sales figures the model handled,\s+(dispatched|direct):\s+(\d+)\s+of\s+(\d+)")
_ROUND_TRIP = _tagged(
    rf"({_ANY_ROUTE}):\s+round trip:\s+(\d+)\s+gap\(s\),\s+min\s+([\d.]+)s,\s+"
    r"median\s+([\d.]+)s,\s+max\s+([\d.]+)s"
)
_RUN_DIRS = _tagged(r"run directories in the guest:\s+(\d+)")
_DISPATCHING = _tagged(r"of those, runs that dispatched:\s+(\d+)")
_LEFT = _tagged(r"transport files left behind:\s+(\d+), of which answered calls:\s+(\d+)")
_DISPOSED = _tagged(r"Disposed\s+(\d+)\s+sandbox\(es\)\.")


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


def _per_route(
    output: str, pattern: re.Pattern[str], what: str
) -> tuple[dict[str, Any], list[str]]:
    """One match per route, or a reason for each that is missing or doubled."""
    found: dict[str, Any] = {}
    failures: list[str] = []
    for route in _ROUTES:
        match, problems = _once(
            [m for m in pattern.findall(output) if m[0] == route], f"{route} {what}"
        )
        failures.extend(problems)
        if match is not None:
            found[route] = match
    return found, failures


def _assess_the_cap_was_budgeted(output: str) -> list[str]:
    """A call-heavy host has to raise the default, and saying so is half the lesson."""
    match, failures = _once(_CAP.findall(output), "dispatch cap")
    if match is None:
        return failures
    cap, minimum, naive = int(match[0]), int(match[1]), int(match[2])
    if cap < minimum:
        failures.append(
            f"the run allowed {cap} dispatches where the walk needs at least {minimum} — the "
            "program cannot finish, and the sample would be measuring a truncated one"
        )
    elif cap <= naive:
        # Against the naive figure rather than the theoretical best, and rather than against
        # the registry's own default: a cap that only clears the efficient path passes on the
        # runs where the model happens to cache its lookups and truncates the ones where it
        # does not, which is the failure this act exists to have already met.
        failures.append(
            f"the run allowed {cap} dispatches against {naive} for the same walk written "
            "without caching. A budget that only fits the efficient program is decided by how "
            "the model felt, and this sample is here because the default does not fit at all"
        )
    return failures


def _assess_both_interpreters_answered(output: str) -> list[str]:
    """Both routes compute in the sandbox, so both are held to what came back."""
    found, failures = _per_route(output, _TOTALS, "state totals")
    for route, match in found.items():
        printed, expected = int(match[1]), int(match[2])
        if expected != _STATES:
            failures.append(f"{route} scored itself out of {expected} states, not {_STATES}")
        if printed != expected:
            failures.append(
                f"the {route}'s program printed {printed} of {expected} state totals — an "
                "interpreter computed them from data the host supplied, so a missing one means "
                "the program did not finish the walk rather than that a model was careless"
            )
    return failures


def _assess_direct_pays_per_stage(output: str) -> list[str]:
    """The structural comparison: batching within a stage, never across one."""
    found, failures = _per_route(output, _TRIPS, "round-trip count")
    shapes, problems = _per_route(output, _SHAPE, "lookups per round trip")
    failures.extend(problems)

    if len(found) == len(_ROUTES):
        dispatched, direct = int(found[_DISPATCH][2]), int(found[_DIRECT][2])
        if direct <= dispatched:
            failures.append(
                f"the direct route took {direct} model round trip(s) and the dispatched route "
                f"{dispatched} — the comparison this sample exists for is that walking the "
                "stages in the model's own loop costs more of them, and this run did not show it"
            )
    for route, match in found.items():
        if int(match[1]) == 0:
            failures.append(f"{route} made no lookups at all, so it answered from somewhere else")

    for route, shape in shapes.items():
        groups = [g for g in shape[1].split(",") if g.strip()]
        # Both numbers are the same list counted two ways, so a disagreement is not a finding
        # about the run — it is one of the two lines not describing it.
        if route in found and len(groups) != int(found[route][2]):
            failures.append(
                f"{route} reports {found[route][2]} model round trip(s) and a shape with "
                f"{len(groups)} entr(y/ies); the sample derives both from one list, so these "
                "cannot both be from this run"
            )
    if _DIRECT in shapes:
        groups = [g for g in shapes[_DIRECT][1].split(",") if g.strip()]
        if len(groups) < _STAGES:
            failures.append(
                f"the direct route asked in {len(groups)} batch(es) and the walk has {_STAGES} "
                "stages — fewer means it did not have to wait for one stage to answer before "
                "asking the next, and the workload stopped being the one described"
            )
    return failures


def _assess_who_carried_the_figures(output: str) -> list[str]:
    """The finding, and both halves are structural rather than a matter of model mood."""
    found, failures = _per_route(output, _WROTE, "figures written")
    wrote = {route: (int(match[1]), int(match[2])) for route, match in found.items()}

    if wrote.get(_DISPATCH, (0, 0))[0] != 0:
        failures.append(
            f"the dispatched route wrote {wrote[_DISPATCH][0]} sales figure(s) into a tool "
            "call — the program is written before any dispatch can answer, so a figure cannot "
            "have reached it that way, and this line has stopped measuring what it says"
        )
    if _DIRECT in wrote:
        carried, expected = wrote[_DIRECT]
        if carried != expected:
            # Not "more than none". Every figure has to cross the model on that road, so a
            # partial count is a run that got its data from somewhere this sample did not
            # measure — and it would still read as the contrast while understating it.
            failures.append(
                f"the direct route wrote {carried} of {expected} sales figures into a tool "
                "call. On that road every value has to cross the model to reach the program, "
                "so anything short of all of them means the run is not the comparison this "
                "sample makes"
            )

    # One restatement per route, matched by route rather than counted. Two for `direct` and
    # none for `dispatched` is also two lines, and would pass a length check while act 4 said
    # nothing at all about half the comparison.
    for short, route in _SHORT.items():
        match, problems = _once(
            [m for m in _HANDLED.findall(output) if m[0] == short], f"act 4 {short} restatement"
        )
        failures.extend(problems)
        if match is not None and route in wrote and (int(match[1]), int(match[2])) != wrote[route]:
            failures.append(
                f"act 4 says the model handled {match[1]} of {match[2]} figure(s) on the "
                f"{route} where the route itself reported {wrote[route][0]} of "
                f"{wrote[route][1]} — the two lines describe one run and disagree"
            )
    return failures


def _assess_the_round_trips(output: str) -> list[str]:
    """Reported for the dispatched route only, and its three figures are ordered."""
    matches = _ROUND_TRIP.findall(output)
    if any(m[0] == _DIRECT for m in matches):
        failures = [
            "a round-trip line was printed for the direct route — its lookups run in the host "
            "process between two model turns, so whatever that measured is not a round trip and "
            "inviting the reader to compare it with the dispatched figure is the wrong reading"
        ]
    else:
        failures = []
    match, problems = _once([m for m in matches if m[0] == _DISPATCH], "dispatch round trip")
    failures.extend(problems)
    if match is None:
        return failures
    gaps, low, mid, high = int(match[1]), float(match[2]), float(match[3]), float(match[4])
    if gaps < 1:
        failures.append("the round-trip line reports no gaps, so nothing was measured")

    # The ledger times the interval between consecutive calls, so *n* dispatches yield exactly
    # *n - 1* of them. Checking only that the count is positive would accept a line claiming
    # 21 lookups and one measured gap, which is a median over a twentieth of the run.
    lookups = {route: int(count) for route, count, _ in _TRIPS.findall(output)}
    if _DISPATCH in lookups and gaps != lookups[_DISPATCH] - 1:
        failures.append(
            f"{gaps} round-trip gap(s) were measured across {lookups[_DISPATCH]} dispatched "
            f"lookup(s), and consecutive pairs of {lookups[_DISPATCH]} calls are "
            f"{lookups[_DISPATCH] - 1} — so the summary describes a different set of calls "
            "from the one the route reported"
        )
    if not low <= mid <= high:
        failures.append(
            f"min {low}s, median {mid}s and max {high}s are not ordered — whatever produced "
            "these did not produce them from one set of samples"
        )
    return failures


def _assess_what_the_runs_left(output: str) -> list[str]:
    """#302's per-run subdirectory, and the cleanup that #438 says nobody can do."""
    failures: list[str] = []
    dirs, problems = _once(_RUN_DIRS.findall(output), "run directories")
    failures.extend(problems)
    dispatching, problems = _once(_DISPATCHING.findall(output), "runs that dispatched")
    failures.extend(problems)
    left, problems = _once(_LEFT.findall(output), "files left behind")
    failures.extend(problems)
    if None in (dirs, dispatching, left):
        return failures

    if int(dispatching) < 1:  # type: ignore[arg-type]
        failures.append("no run dispatched, so there is no transport traffic to have left behind")
    if int(dirs) < int(dispatching):  # type: ignore[arg-type]
        failures.append(
            f"{dispatching} run(s) dispatched out of {dirs} in the guest, which is not arithmetic"
        )
    total, answers = int(left[0]), int(left[1])  # type: ignore[index]
    if total < 1:
        failures.append(
            "no transport files were found in the guest. Nothing deletes them — the protocol "
            "has no way (#438) — so zero means the enumeration looked somewhere the transport "
            "does not write"
        )
    if answers > total:
        failures.append(f"{answers} answered call(s) among {total} file(s) is not arithmetic")
    if answers < 1:
        failures.append(
            "no answered call was found among the files left behind, so nothing in the guest "
            "records a dispatch having been served"
        )
    return failures


def _assess_the_sandbox_went_away(output: str) -> list[str]:
    """Billable, and one sandbox serves both routes and act 5's enumeration."""
    match, failures = _once(_DISPOSED.findall(output), "Disposed")
    if match is None:
        return failures
    if int(match) != 1:
        failures.append(
            f"{match} sandbox(es) disposed where the sample acquires one — a sandbox this "
            "sample leaves behind bills until the lifecycle timers reach it, and it is also "
            "the only thing that removes the files act 5 counted"
        )
    return failures


def assess(output: str) -> list[str]:
    """Every reason the run does not show a real, measured, call-heavy dispatch."""
    _, cost_problems = _per_route(output, _COST, "cost")
    return [
        *_assess_the_cap_was_budgeted(output),
        *cost_problems,
        *_assess_both_interpreters_answered(output),
        *_assess_direct_pays_per_stage(output),
        *_assess_who_carried_the_figures(output),
        *_assess_the_round_trips(output),
        *_assess_what_the_runs_left(output),
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

    trips = {route: count for route, _, count in _TRIPS.findall(output)}
    print(
        f"OK  both programs answered from the sandbox; the walk cost "
        f"{trips.get(_DIRECT, '?')} model round trips in the model's loop against "
        f"{trips.get(_DISPATCH, '?')} dispatched, and the model carried no data on the second"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
