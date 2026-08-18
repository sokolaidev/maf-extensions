"""What `scripts/check_live_host_tools_dispatch_sample.py` will and will not let through.

`_HEALTHY` is a real run of `samples/15_acas_codeact_host_tools` against a live ACAS sandbox
and a live model, verbatim apart from the two tables the model produced.

The suite is organised around what the check is *allowed* to fail a release for. Thirteen live
runs went into choosing that: the figures below moved between them — 18 to 29 lookups, 35s to
87s, two to four dispatched tool-calling rounds — and what did not move is what is asserted.

What each test pins is the property, not the run that produced it: an assertion keyed on a
model's prose, a totals matcher blind to a thousands separator, or an enumeration reading the
wrong guest directory would each pass a happy-path suite and fail a real one.
"""

from __future__ import annotations

import importlib.util
import io
import sys
from pathlib import Path

import pytest

_SCRIPT = (
    Path(__file__).resolve().parent.parent / "scripts" / "check_live_host_tools_dispatch_sample.py"
)
_spec = importlib.util.spec_from_file_location("check_dispatch", _SCRIPT)
assert _spec and _spec.loader
check = importlib.util.module_from_spec(_spec)
sys.modules["check_dispatch"] = check
_spec.loader.exec_module(check)


#: A real run. The two summary tables are the model's own output and the only edited lines.
_HEALTHY = """== 1. What the host wired ==

  registered:                  state_id, stores_in_state, store_sales, product_name
  [measured] dispatch cap for the run: 32 (the walk needs 12 at best, 21 written naively)
  identities the spec carries: ['app']

== 2. The lookups happen inside the sandbox ==

state	product	total_sales
Washington	TOTAL	3564.55
Oregon	TOTAL	3514.35

  [measured] dispatch route: 25 lookup(s) over 2 tool-calling round(s)
  [measured] dispatch route: tool calls per round: [1, 1]
  [measured] dispatch route: lookup stages exercised: 4 of 4 (product_name, state_id, store_sales, stores_in_state)
  [measured] dispatch route: 42.40s, 3616 tokens (in 2960, cached 1024, out 656)
  [measured] dispatch route: state totals the program printed: 2 of 2
  [measured] dispatch route: product totals the program printed: 6 of 6
  [measured] dispatch route: product names in the table: 3 of 3
  [measured] dispatch route: sales figures the model wrote into code: 0 of 12
  [measured] dispatch route: round trip: 23 gap(s), min 1.08s, median 1.11s, max 1.60s

== 3. The lookups happen in the model's tool loop ==

| Washington | TOTAL | 3564.55 |
| Oregon | TOTAL | 3514.35 |

  [measured] direct route: 12 lookup(s) over 5 tool-calling round(s)
  [measured] direct route: tool calls per round: [2, 2, 5, 3, 1]
  [measured] direct route: lookup stages exercised: 4 of 4 (product_name, state_id, store_sales, stores_in_state)
  [measured] direct route: 14.60s, 6217 tokens (in 5559, cached 2048, out 658)
  [measured] direct route: state totals the program printed: 2 of 2
  [measured] direct route: product totals the program printed: 6 of 6
  [measured] direct route: product names in the table: 0 of 3
  [measured] direct route: sales figures the model wrote into code: 12 of 12

== 4. What the round trips bought ==

  [measured] sales figures the model wrote into code, dispatched: 0 of 12
  [measured] sales figures the model wrote into code, direct:     12 of 12

== 5. What the runs left in the guest ==

  [measured] run directories across both sandboxes: 3
  [measured] of those, runs that dispatched: 2
  [measured] transport files left behind: 75, of which answered calls: 25

  [measured] Disposed 2 sandbox(es).
"""


def _without(line: str) -> str:
    kept = [row for row in _HEALTHY.splitlines() if line not in row]
    assert len(kept) < len(_HEALTHY.splitlines()), f"nothing to remove for {line!r}"
    return "\n".join(kept)


def _swap(old: str, new: str) -> str:
    assert old in _HEALTHY, f"{old!r} is not in the fixture"
    return _HEALTHY.replace(old, new)


class TestAHealthyRun:
    def test_it_passes(self):
        assert check.assess(_HEALTHY) == []


class TestBothProgramsHadToAnswer:
    """Both routes compute in the sandbox, so neither gets a pass on the totals."""

    @pytest.mark.parametrize("route", ["dispatch route", "direct route"])
    def test_a_program_that_printed_one_total_fails(self, route: str):
        broken = _swap(
            f"[measured] {route}: state totals the program printed: 2 of 2",
            f"[measured] {route}: state totals the program printed: 1 of 2",
        )
        assert any("did not finish the walk" in r for r in check.assess(broken))

    @pytest.mark.parametrize("route", ["dispatch route", "direct route"])
    def test_a_missing_totals_line_fails(self, route: str):
        assert check.assess(_without(f"{route}: state totals")) != []

    @pytest.mark.parametrize("route", ["dispatch route", "direct route"])
    def test_a_table_missing_a_row_fails(self, route: str):
        """Both state totals can be right while a row underneath one of them is gone.

        The totals are sums. A program that dropped Oregon's Gasket row and printed a total
        computed before it — or after it, from the same sales rows — satisfies `2 of 2` and
        answers a different question from the one asked.
        """
        broken = _swap(
            f"[measured] {route}: product totals the program printed: 6 of 6",
            f"[measured] {route}: product totals the program printed: 5 of 6",
        )
        assert any("hides its terms" in r for r in check.assess(broken))

    @pytest.mark.parametrize("route", ["dispatch route", "direct route"])
    def test_a_table_scored_out_of_its_own_number_fails(self, route: str):
        broken = _swap(
            f"[measured] {route}: product totals the program printed: 6 of 6",
            f"[measured] {route}: product totals the program printed: 4 of 4",
        )
        assert any("not 6" in r for r in check.assess(broken))

    @pytest.mark.parametrize("route", ["dispatch route", "direct route"])
    def test_a_missing_product_totals_line_fails(self, route: str):
        assert check.assess(_without(f"{route}: product totals")) != []


class TestDirectPaysPerStage:
    """The structural comparison, and the reason the workload has four stages."""

    def test_direct_needing_no_more_round_trips_fails(self):
        assert any(
            "did not show it" in r
            for r in check.assess(
                _swap(
                    "[measured] direct route: 12 lookup(s) over 5 tool-calling round(s)",
                    "[measured] direct route: 12 lookup(s) over 2 tool-calling round(s)",
                )
            )
        )

    def test_a_collapsed_direct_shape_fails(self):
        """One batch means the stages stopped depending on each other."""
        assert any(
            "stages" in r
            for r in check.assess(
                _swap(
                    "[measured] direct route: tool calls per round: [2, 2, 5, 3, 1]",
                    "[measured] direct route: tool calls per round: [12]",
                )
            )
        )

    @pytest.mark.parametrize("route", ["dispatch route", "direct route"])
    def test_a_route_that_made_no_lookups_fails(self, route: str):
        line = [r for r in _HEALTHY.splitlines() if f"{route}: " in r and "lookup(s) over" in r][0]
        broken = _swap(line, line.replace("25 lookup", "0 lookup").replace("12 lookup", "0 lookup"))
        assert any("no lookups at all" in r for r in check.assess(broken))

    @pytest.mark.parametrize(("route", "count"), [("dispatch route", 25), ("direct route", 12)])
    def test_a_route_below_the_minimum_walk_fails(self, route: str, count: int):
        """Zero is not the only count that cannot have produced the table.

        The walk is fixed at twelve lookups — two state ids, two store lists, five stores'
        sales and three product names — so eleven is a run that fetched less than the answer is
        made of. The direct route sits *on* that floor in a healthy run, which is what makes it
        a floor rather than a margin.
        """
        broken = _swap(
            f"[measured] {route}: {count} lookup(s) over",
            f"[measured] {route}: 11 lookup(s) over",
        )
        assert any("cannot have produced the table" in r for r in check.assess(broken))

    def test_more_dispatched_round_trips_than_measured_is_fine(self):
        """Two to four was the live range; the check bounds the comparison, not the value.

        Every line a third program would move has to move with it — the shape, the gap count,
        the run directories, and the traffic those runs left. A variant that raises the lookups
        alone is not a slower healthy run, it is a run that cannot have happened.
        """
        assert (
            check.assess(
                _swap(
                    "[measured] dispatch route: 25 lookup(s) over 2 tool-calling round(s)",
                    "[measured] dispatch route: 29 lookup(s) over 3 tool-calling round(s)",
                )
                .replace("round trip: 23 gap(s)", "round trip: 26 gap(s)")
                .replace(
                    "dispatch route: tool calls per round: [1, 1]",
                    "dispatch route: tool calls per round: [1, 1, 1]",
                )
                .replace("run directories across both sandboxes: 3", "…dirs…")
                .replace("of those, runs that dispatched: 2", "of those, runs that dispatched: 3")
                .replace("…dirs…", "run directories across both sandboxes: 4")
                .replace(
                    "transport files left behind: 75, of which answered calls: 25",
                    "transport files left behind: 87, of which answered calls: 29",
                )
            )
            == []
        )


class TestTwoViewsOfOneListHaveToAgree:
    """The trip count and the shape are the same list counted twice."""

    @pytest.mark.parametrize(
        ("route", "shape"),
        [("dispatch route", "[1, 1]"), ("direct route", "[2, 2, 5, 3, 1]")],
    )
    def test_a_shape_shorter_than_the_trip_count_fails(self, route: str, shape: str):
        trimmed = ", ".join(shape.strip("[]").split(", ")[:-1])
        assert any(
            "cannot both be from this run" in r
            for r in check.assess(
                _swap(
                    f"[measured] {route}: tool calls per round: {shape}",
                    f"[measured] {route}: tool calls per round: [{trimmed}]",
                )
            )
        )


class TestTheCapIsJudgedAgainstTheWorkload:
    """The default fits the efficient program and truncates the careless one."""

    def test_the_registry_default_fails(self):
        assert any(
            "only fits the efficient program" in r
            for r in check.assess(
                _swap(
                    "[measured] dispatch cap for the run: 32 (the walk needs 12",
                    "[measured] dispatch cap for the run: 16 (the walk needs 12",
                )
            )
        )

    def test_a_cap_at_the_naive_figure_fails(self):
        assert any(
            "only fits the efficient program" in r
            for r in check.assess(
                _swap(
                    "[measured] dispatch cap for the run: 32 (the walk needs 12",
                    "[measured] dispatch cap for the run: 21 (the walk needs 12",
                )
            )
        )

    def test_a_cap_below_the_minimum_still_reports_the_truncation(self):
        reasons = check.assess(
            _swap(
                "[measured] dispatch cap for the run: 32 (the walk needs 12",
                "[measured] dispatch cap for the run: 8 (the walk needs 12",
            )
        )
        assert any("cannot finish" in r for r in reasons)


class TestTheDenominatorIsNotTheRunsToChoose:
    """A self-consistent output agrees with itself; the dataset is what settles it."""

    def test_zero_of_zero_on_every_line_fails(self):
        """Direct has carried == expected, dispatch has zero, and act 4 agrees with both."""
        zeroed = _HEALTHY
        for old, new in (
            (
                "dispatch route: sales figures the model wrote into code: 0 of 12",
                "dispatch route: sales figures the model wrote into code: 0 of 0",
            ),
            (
                "direct route: sales figures the model wrote into code: 12 of 12",
                "direct route: sales figures the model wrote into code: 0 of 0",
            ),
            (
                "sales figures the model wrote into code, dispatched: 0 of 12",
                "sales figures the model wrote into code, dispatched: 0 of 0",
            ),
            (
                "sales figures the model wrote into code, direct:     12 of 12",
                "sales figures the model wrote into code, direct:     0 of 0",
            ),
        ):
            zeroed = zeroed.replace(old, new)
        reasons = check.assess(zeroed)
        # Two branches police the denominator — the route's own line and act 4's restatement of
        # it — and both say "the dataset has 12". Asserting the shared phrase lets either one
        # cover for the other, so each is named by the wording only it produces.
        assert any("scored itself out of 0 sales figures" in r for r in reasons)
        assert any("act 4 scores the" in r for r in reasons)

    def test_a_smaller_but_consistent_dataset_fails(self):
        """`7 of 7` is internally sound and describes a dataset this sample does not have."""
        shrunk = _HEALTHY
        for old, new in (
            (
                "direct route: sales figures the model wrote into code: 12 of 12",
                "direct route: sales figures the model wrote into code: 7 of 7",
            ),
            (
                "sales figures the model wrote into code, direct:     12 of 12",
                "sales figures the model wrote into code, direct:     7 of 7",
            ),
        ):
            shrunk = shrunk.replace(old, new)
        reasons = check.assess(shrunk)
        assert any("scored itself out of 7 sales figures" in r for r in reasons)
        assert any("act 4 scores the" in r for r in reasons)


class TestAMissingMeasurementIsNotAMeasurement:
    def test_a_token_count_of_zero_fails(self):
        """Every route here invokes the model, so nought is usage missing, not a free run."""
        assert any(
            "0 tokens" in r
            for r in check.assess(
                _swap(
                    "[measured] dispatch route: 42.40s, 3616 tokens",
                    "[measured] dispatch route: 42.40s, 0 tokens",
                )
            )
        )

    def test_a_token_count_of_none_fails(self):
        """Usage reporting disappearing is a run that measured nothing, not a passing one."""
        assert (
            check.assess(
                _swap(
                    "[measured] dispatch route: 42.40s, 3616 tokens",
                    "[measured] dispatch route: 42.40s, None tokens",
                )
            )
            != []
        )


class TestTheWholeWalkHappened:
    """A count and a shape do not say the fourth stage ran."""

    @pytest.mark.parametrize("route", ["dispatch route", "direct route"])
    def test_a_route_that_skipped_a_stage_fails(self, route: str):
        """State totals are sums of amounts, so a program can skip product_name and look whole."""
        assert any(
            "measures a shorter chain" in r
            for r in check.assess(
                _swap(
                    f"[measured] {route}: lookup stages exercised: 4 of 4 "
                    "(product_name, state_id, store_sales, stores_in_state)",
                    f"[measured] {route}: lookup stages exercised: 3 of 4 "
                    "(state_id, store_sales, stores_in_state)",
                )
            )
        )

    @pytest.mark.parametrize("route", ["dispatch route", "direct route"])
    def test_a_missing_stages_line_fails(self, route: str):
        assert check.assess(_without(f"{route}: lookup stages exercised")) != []

    def test_a_dispatched_table_without_product_names_fails(self):
        """The model on that route never sees a name, so an unnamed table is a skipped stage."""
        assert any(
            "never receives a product name" in r
            for r in check.assess(
                _swap(
                    "[measured] dispatch route: product names in the table: 3 of 3",
                    "[measured] dispatch route: product names in the table: 0 of 3",
                )
            )
        )

    def test_the_direct_table_naming_nothing_is_fine(self):
        """Measured at 0 of 3 on a healthy run: that model labels in its reply, not its program."""
        assert check.assess(_HEALTHY) == []


class TestTheWorkloadArithmeticIsPinned:
    """The cap is graded against these two figures, so the run cannot supply them."""

    def test_a_run_describing_a_smaller_walk_fails(self):
        assert any(
            "makes the grade its own" in r
            for r in check.assess(
                _swap(
                    "[measured] dispatch cap for the run: 32 (the walk needs 12 at best, 21 written naively)",
                    "[measured] dispatch cap for the run: 32 (the walk needs 2 at best, 12 written naively)",
                )
            )
        )

    def test_a_cap_at_the_registry_default_fails(self):
        assert any(
            "the registry allows by default" in r
            for r in check.assess(
                _swap(
                    "[measured] dispatch cap for the run: 32 (the walk",
                    "[measured] dispatch cap for the run: 16 (the walk",
                )
            )
        )


class TestWhoCarriedTheFigures:
    def test_the_dispatched_route_writing_a_figure_fails(self):
        broken = _swap(
            "[measured] dispatch route: sales figures the model wrote into code: 0 of 12",
            "[measured] dispatch route: sales figures the model wrote into code: 4 of 12",
        ).replace(
            "[measured] sales figures the model wrote into code, dispatched: 0 of 12",
            "[measured] sales figures the model wrote into code, dispatched: 4 of 12",
        )
        assert any("before any dispatch can answer" in r for r in check.assess(broken))

    def test_the_direct_route_writing_none_fails(self):
        broken = _swap(
            "[measured] direct route: sales figures the model wrote into code: 12 of 12",
            "[measured] direct route: sales figures the model wrote into code: 0 of 12",
        ).replace(
            "[measured] sales figures the model wrote into code, direct:     12 of 12",
            "[measured] sales figures the model wrote into code, direct:     0 of 12",
        )
        assert any("not the comparison this sample makes" in r for r in check.assess(broken))

    def test_act_four_has_to_agree_on_the_denominator_too(self):
        """`12 of 7` against `12 of 12` agrees on the numerator and describes another dataset."""
        assert any(
            "disagree" in r
            for r in check.assess(
                _swap(
                    "[measured] sales figures the model wrote into code, direct:     12 of 12",
                    "[measured] sales figures the model wrote into code, direct:     12 of 7",
                )
            )
        )

    def test_act_four_has_to_agree(self):
        assert any(
            "disagree" in r
            for r in check.assess(
                _swap(
                    "[measured] sales figures the model wrote into code, direct:     12 of 12",
                    "[measured] sales figures the model wrote into code, direct:     7 of 12",
                )
            )
        )

    def test_a_missing_restatement_fails(self):
        assert any(
            "act 4 dispatched restatement" in r
            for r in check.assess(_without("sales figures the model wrote into code, dispatched"))
        )


class TestTheRunsLeftTheirTrafficBehind:
    """#302's per-run subdirectory, and the cleanup #438 says nobody can do."""

    def test_no_files_left_behind_fails(self):
        """Zero means the sample looked in the wrong place, which is what it did."""
        assert any(
            "does not write" in r
            for r in check.assess(
                _swap(
                    "[measured] transport files left behind: 75, of which answered calls: 25",
                    "[measured] transport files left behind: 0, of which answered calls: 0",
                )
            )
        )

    def test_no_run_dispatching_fails(self):
        assert any(
            "no transport traffic" in r
            for r in check.assess(
                _swap(
                    "[measured] of those, runs that dispatched: 2",
                    "[measured] of those, runs that dispatched: 0",
                )
            )
        )

    def test_files_but_no_answered_call_fails(self):
        """Litter with no answer in it means nothing in the guest records a call being served."""
        assert any(
            "records a dispatch having been served" in r
            for r in check.assess(
                _swap(
                    "[measured] transport files left behind: 75, of which answered calls: 25",
                    "[measured] transport files left behind: 75, of which answered calls: 0",
                )
            )
        )

    def test_fewer_files_than_three_per_answer_fails(self):
        """A served call leaves three files, so 25 answers cannot sit in 25 files."""
        assert any(
            "is the floor" in r
            for r in check.assess(
                _swap(
                    "[measured] transport files left behind: 75, of which answered calls: 25",
                    "[measured] transport files left behind: 25, of which answered calls: 25",
                )
            )
        )

    def test_fewer_answers_than_lookups_fails(self):
        """25 lookups the route recorded cannot have left one answer in the guest."""
        assert any(
            "missed part of the traffic" in r
            for r in check.assess(
                _swap(
                    "[measured] transport files left behind: 75, of which answered calls: 25",
                    "[measured] transport files left behind: 75, of which answered calls: 1",
                )
            )
        )

    def test_more_answers_than_files_fails(self):
        assert any(
            "not arithmetic" in r
            for r in check.assess(
                _swap(
                    "[measured] transport files left behind: 75, of which answered calls: 25",
                    "[measured] transport files left behind: 5, of which answered calls: 25",
                )
            )
        )

    def test_more_dispatching_runs_than_directories_fails(self):
        assert any(
            "not arithmetic" in r
            for r in check.assess(
                _swap(
                    "[measured] of those, runs that dispatched: 2",
                    "[measured] of those, runs that dispatched: 9",
                )
            )
        )

    @pytest.mark.parametrize(
        "line",
        [
            "run directories across both sandboxes",
            "of those, runs that dispatched",
            "transport files left behind",
        ],
    )
    def test_each_line_is_required(self, line: str):
        assert check.assess(_without(line)) != []


class TestACountIsNotAMatch:
    """Holes a length check or a positive-count check leaves open.

    Each of these passes a naive implementation: the totals agree, the counts are non-zero and
    the number of lines is right, while the thing being counted is the wrong thing.
    """

    def test_a_partial_direct_count_fails(self):
        """1-11 of 12 is not "the model carried the data", it is an unmeasured road."""
        broken = _swap(
            "[measured] direct route: sales figures the model wrote into code: 12 of 12",
            "[measured] direct route: sales figures the model wrote into code: 7 of 12",
        ).replace(
            "[measured] sales figures the model wrote into code, direct:     12 of 12",
            "[measured] sales figures the model wrote into code, direct:     7 of 12",
        )
        assert any("7 of 12" in r for r in check.assess(broken))

    def test_a_positive_but_inconsistent_gap_count_fails(self):
        """21 lookups yield exactly 20 gaps; one gap is a median over a twentieth of the run."""
        assert any(
            "different set of calls" in r
            for r in check.assess(_swap("round trip: 23 gap(s)", "round trip: 1 gap(s)"))
        )

    def test_two_restatements_of_one_route_and_none_of_the_other_fails(self):
        """Both are two lines, so a length check passes while act 4 omits half the comparison."""
        doubled = _without("sales figures the model wrote into code, dispatched").replace(
            "  [measured] sales figures the model wrote into code, direct:     12 of 12",
            "  [measured] sales figures the model wrote into code, direct:     12 of 12\n"
            "  [measured] sales figures the model wrote into code, direct:     12 of 12",
        )
        reasons = check.assess(doubled)
        assert any("act 4 dispatched restatement" in r for r in reasons)
        assert any("appears 2 times" in r for r in reasons)


class TestTheRoundTripLine:
    def test_an_unordered_summary_fails(self):
        assert any(
            "not ordered" in r
            for r in check.assess(
                _swap("min 1.08s, median 1.11s, max 1.60s", "min 2.00s, median 1.11s, max 1.60s")
            )
        )

    def test_a_round_trip_line_for_the_direct_route_fails(self):
        """Its lookups run in the host process; whatever that measured is not a round trip."""
        forged = _HEALTHY.replace(
            "== 4. What the round trips bought ==",
            "  [measured] direct route: round trip: 11 gap(s), min 0.00s, median 0.00s, max 2.14s\n"
            "== 4. What the round trips bought ==",
        )
        assert any("not a round trip" in r for r in check.assess(forged))

    def test_a_missing_round_trip_line_fails(self):
        assert any("round trip" in r for r in check.assess(_without("round trip:")))

    def test_a_gap_count_the_guest_contradicts_fails(self):
        """The boundary count is the guest's, not the emitter's arithmetic about itself."""
        assert any(
            "different set of calls from the one the guest" in r
            for r in check.assess(_swap("runs that dispatched: 2", "runs that dispatched: 3"))
        )

    def test_a_program_that_dispatched_nothing_fails(self):
        """A probe program has no boundary, so dropping a gap for it drops a real round trip.

        Three `execute_code` calls where the guest holds two run directories that dispatched:
        the emitter drops two gaps for two boundaries that are not both there, and the largest
        genuine transport gap goes with them. Nothing in the summary looks wrong afterwards,
        which is why the guest has to be asked.
        """
        probed = _swap(
            "[measured] dispatch route: 25 lookup(s) over 2 tool-calling round(s)",
            "[measured] dispatch route: 25 lookup(s) over 3 tool-calling round(s)",
        ).replace(
            "dispatch route: tool calls per round: [1, 1]",
            "dispatch route: tool calls per round: [1, 1, 1]",
        )
        assert any("have to agree" in r for r in check.assess(probed))

    def test_a_dispatched_shape_that_is_not_counts_fails(self):
        """The program count comes from the shape, so a shape that is not numbers has to say so."""
        assert any(
            "not a list of counts" in r
            for r in check.assess(
                _swap(
                    "dispatch route: tool calls per round: [1, 1]",
                    "dispatch route: tool calls per round: [1, one]",
                )
            )
        )

    def test_an_all_zero_summary_fails(self):
        """Ordered, and every figure zero: the measurement disappeared rather than went fast."""
        assert any(
            "all zeroes" in r
            for r in check.assess(
                _swap("min 1.08s, median 1.11s, max 1.60s", "min 0.00s, median 0.00s, max 0.00s")
            )
        )

    def test_a_zero_median_fails(self):
        """One real maximum does not rescue a median of zero across twenty-three gaps."""
        assert any(
            "no time at all" in r
            for r in check.assess(
                _swap("min 1.08s, median 1.11s, max 1.60s", "min 0.00s, median 0.00s, max 1.60s")
            )
        )


class TestTheCapWasBudgeted:
    def test_a_cap_below_the_minimum_fails(self):
        assert any(
            "cannot finish" in r
            for r in check.assess(
                _swap(
                    "[measured] dispatch cap for the run: 32 (the walk needs 12",
                    "[measured] dispatch cap for the run: 8 (the walk needs 12",
                )
            )
        )

    def test_a_missing_cap_line_fails(self):
        assert any("dispatch cap" in r for r in check.assess(_without("dispatch cap")))


class TestWhatIsRecordedAndNeverBounded:
    """A slow control plane, an expensive run or a chatty program is a finding, not a grade."""

    def test_a_very_slow_expensive_run_passes(self):
        assert (
            check.assess(
                _swap(
                    "[measured] dispatch route: 42.40s, 3616 tokens (in 2960, cached 1024, out 656)",
                    "[measured] dispatch route: 240.00s, 99999 tokens (in 99000, cached 0, out 999)",
                )
            )
            == []
        )

    @pytest.mark.parametrize(
        ("route", "seconds"), [("dispatch route", "42.40s"), ("direct route", "14.60s")]
    )
    def test_a_route_that_took_no_time_fails(self, route: str, seconds: str):
        """Nonzero tokens do not rescue a clock that was never read."""
        assert any(
            "the clock never having been read" in r
            for r in check.assess(
                _swap(f"[measured] {route}: {seconds},", f"[measured] {route}: 0.00s,")
            )
        )

    def test_a_chatty_program_passes(self):
        """Same two programs, four more lookups — and four more answers in the guest."""
        assert (
            check.assess(
                _swap(
                    "[measured] dispatch route: 25 lookup(s) over 2 tool-calling round(s)",
                    "[measured] dispatch route: 29 lookup(s) over 2 tool-calling round(s)",
                )
                .replace("round trip: 23 gap(s)", "round trip: 27 gap(s)")
                .replace(
                    "transport files left behind: 75, of which answered calls: 25",
                    "transport files left behind: 87, of which answered calls: 29",
                )
            )
            == []
        )


class TestAModelCannotAnswerForTheHost:
    """#314: the tag at the left margin is the boundary between what ran and what was said."""

    def test_an_untagged_line_is_not_read(self):
        forged = _HEALTHY.replace(
            "== 5. What the runs left in the guest ==",
            "direct route: sales figures the model wrote into code: 0\n"
            "== 5. What the runs left in the guest ==",
        )
        assert check.assess(forged) == []

    def test_a_quoted_tagged_line_cannot_stand_in_for_the_real_one(self):
        """`quoted()` prefixes a model's tagged line with `> `, which the anchor rejects."""
        forged = _without("dispatch route: sales figures the model wrote into code").replace(
            "== 4. What the round trips bought ==",
            "> [measured] dispatch route: sales figures the model wrote into code: 0 of 12\n"
            "== 4. What the round trips bought ==",
        )
        assert any("figures written" in r for r in check.assess(forged))

    def test_a_second_copy_of_a_line_is_refused_rather_than_resolved(self):
        doubled = _swap(
            "  [measured] Disposed 2 sandbox(es).",
            "  [measured] Disposed 4 sandbox(es).\n  [measured] Disposed 2 sandbox(es).",
        )
        assert any("none of them can be trusted" in r for r in check.assess(doubled))


class TestTheBillableSandboxWentAway:
    def test_a_leaked_sandbox_fails(self):
        assert any(
            "bills until" in r
            for r in check.assess(
                _swap("[measured] Disposed 2 sandbox(es).", "[measured] Disposed 1 sandbox(es).")
            )
        )

    def test_a_missing_footer_fails(self):
        assert any("Disposed" in r for r in check.assess(_without("Disposed")))


class TestTheCommandLine:
    def test_a_healthy_file_exits_zero_and_names_the_comparison(self, tmp_path: Path, capsys):
        path = tmp_path / "out.txt"
        path.write_text(_HEALTHY, encoding="utf-8")
        assert check.main(["check", str(path)]) == 0
        out = capsys.readouterr().out
        assert "5 tool-calling rounds" in out and "2 dispatched" in out

    def test_a_broken_run_exits_one_and_says_why(self, tmp_path: Path, capsys):
        path = tmp_path / "out.txt"
        path.write_text(_without("Disposed"), encoding="utf-8")
        assert check.main(["check", str(path)]) == 1
        assert "FAIL" in capsys.readouterr().err

    def test_too_many_arguments_is_a_usage_error(self, capsys):
        assert check.main(["check", "a", "b"]) == 2
        assert "usage" in capsys.readouterr().err

    def test_stdin_is_read_when_no_path_is_given(self, monkeypatch):
        monkeypatch.setattr("sys.stdin", io.StringIO(_HEALTHY))
        assert check.main(["check"]) == 0


@pytest.mark.parametrize(
    "line",
    [
        "dispatch cap for the run",
        "dispatch route: 25 lookup(s)",
        "direct route: 12 lookup(s)",
        "direct route: tool calls per round",
        "dispatch route: state totals",
        "direct route: state totals",
        "dispatch route: sales figures the model wrote",
        "direct route: sales figures the model wrote",
        "sales figures the model wrote into code, direct",
        "round trip:",
        "run directories across both sandboxes",
        "transport files left behind",
        "Disposed",
    ],
)
def test_every_measured_line_is_load_bearing(line: str):
    """Removing any one of them fails the check, so none is decoration."""
    assert check.assess(_without(line)) != []
