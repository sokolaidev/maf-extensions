"""Assert that a live `samples/15_acas_codeact_host_tools` run really dispatched, and measured.

The live workflow installs the *published* wheels, runs the sample and pipes its output here.

    python samples/15_acas_codeact_host_tools/agent.py | tee out.txt
    python scripts/check_live_host_tools_dispatch_sample.py out.txt   # or: ... | python -

**What is asserted is chosen so a model's mood cannot decide a release.** Both routes run
Python in the sandbox and walk the same four stages, so what is enforced is either an
interpreter's output or a structural property of the two roads:

- Both programs printed the whole table — both state totals and all six cells.
- Direct needed more tool-calling rounds than dispatch.
- The dispatched route's model carried no sales figure into code; the direct route's carried
  all twelve.
- The runs declare both transport cleanup and framework call-directory cleanup, which act 5 measures.
- The gaps behind the round-trip summary were the ones the transport made: *n* calls over *p*
  programs leave *n - p*.

Wall clock, tokens and lookup counts are recorded and never bounded — a threshold would turn a
measurement into a pass mark on somebody else's control plane. What a model *said* is never
read, and every line must carry the `[measured]` tag at the left margin (#314).

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

#: The walk's stages. Direct pays a tool-calling round for each, which is the comparison,
#: so a run where the shape collapsed is a run that measured something else.
_STAGES = 4

#: Distinct sales figures in the dataset. Held here rather than taken from the sample's own
#: denominator: `0 of 0` is self-consistent on every line and would otherwise pass, as would
#: `7 of 7`. A check that reads its expectations off the output it is checking agrees with
#: whatever it is given.
_FIGURES = 12

#: One sandbox per route, so neither route's program can read the other's leftovers.
_SANDBOXES = 2

#: The walk's own arithmetic, and the registry default it has to clear. Pinned here for the
#: reason `_FIGURES` is: read off the sample's own line, `32 (the walk needs 2 at best, 12
#: written naively)` grades itself and passes while demonstrating nothing.
_MINIMUM_LOOKUPS = 12
_NAIVE_LOOKUPS = 21
_REGISTRY_DEFAULT_CAP = 16

#: The worst live run so far. The naive figure is what the walk costs written carelessly, not
#: a ceiling — the model writes the program, and one that re-reads a name it already has costs
#: more than either arithmetic predicts. A cap has to clear this, not that.
_OBSERVED_MAX_LOOKUPS = 29

#: Product names, which only the by-product table needs. A per-state total is a sum of the
#: sales amounts, so a program can print both totals without ever asking for one.
_PRODUCTS = 3

#: One per state and product: the table the task asks for, and the thing the state totals
#: cannot establish on their own, a total being a sum that hides its terms.
_CELLS = _STATES * _PRODUCTS

_F = re.MULTILINE


def _tagged(pattern: str) -> re.Pattern[str]:
    """Anchored at the left margin, because that is what a model cannot forge."""
    return re.compile(rf"^\s*{re.escape(_TAG)}\s+{pattern}", _F)


_CAP = _tagged(r"dispatch cap for the run:\s+(\d+)\s+\(the walk needs\s+(\d+)[^,]*,\s+(\d+)")
_TRIPS = _tagged(rf"({_ANY_ROUTE}):\s+(\d+)\s+lookup\(s\) over\s+(\d+)\s+tool-calling round\(s\)")
_SHAPE = _tagged(rf"({_ANY_ROUTE}):\s+tool calls per round:\s+\[([^\]]*)\]")
_COST = _tagged(rf"({_ANY_ROUTE}):\s+([\d.]+)s,\s+(\d+)\s+tokens")
_TOTALS = _tagged(rf"({_ANY_ROUTE}):\s+state totals the program printed:\s+(\d+)\s+of\s+(\d+)")
_CELLS_LINE = _tagged(
    rf"({_ANY_ROUTE}):\s+product totals the program printed:\s+(\d+)\s+of\s+(\d+)"
)
_ROWS_LINE = _tagged(rf"({_ANY_ROUTE}):\s+table rows the program printed:\s+(\d+)\s+of\s+(\d+)")
_WROTE = _tagged(
    rf"({_ANY_ROUTE}):\s+sales figures the model wrote into code:\s+(\d+)\s+of\s+(\d+)"
)
_RESTATED = _tagged(
    r"sales figures the model wrote into code,\s+(dispatched|direct):\s+(\d+)\s+of\s+(\d+)"
)
_OBSERVED_PROGRAMS = _tagged(rf"({_ANY_ROUTE}):\s+programs that dispatched:\s+(\d+)")
_ROUND_TRIP = _tagged(
    rf"({_ANY_ROUTE}):\s+round trip:\s+(\d+)\s+gap\(s\),\s+min\s+([\d.]+)s,\s+"
    r"median\s+([\d.]+)s,\s+max\s+([\d.]+)s"
)
_BOUNDARIES = _tagged(
    rf"({_ANY_ROUTE}):\s+program boundaries observed:\s+(\d+),\s+min\s+([\d.]+)s,\s+"
    r"max\s+([\d.]+)s"
)
_STAGES_RUN = _tagged(rf"({_ANY_ROUTE}):\s+lookup stages exercised:\s+(\d+)\s+of\s+(\d+)")
_NAMED = _tagged(rf"({_ANY_ROUTE}):\s+product names in the table:\s+(\d+)\s+of\s+(\d+)")
_CLEANUP = _tagged(r"transport cleanup:\s+(reclaimed by the transport|left for the sandbox)")
_CALL_CLEANUP = _tagged(
    r"call directory cleanup:\s+(reclaimed by the framework|left for the sandbox)"
)
_RUN_DIRS = _tagged(r"run directories across both sandboxes:\s+(\d+)")
_DISPATCHING = _tagged(r"of those, runs that dispatched:\s+(\d+)")
_LEFT = _tagged(r"transport files left behind:\s+(\d+), of which answered calls:\s+(\d+)")
_DISPOSED = _tagged(r"Disposed\s+(\d+)\s+sandbox\(es\)\.")


def _once[M](matches: list[M], what: str) -> tuple[M | None, list[str]]:
    """Exactly one, or refuse.

    Two lines of the same shape are not resolved in favour of either: one came from somewhere
    else, and picking would be choosing which of two disagreeing sources to believe.
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
    if (minimum, naive) != (_MINIMUM_LOOKUPS, _NAIVE_LOOKUPS):
        failures.append(
            f"the run describes a walk needing {minimum} at best and {naive} written naively, "
            f"where this one needs {_MINIMUM_LOOKUPS} and {_NAIVE_LOOKUPS}. The cap is graded "
            "against these two figures, so a run supplying its own makes the grade its own"
        )
    if cap <= _REGISTRY_DEFAULT_CAP:
        failures.append(
            f"the run allowed {cap} dispatches, which the registry allows by default "
            f"({_REGISTRY_DEFAULT_CAP}). The sample is here because this workload does not fit "
            "the default, and a run that never raised it is not showing that"
        )
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
    elif cap <= _OBSERVED_MAX_LOOKUPS:
        # Above both arithmetics and still short: neither figure is a ceiling, because the
        # model writes the program. A cap here passes on the run that budgeted it and
        # truncates the next one, which is the worst way for this to fail — intermittently,
        # with a partial table and no error.
        failures.append(
            f"the run allowed {cap} dispatches, above the {naive} the walk costs written "
            f"carelessly but not above the {_OBSERVED_MAX_LOOKUPS} a live run has actually "
            "used. Neither arithmetic is a ceiling — the model writes the program — so a cap "
            "in this range truncates a later run rather than this one"
        )

    # Everything above grades the cap against the workload. This grades it against the run.
    # The cap bounds one `HostToolRun` and CodeAct builds a fresh one per `execute_code`, so
    # the route's ledger holds at most `cap` calls from each program that dispatched — not
    # `cap` in total. Above that is a ledger no run could have filled: the call past a
    # program's budget is refused before the tool body that records it runs.
    counted = {route: int(count) for route, count, _ in _TRIPS.findall(output)}
    # A missing or doubled line belongs to the assessment that owns it, not to this one.
    dispatching = _observed_programs(output)
    if _DISPATCH in counted and dispatching is not None and dispatching > 0:
        allowed = cap * dispatching
        if counted[_DISPATCH] > allowed:
            failures.append(
                f"the dispatched route recorded {counted[_DISPATCH]} lookup(s) across "
                f"{dispatching} program(s) that dispatched, where a cap of {cap} a program "
                f"allows at most {allowed}. The cap is per `execute_code` run and a refused "
                "call never reaches the tool body that records it, so this ledger is longer "
                "than the run could have made it"
            )
    return failures


def _assess_the_whole_walk_happened(output: str) -> list[str]:
    """All four stages, and the by-product table the last one exists for.

    A count is not enough: a program can take three stages, skip `product_name` and print both
    totals from the amounts alone, never touching the stage the comparison is about.
    """
    failures: list[str] = []
    stages, problems = _per_route(output, _STAGES_RUN, "lookup stages")
    failures.extend(problems)
    for route, match in stages.items():
        run, expected = int(match[1]), int(match[2])
        if expected != _STAGES:
            failures.append(f"{route} scored itself out of {expected} stages, not {_STAGES}")
        if run != expected:
            failures.append(
                f"{route} exercised {run} of {expected} lookup stages. The walk is the workload: "
                "a route that skipped one still prints state totals, because those are sums of "
                "the amounts, and measures a shorter chain than the one described"
            )

    named, problems = _per_route(output, _NAMED, "product names")
    failures.extend(problems)
    for route, match in named.items():
        found, expected = int(match[1]), int(match[2])
        if expected != _PRODUCTS:
            failures.append(f"{route} scored itself out of {expected} products, not {_PRODUCTS}")
        # Enforced on the dispatched route only, and the asymmetry is the sample's subject
        # rather than a loophole. There the model is never handed a product name, so a named
        # table can only have come from the program — which is what makes the fourth stage
        # visible. On the direct route the model holds the names from its own tool loop and
        # routinely labels the table in its reply while the program returns bare numbers;
        # measured at 0 of 3 on a healthy run. Requiring it there would fail a correct run for
        # doing the presentation in the place that route naturally does it.
        if route == _DISPATCH and found != expected:
            failures.append(
                f"the dispatched program's table names {found} of {expected} products. The model "
                "on that route never receives a product name, so the names can only come from "
                "the program — and a table without them is the fourth stage never having run"
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

    # The totals are sums, so they survive a table that lost its rows. These are the rows.
    cells, problems = _per_route(output, _CELLS_LINE, "product totals")
    failures.extend(problems)
    for route, match in cells.items():
        printed, expected = int(match[1]), int(match[2])
        if expected != _CELLS:
            failures.append(f"{route} scored itself out of {expected} product totals, not {_CELLS}")
        if printed != expected:
            failures.append(
                f"the {route}'s program printed {printed} of {expected} per-state, per-product "
                "totals. Both state totals can be right while a row underneath them is missing "
                "or wrong, because a total hides its terms — these are the table the task asked "
                "for, and they are what says the two routes reached the same answer"
            )

    # The cells are a multiset, so swapping the two states' figures leaves them intact. Rows
    # carry the association, and are required of the dispatched route for the same reason its
    # product names are: that model is never handed one, so a correctly labelled row can only
    # have come from the walk. The direct route's program prints figures its model then labels,
    # and has been measured printing no names at all, so there the rows are recorded.
    rows, problems = _per_route(output, _ROWS_LINE, "table rows")
    failures.extend(problems)
    for route, match in rows.items():
        printed, expected = int(match[1]), int(match[2])
        if expected != _CELLS:
            failures.append(f"{route} scored itself out of {expected} table rows, not {_CELLS}")
        if route == _DISPATCH and printed != expected:
            failures.append(
                f"the dispatched program printed {printed} of {expected} rows with the state and "
                "product attached. The six values can all be present and belong to the wrong "
                "rows — two states' figures swapped leaves the same numbers and the same two "
                "totals — so the labels are what say the table is the answer"
            )
    return failures


def _assess_direct_pays_per_stage(output: str) -> list[str]:
    """The structural comparison: batching within a stage, never across one."""
    found, failures = _per_route(output, _TRIPS, "round-trip count")
    shapes, problems = _per_route(output, _SHAPE, "tool calls per round")
    failures.extend(problems)

    if len(found) == len(_ROUTES):
        dispatched, direct = int(found[_DISPATCH][2]), int(found[_DIRECT][2])
        if direct <= dispatched:
            failures.append(
                f"the direct route took {direct} tool-calling round(s) and the dispatched "
                f"route {dispatched} — the comparison this sample exists for is that walking "
                "the stages in the model's own loop costs more of them, and this run did not "
                "show it"
            )
    for route, match in found.items():
        lookups = int(match[1])
        if lookups == 0:
            failures.append(f"{route} made no lookups at all, so it answered from somewhere else")
        elif lookups < _MINIMUM_LOOKUPS:
            # The walk is fixed, so its floor is arithmetic rather than a tolerance: two state
            # ids, two store lists, five stores' sales and three product names. A run under it
            # did not fetch what the table is made of, whichever route it was on.
            failures.append(
                f"{route} made {lookups} lookup(s) where the walk needs {_MINIMUM_LOOKUPS} at "
                "best — two state ids, two store lists, five stores' sales rows and three "
                "product names. A ledger this short cannot have produced the table above it"
            )

    for route, shape in shapes.items():
        groups = [g.strip() for g in shape[1].split(",") if g.strip()]
        # Read as counts before anything counts them. An entry is how many calls one message
        # asked for, so a shape of words has a length and means nothing, and a zero is a
        # message that was never an entry. Both routes: either length is read as a round count,
        # and the dispatched one is summed for the programs behind the round-trip summary.
        if not groups:
            # Refused before the rule below, which an empty list passes by having nothing to
            # break it, and it would then agree with a round count of zero. Both routes reach
            # the sandbox through `execute_code` and that is itself a tool call, so a route
            # with a table above it made at least one.
            failures.append(
                f"the {route} reports no tool calls at all, and its table came from an "
                "`execute_code` call, which is one. An empty shape describes a run that asked "
                "the model for nothing"
            )
        elif not all(entry.isdigit() and int(entry) > 0 for entry in groups):
            failures.append(
                f"the {route}'s shape [{', '.join(groups)}] is not a list of positive counts, "
                "so the rounds and programs it is supposed to describe cannot be read from it"
            )
        # Both numbers are the same list counted two ways, so a disagreement is not a finding
        # about the run — it is one of the two lines not describing it.
        if route in found and len(groups) != int(found[route][2]):
            failures.append(
                f"{route} reports {found[route][2]} tool-calling round(s) and a shape with "
                f"{len(groups)} entr(y/ies); the sample derives both from one list, so these "
                "cannot both be from this run"
            )
        if (
            route == _DISPATCH
            and all(entry.isdigit() for entry in groups)
            and any(int(entry) > 1 for entry in groups)
        ):
            failures.append(
                f"the dispatched route asked for {max(int(entry) for entry in groups)} tool call(s) "
                "in one message. Those programs can interleave in the ledger, so the observed "
                "run-boundary measurement would not describe consecutive programs"
            )
    if _DIRECT in shapes:
        groups = [g for g in shapes[_DIRECT][1].split(",") if g.strip()]
        # Length says how many times the model waited; the entries say what it asked for. On
        # this route every lookup is a tool call in the model's own loop, and the program that
        # printed the table is one more, so the shape holds at least one call the walk did not.
        if _DIRECT in found and all(g.strip().isdigit() for g in groups):
            calls, lookups = sum(int(g) for g in groups), int(found[_DIRECT][1])
            if calls <= lookups:
                failures.append(
                    f"the direct route's shape holds {calls} tool call(s) against {lookups} "
                    "lookup(s) and the `execute_code` that printed its table. Every one of "
                    "those is a call in the model's loop, so the shape is short by "
                    f"{lookups + 1 - calls} and is not the list the line above it was counted "
                    "from"
                )
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

    for route, (_, expected) in wrote.items():
        if expected != _FIGURES:
            failures.append(
                f"{route} scored itself out of {expected} sales figures where the dataset has "
                f"{_FIGURES} — a denominator the run chose is one the run can satisfy, and "
                "`0 of 0` agrees with itself on every line in this act"
            )

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
            [m for m in _RESTATED.findall(output) if m[0] == short],
            f"act 4 {short} restatement",
        )
        failures.extend(problems)
        if match is not None and int(match[2]) != _FIGURES:
            failures.append(
                f"act 4 scores the {route} out of {match[2]} sales figures where the dataset "
                f"has {_FIGURES}"
            )
        if match is not None and route in wrote and (int(match[1]), int(match[2])) != wrote[route]:
            failures.append(
                f"act 4 says the model wrote {match[1]} of {match[2]} figure(s) into code on "
                f"the {route} where the route itself reported {wrote[route][0]} of "
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
    if high <= 0:
        failures.append(
            "the round-trip summary is all zeroes. A dispatch is a file written, polled for and "
            "read back across the control plane, so zero is not a fast run — it is the "
            "measurement having disappeared, the same way a token count of zero is"
        )
    elif mid <= 0:
        failures.append(
            f"the median round trip is {mid}s over {gaps} gap(s), so at least half of them took "
            "no time at all, which a file round trip through the control plane cannot do"
        )

    # The ledger now classifies every gap from the observed HostToolRun identity. There are still
    # *n - 1* gaps, but no arithmetic or latency threshold decides which ones are boundaries.
    counted = {route: int(count) for route, count, _ in _TRIPS.findall(output)}
    dispatching = _observed_programs(output)
    shaped = dict(_SHAPE.findall(output))
    groups = [g.strip() for g in shaped.get(_DISPATCH, "").split(",") if g.strip()]
    if _DISPATCH in counted and dispatching is not None:
        lookups = counted[_DISPATCH]
        expected = lookups - dispatching
        if gaps != expected:
            failures.append(
                f"{gaps} round-trip gap(s) were measured across {lookups} dispatched lookup(s), "
                f"where the observer saw {dispatching} program(s) and run transitions leave "
                f"{expected} same-run gaps"
            )
    if groups and all(entry.isdigit() for entry in groups) and dispatching is not None:
        shaped_programs = sum(int(entry) for entry in groups)
        if shaped_programs != dispatching:
            failures.append(
                f"the tool-call shape describes {shaped_programs} program(s), but the observer "
                f"saw {dispatching}; the independent host record and model message shape disagree"
            )
    observed = [match for match in _BOUNDARIES.findall(output) if match[0] == _DISPATCH]
    direct_boundaries = [match for match in _BOUNDARIES.findall(output) if match[0] == _DIRECT]
    if direct_boundaries:
        failures.append("a program-boundary line was printed for the direct route")
    if len(observed) > 1:
        failures.append(
            f"the dispatch route reports program boundaries {len(observed)} times; exactly one "
            "boundary summary is required"
        )
    if len(observed) == 1 and dispatching is not None:
        _, count, smallest, largest = observed[0]
        expected = dispatching - 1
        if expected < 1:
            failures.append(
                f"the dispatch route reports a program boundary summary, but only {dispatching} "
                "program dispatched; a boundary requires at least two programs"
            )
        if int(count) != expected:
            failures.append(
                f"{count} program boundary/ies were observed where {dispatching} program(s) "
                f"dispatched, so run identity should produce {expected}"
            )
        if float(smallest) <= 0 or float(largest) <= 0 or float(smallest) > float(largest):
            failures.append(
                f"observed boundary times {smallest}s and {largest}s are not positive and ordered"
            )
    elif dispatching is not None and dispatching > 1:
        failures.append(
            f"{dispatching} program(s) dispatched and no program boundary was reported; "
            "the observer-derived run identities must be visible in the measurement"
        )
    if not low <= mid <= high:
        failures.append(
            f"min {low}s, median {mid}s and max {high}s are not ordered — whatever produced "
            "these did not produce them from one set of samples"
        )
    return failures


def _reclaims(output: str) -> bool | None:
    """Whether this run's transport takes its own files back, or None if it did not say."""
    said = _CLEANUP.findall(output)
    return said[0].startswith("reclaimed") if len(said) == 1 else None


def _call_reclaims(output: str) -> bool | None:
    """Whether the framework reclaims CodeAct call directories, if the run reports it."""
    said = _CALL_CLEANUP.findall(output)
    return said[0].startswith("reclaimed") if len(said) == 1 else None


def _observed_programs(output: str) -> int | None:
    """How many distinct `HostToolRun` identities the host observer saw dispatch."""
    seen = _OBSERVED_PROGRAMS.findall(output)
    dispatch = [match for match in seen if match[0] == _DISPATCH]
    direct = [match for match in seen if match[0] == _DIRECT]
    if len(dispatch) != 1 or direct:
        return None
    return int(dispatch[0][1])


def _assess_observed_program_count(output: str) -> list[str]:
    """Require one observer count for the dispatched route and none for the direct route."""
    seen = _OBSERVED_PROGRAMS.findall(output)
    dispatch = [match for match in seen if match[0] == _DISPATCH]
    direct = [match for match in seen if match[0] == _DIRECT]
    failures: list[str] = []
    if len(dispatch) == 0:
        failures.append("no tagged dispatch-route 'programs that dispatched' line was reported")
    elif len(dispatch) > 1:
        failures.append(
            f"the dispatch route reports 'programs that dispatched' {len(dispatch)} times; "
            "exactly one observer count is required"
        )
    if direct:
        failures.append(
            "the direct route reports 'programs that dispatched'; only the dispatch route has "
            "an observer count"
        )
    return failures


def _assess_what_the_runs_left(output: str) -> list[str]:
    """#302's per-run subdirectory, and what became of the traffic in it.

    Two transports and two right answers: files kept are the run's traffic, files reclaimed mean
    zero is the cleanup working. The run says which it measured and this grades that one.
    """
    failures: list[str] = []
    said = _CLEANUP.findall(output)
    _, problems = _once(said, "transport cleanup")
    failures.extend(problems)
    call_said = _CALL_CLEANUP.findall(output)
    call_reclaims, problems = _once(call_said, "call directory cleanup")
    failures.extend(problems)
    dirs, problems = _once(_RUN_DIRS.findall(output), "run directories")
    failures.extend(problems)
    dispatching, problems = _once(_DISPATCHING.findall(output), "runs that dispatched")
    failures.extend(problems)
    left, problems = _once(_LEFT.findall(output), "files left behind")
    failures.extend(problems)
    if None in (dirs, dispatching, left) or len(said) != 1 or call_reclaims is None:
        return failures
    total, answers = int(left[0]), int(left[1])  # type: ignore[index]

    if call_reclaims.startswith("reclaimed"):
        if int(dirs) != 0 or int(dispatching) != 0:  # type: ignore[arg-type]
            failures.append(
                f"{dirs} run director(y/ies), including {dispatching} dispatched run(s), survived "
                "framework reclamation of CodeAct call directories"
            )
        if total or answers:
            failures.append(
                f"{total} transport file(s) and {answers} answered call(s) survived after the "
                "framework reclaimed every CodeAct call directory"
            )
        return failures

    if _reclaims(output):
        # Nothing is a measurement here, so it has to be exactly nothing: this transport removes
        # the whole directory it owns, and a run that left some of it behind either did not
        # reclaim or reclaimed part. The runs themselves are the kind's and stay — they are what
        # still says the programs ran, and there has to be one more of them than dispatched,
        # because the direct route's program is in the other sandbox.
        shaped = dict(_SHAPE.findall(output))
        groups = [g.strip() for g in shaped.get(_DISPATCH, "").split(",") if g.strip()]
        programs = sum(int(g) for g in groups) if groups and all(g.isdigit() for g in groups) else 0
        if int(dispatching) != 0:  # type: ignore[arg-type]
            failures.append(
                f"{dispatching} run(s) still hold a transport directory, and this transport "
                "removes the one it owns on every exit path. Either the cleanup did not run or "
                "the enumeration is counting something the transport does not write"
            )
        if (total, answers) != (0, 0):
            failures.append(
                f"{total} transport file(s) and {answers} answered call(s) survived a transport "
                "that reclaims them. The requests and responses go with the directory holding "
                "them, so anything left is the cleanup having half worked, which is worse than "
                "not running: the next run in this sandbox can read it"
            )
        if programs and int(dirs) <= programs:  # type: ignore[arg-type]
            failures.append(
                f"{dirs} run director(y/ies) in the guest against {programs} dispatched "
                "program(s), and the direct route's program leaves one more in the other "
                "sandbox. The traffic is reclaimed now, so these directories are the only "
                "thing left saying the programs ran at all"
            )
        return failures

    if int(dispatching) < 1:  # type: ignore[arg-type]
        failures.append("no run dispatched, so there is no transport traffic to have left behind")
    if int(dirs) < int(dispatching):  # type: ignore[arg-type]
        failures.append(
            f"{dispatching} run(s) dispatched out of {dirs} in the guest, which is not arithmetic"
        )
    elif int(dirs) == int(dispatching):  # type: ignore[arg-type]
        # The direct route runs its program in a sandbox built with no registry, so its run
        # directory has no transport in it and is counted here but not there. A run that
        # reaches this act has one — its table came from an `execute_code` — so a count with
        # none of them in it is an enumeration that read one sandbox and reported both.
        failures.append(
            f"all {dirs} run directories dispatched, so none of them is the direct route's. "
            "That program runs in a sandbox with no registry and leaves a directory with no "
            "transport in it, and its table above says it ran — so this count is one sandbox "
            "short of what it claims to cover"
        )
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

    # Two floors the transport cannot go under, so a broken enumeration cannot pass as traffic.
    if total < 3 * answers:
        failures.append(
            f"{total} transport file(s) hold {answers} answered call(s), where a served call "
            f"leaves three — the id its caller claimed, the request and the answer — so "
            f"{3 * answers} is the floor. Below it the enumeration found less than it counted"
        )
    dispatched = {route: int(count) for route, count, _ in _TRIPS.findall(output)}
    if _DISPATCH in dispatched and answers < dispatched[_DISPATCH]:
        failures.append(
            f"the guest holds {answers} answered call(s) against the {dispatched[_DISPATCH]} "
            "lookup(s) the dispatched route recorded. The host answers every call it serves and "
            "nothing deletes the answer, so fewer answers than lookups is the enumeration "
            "having missed part of the traffic rather than the run having made less of it"
        )
    return failures


def _assess_the_sandbox_went_away(output: str) -> list[str]:
    """Billable, and there are two — a sandbox per route, both read by act 5 and both gone."""
    match, failures = _once(_DISPOSED.findall(output), "Disposed")
    if match is None:
        return failures
    if int(match) != _SANDBOXES:
        failures.append(
            f"{match} sandbox(es) disposed where the sample acquires {_SANDBOXES}, one per "
            "route so neither can read the other's leftovers — a sandbox this "
            "sample leaves behind bills until the lifecycle timers reach it, and it is also "
            "the only thing that removes the files act 5 counted"
        )
    return failures


def _assess_the_cost_was_measured(output: str) -> list[str]:
    """Each route publishes a cost, and the token half of it is a real figure.

    Wall clock is never bounded above — a threshold would make this a pass mark on somebody
    else's control plane. Zero is the exception at both ends: a model call over the network and a
    program in a microVM cannot take no time and no tokens.
    """
    found, failures = _per_route(output, _COST, "cost")
    for route, match in found.items():
        if float(match[1]) <= 0:
            failures.append(
                f"{route} reports {match[1]}s of wall clock. Every route here waits on a model "
                "and on a sandbox, so zero is the clock never having been read rather than a "
                "run that took no time"
            )
        if int(match[2]) == 0:
            failures.append(
                f"{route} reports 0 tokens. Every route here invokes the model, so this is a "
                "run whose usage never arrived rather than one that was free, and publishing "
                "it as a measurement is what this check exists to stop"
            )
    return failures


def assess(output: str) -> list[str]:
    """Every reason the run does not show a real, measured, call-heavy dispatch."""
    return [
        *_assess_the_cap_was_budgeted(output),
        *_assess_the_cost_was_measured(output),
        *_assess_both_interpreters_answered(output),
        *_assess_the_whole_walk_happened(output),
        *_assess_observed_program_count(output),
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
        f"{trips.get(_DIRECT, '?')} tool-calling rounds in the model's loop against "
        f"{trips.get(_DISPATCH, '?')} dispatched, and the model wrote no data into code on "
        "the second"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
