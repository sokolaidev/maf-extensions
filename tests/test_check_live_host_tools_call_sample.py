"""What `scripts/check_live_host_tools_call_sample.py` will and will not let through.

`_HEALTHY` is a real run against a live ACAS sandbox and a live model, verbatim apart from the
two tables the model produced. Twenty live runs went into choosing what is asserted: 18 to 29
lookups, 35s to 87s, two to four host-tool-call rounds — and what did not move is what is pinned.
"""

from __future__ import annotations

import importlib.util
import io
import sys
from pathlib import Path

import pytest

_SCRIPT = (
    Path(__file__).resolve().parent.parent / "scripts" / "check_live_host_tools_call_sample.py"
)
_spec = importlib.util.spec_from_file_location("check_host_tool_call", _SCRIPT)
assert _spec and _spec.loader
check = importlib.util.module_from_spec(_spec)
sys.modules["check_host_tool_call"] = check
_spec.loader.exec_module(check)


#: A real run. The two summary tables are the model's own output and the only edited lines.
_HEALTHY = """== 1. What the host wired ==

  registered:                  state_id, stores_in_state, store_sales, product_name
  [measured] host-tool-call cap for the run: 32 (the walk needs 12 at best, 21 written naively)
  identities the spec carries: ['app']

== 2. The lookups happen inside the sandbox ==

state	product	total_sales
Washington	TOTAL	3564.55
Oregon	TOTAL	3514.35

  [measured] host-tool-call route: 25 lookup(s) over 2 tool-calling round(s)
  [measured] host-tool-call route: tool calls per round: [1, 1]
  [measured] host-tool-call route: lookup stages exercised: 4 of 4 (product_name, state_id, store_sales, stores_in_state)
  [measured] host-tool-call route: 42.40s, 3616 tokens (in 2960, cached 1024, out 656)
  [measured] host-tool-call route: state totals the program printed: 2 of 2
  [measured] host-tool-call route: product totals the program printed: 6 of 6
  [measured] host-tool-call route: table rows the program printed: 6 of 6
  [measured] host-tool-call route: product names in the table: 3 of 3
  [measured] host-tool-call route: sales figures the model wrote into code: 0 of 12
  [measured] host-tool-call route: programs that called a host tool: 2
  [measured] host-tool-call route: round trip: 23 gap(s), min 1.08s, median 1.11s, max 1.60s
  [measured] host-tool-call route: program boundaries observed: 1, min 5.38s, max 5.38s

== 3. The lookups happen in the model's tool loop ==

| Washington | TOTAL | 3564.55 |
| Oregon | TOTAL | 3514.35 |

  [measured] direct route: 12 lookup(s) over 5 tool-calling round(s)
  [measured] direct route: tool calls per round: [2, 2, 5, 3, 1]
  [measured] direct route: lookup stages exercised: 4 of 4 (product_name, state_id, store_sales, stores_in_state)
  [measured] direct route: 14.60s, 6217 tokens (in 5559, cached 2048, out 658)
  [measured] direct route: state totals the program printed: 2 of 2
  [measured] direct route: product totals the program printed: 6 of 6
  [measured] direct route: table rows the program printed: 0 of 6
  [measured] direct route: product names in the table: 0 of 3
  [measured] direct route: sales figures the model wrote into code: 12 of 12

== 4. What the round trips bought ==

  [measured] sales figures the model wrote into code, host-tool-call: 0 of 12
  [measured] sales figures the model wrote into code, direct:         12 of 12

== 5. What the runs left in the guest ==

  [measured] transport cleanup: left for the sandbox (#438)
  [measured] call directory cleanup: left for the sandbox (#438)
  [measured] call directories across both sandboxes: 3
  [measured] of those, runs that called a host tool: 2
  [measured] transport files left behind: 75, of which answered calls: 25

  [measured] Disposed 2 sandbox(es).
"""


def _without(line: str) -> str:
    kept = [row for row in _HEALTHY.splitlines() if line not in row]
    assert len(kept) < len(_HEALTHY.splitlines()), f"nothing to remove for {line!r}"
    return "\n".join(kept)


#: The same healthy run against the transport #434 gave a cleanup. Derived from `_HEALTHY`
#: rather than written out, so the two cannot drift into describing different runs — every
#: line above act 5 is identical, which is the point: what moved is what the guest kept.
_RECLAIMED = (
    _HEALTHY.replace(
        "transport cleanup: left for the sandbox (#438)",
        "transport cleanup: reclaimed by the transport",
    )
    .replace(
        "call directory cleanup: left for the sandbox (#438)",
        "call directory cleanup: reclaimed by the framework",
    )
    .replace(
        "call directories across both sandboxes: 3", "call directories across both sandboxes: 0"
    )
    .replace(
        "of those, runs that called a host tool: 2", "of those, runs that called a host tool: 0"
    )
    .replace(
        "transport files left behind: 75, of which answered calls: 25",
        "transport files left behind: 0, of which answered calls: 0",
    )
)


def _reclaimed(old: str, new: str) -> str:
    assert old in _RECLAIMED, f"{old!r} is not in the reclaimed fixture"
    return _RECLAIMED.replace(old, new)


_TRANSPORT_RECLAIMED = _HEALTHY.replace(
    "transport cleanup: left for the sandbox (#438)",
    "transport cleanup: reclaimed by the transport",
)


def _transport_reclaimed(old: str, new: str) -> str:
    assert old in _TRANSPORT_RECLAIMED, f"{old!r} is not in the transport fixture"
    return _TRANSPORT_RECLAIMED.replace(old, new)


def _swap(old: str, new: str) -> str:
    assert old in _HEALTHY, f"{old!r} is not in the fixture"
    return _HEALTHY.replace(old, new)


class TestAHealthyRun:
    def test_it_passes(self):
        assert check.assess(_HEALTHY) == []


class TestBothProgramsHadToAnswer:
    """Both routes compute in the sandbox, so neither gets a pass on the totals."""

    @pytest.mark.parametrize("route", ["host-tool-call route", "direct route"])
    def test_a_program_that_printed_one_total_fails(self, route: str):
        broken = _swap(
            f"[measured] {route}: state totals the program printed: 2 of 2",
            f"[measured] {route}: state totals the program printed: 1 of 2",
        )
        assert any(
            f"{route}'s program printed 1 of 2 state totals" in r for r in check.assess(broken)
        )

    @pytest.mark.parametrize("route", ["host-tool-call route", "direct route"])
    def test_a_missing_total_says_nothing_about_the_transport_that_fed_it(self, route: str):
        """An attempt can serve every lookup and still print zeros, so the reason may not
        claim the program was starved."""
        broken = _swap(
            f"[measured] {route}: state totals the program printed: 2 of 2",
            f"[measured] {route}: state totals the program printed: 0 of 2",
        )
        reasons = [r for r in check.assess(broken) if "state totals" in r]
        assert reasons
        assert not any("did not finish the walk" in r for r in reasons), reasons

    @pytest.mark.parametrize("route", ["host-tool-call route", "direct route"])
    def test_a_missing_totals_line_fails(self, route: str):
        assert check.assess(_without(f"{route}: state totals")) != []

    @pytest.mark.parametrize("route", ["host-tool-call route", "direct route"])
    def test_a_table_missing_a_row_fails(self, route: str):
        """Both state totals can be right while a row underneath one of them is gone."""
        broken = _swap(
            f"[measured] {route}: product totals the program printed: 6 of 6",
            f"[measured] {route}: product totals the program printed: 5 of 6",
        )
        assert any("hides its terms" in r for r in check.assess(broken))

    @pytest.mark.parametrize("route", ["host-tool-call route", "direct route"])
    def test_a_table_scored_out_of_its_own_number_fails(self, route: str):
        broken = _swap(
            f"[measured] {route}: product totals the program printed: 6 of 6",
            f"[measured] {route}: product totals the program printed: 4 of 4",
        )
        assert any("not 6" in r for r in check.assess(broken))

    @pytest.mark.parametrize("route", ["host-tool-call route", "direct route"])
    def test_a_missing_product_totals_line_fails(self, route: str):
        assert check.assess(_without(f"{route}: product totals")) != []

    def test_a_host_tool_call_table_with_the_rows_wrong_fails(self):
        """Every value present and attached to the wrong state is the same six numbers."""
        broken = _swap(
            "[measured] host-tool-call route: table rows the program printed: 6 of 6",
            "[measured] host-tool-call route: table rows the program printed: 0 of 6",
        )
        assert any("the labels are what say" in r for r in check.assess(broken))

    def test_the_direct_table_rows_are_recorded_not_required(self):
        """That program prints figures its model labels, so `0 of 6` there is a healthy run."""
        assert check.assess(_HEALTHY) == []
        assert (
            check.assess(
                _swap(
                    "[measured] direct route: table rows the program printed: 0 of 6",
                    "[measured] direct route: table rows the program printed: 6 of 6",
                )
            )
            == []
        )

    @pytest.mark.parametrize("route", ["host-tool-call route", "direct route"])
    def test_rows_scored_out_of_their_own_number_fails(self, route: str):
        line = [r for r in _HEALTHY.splitlines() if f"{route}: table rows" in r][0]
        broken = _swap(line, line.rsplit(":", 1)[0] + ": 4 of 4")
        assert any("not 6" in r for r in check.assess(broken))

    @pytest.mark.parametrize("route", ["host-tool-call route", "direct route"])
    def test_a_missing_table_rows_line_fails(self, route: str):
        assert check.assess(_without(f"{route}: table rows")) != []


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

    @pytest.mark.parametrize("shape", ["[1, 1, 1, 1, 1]", "[4, 3, 2, 2, 1]"])
    def test_a_direct_shape_too_small_to_hold_its_lookups_fails(self, shape: str):
        """Five tool calls cannot be twelve lookups, and neither can twelve."""
        assert any(
            "is short by" in r
            for r in check.assess(
                _swap(
                    "[measured] direct route: tool calls per round: [2, 2, 5, 3, 1]",
                    f"[measured] direct route: tool calls per round: {shape}",
                )
            )
        )

    def test_a_direct_shape_holding_its_lookups_and_one_program_passes(self):
        """Thirteen against twelve is the floor: the walk, plus the one program."""
        assert not any(
            "is short by" in r
            for r in check.assess(
                _swap(
                    "[measured] direct route: tool calls per round: [2, 2, 5, 3, 1]",
                    "[measured] direct route: tool calls per round: [3, 3, 3, 3, 1]",
                )
            )
        )

    def test_a_host_tool_call_batch_of_more_than_one_fails(self):
        """One message asking for two programs runs them at once, and one ledger times both."""
        assert any(
            "can interleave" in r
            for r in check.assess(
                _swap(
                    "[measured] host-tool-call route: tool calls per round: [1, 1]",
                    "[measured] host-tool-call route: tool calls per round: [2]",
                ).replace(
                    "[measured] host-tool-call route: 25 lookup(s) over 2 tool-calling round(s)",
                    "[measured] host-tool-call route: 25 lookup(s) over 1 tool-calling round(s)",
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

    @pytest.mark.parametrize("route", ["host-tool-call route", "direct route"])
    def test_a_route_that_made_no_lookups_fails(self, route: str):
        line = [r for r in _HEALTHY.splitlines() if f"{route}: " in r and "lookup(s) over" in r][0]
        broken = _swap(line, line.replace("25 lookup", "0 lookup").replace("12 lookup", "0 lookup"))
        assert any("no lookups at all" in r for r in check.assess(broken))

    @pytest.mark.parametrize(
        ("route", "count"), [("host-tool-call route", 25), ("direct route", 12)]
    )
    def test_a_route_below_the_minimum_walk_fails(self, route: str, count: int):
        """The walk is fixed at twelve lookups, so eleven fetched less than the answer is made of."""
        broken = _swap(
            f"[measured] {route}: {count} lookup(s) over",
            f"[measured] {route}: 11 lookup(s) over",
        )
        assert any("cannot have produced the table" in r for r in check.assess(broken))

    def test_more_host_tool_call_round_trips_than_measured_is_fine(self):
        """Every line a third program would move has to move with it, or the run cannot have happened."""
        assert (
            check.assess(
                _swap(
                    "[measured] host-tool-call route: 25 lookup(s) over 2 tool-calling round(s)",
                    "[measured] host-tool-call route: 29 lookup(s) over 3 tool-calling round(s)",
                )
                .replace("round trip: 23 gap(s)", "round trip: 26 gap(s)")
                .replace("program boundaries observed: 1,", "program boundaries observed: 2,")
                .replace(
                    "host-tool-call route: tool calls per round: [1, 1]",
                    "host-tool-call route: tool calls per round: [1, 1, 1]",
                )
                .replace("call directories across both sandboxes: 3", "…dirs…")
                .replace(
                    "programs that called a host tool: 2", "programs that called a host tool: 3"
                )
                .replace("…dirs…", "call directories across both sandboxes: 4")
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
        ("route", "shape", "words"),
        [
            ("host-tool-call route", "[1, 1]", "[one, one]"),
            ("direct route", "[2, 2, 5, 3, 1]", "[two, two, five, three, one]"),
        ],
    )
    def test_a_shape_that_is_not_counts_fails(self, route: str, shape: str, words: str):
        """A shape of words has a length and nothing else, and the length is read as a count."""
        assert any(
            "not a list of positive counts" in r
            for r in check.assess(
                _swap(
                    f"[measured] {route}: tool calls per round: {shape}",
                    f"[measured] {route}: tool calls per round: {words}",
                )
            )
        )

    @pytest.mark.parametrize(
        ("route", "shape", "rounds"),
        [("host-tool-call route", "[1, 1]", 2), ("direct route", "[2, 2, 5, 3, 1]", 5)],
    )
    def test_an_empty_shape_fails(self, route: str, shape: str, rounds: int):
        """Every rule about the entries holds of no entries, so the emptiness is its own rule."""
        assert any(
            "reports no tool calls at all" in r
            for r in check.assess(
                _swap(
                    f"[measured] {route}: tool calls per round: {shape}",
                    f"[measured] {route}: tool calls per round: []",
                )
                .replace(
                    f"[measured] {route}: 12 lookup(s) over {rounds} tool-calling round(s)",
                    f"[measured] {route}: 12 lookup(s) over 0 tool-calling round(s)",
                )
                .replace(
                    f"[measured] {route}: 25 lookup(s) over {rounds} tool-calling round(s)",
                    f"[measured] {route}: 25 lookup(s) over 0 tool-calling round(s)",
                )
            )
        )

    @pytest.mark.parametrize(
        ("route", "shape"),
        [("host-tool-call route", "[1, 1]"), ("direct route", "[2, 2, 5, 3, 1]")],
    )
    def test_a_shape_entry_of_zero_fails(self, route: str, shape: str):
        """A message with no tool call is not an entry, so no entry can be zero."""
        zeroed = (
            shape.replace("[1", "[0", 1) if shape.startswith("[1") else shape.replace("[2", "[0", 1)
        )
        assert any(
            "not a list of positive counts" in r
            for r in check.assess(
                _swap(
                    f"[measured] {route}: tool calls per round: {shape}",
                    f"[measured] {route}: tool calls per round: {zeroed}",
                )
            )
        )

    @pytest.mark.parametrize(
        ("route", "shape"),
        [("host-tool-call route", "[1, 1]"), ("direct route", "[2, 2, 5, 3, 1]")],
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
                    "[measured] host-tool-call cap for the run: 32 (the walk needs 12",
                    "[measured] host-tool-call cap for the run: 16 (the walk needs 12",
                )
            )
        )

    def test_a_cap_at_the_naive_figure_fails(self):
        assert any(
            "only fits the efficient program" in r
            for r in check.assess(
                _swap(
                    "[measured] host-tool-call cap for the run: 32 (the walk needs 12",
                    "[measured] host-tool-call cap for the run: 21 (the walk needs 12",
                )
            )
        )

    def test_a_cap_above_the_naive_figure_but_below_a_real_run_fails(self):
        """22 clears both arithmetics and would still truncate the run that used 29."""
        assert any(
            "truncates a later run" in r
            for r in check.assess(
                _swap(
                    "[measured] host-tool-call cap for the run: 32 (the walk needs 12",
                    "[measured] host-tool-call cap for the run: 22 (the walk needs 12",
                )
            )
        )

    def test_a_cap_above_the_worst_live_run_is_fine(self):
        """The bound is the workload's, not a copy of the sample's constant."""
        assert (
            check.assess(
                _swap(
                    "[measured] host-tool-call cap for the run: 32 (the walk needs 12",
                    "[measured] host-tool-call cap for the run: 30 (the walk needs 12",
                )
            )
            == []
        )

    def test_a_cap_below_the_minimum_still_reports_the_truncation(self):
        reasons = check.assess(
            _swap(
                "[measured] host-tool-call cap for the run: 32 (the walk needs 12",
                "[measured] host-tool-call cap for the run: 8 (the walk needs 12",
            )
        )
        assert any("cannot finish" in r for r in reasons)


class TestTheDenominatorIsNotTheRunsToChoose:
    """A self-consistent output agrees with itself; the dataset is what settles it."""

    def test_zero_of_zero_on_every_line_fails(self):
        """Direct has carried == expected, the host-tool-call route has zero, and act 4 agrees with both."""
        zeroed = _HEALTHY
        for old, new in (
            (
                "host-tool-call route: sales figures the model wrote into code: 0 of 12",
                "host-tool-call route: sales figures the model wrote into code: 0 of 0",
            ),
            (
                "direct route: sales figures the model wrote into code: 12 of 12",
                "direct route: sales figures the model wrote into code: 0 of 0",
            ),
            (
                "sales figures the model wrote into code, host-tool-call: 0 of 12",
                "sales figures the model wrote into code, host-tool-call: 0 of 0",
            ),
            (
                "sales figures the model wrote into code, direct:         12 of 12",
                "sales figures the model wrote into code, direct:         0 of 0",
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
                "sales figures the model wrote into code, direct:         12 of 12",
                "sales figures the model wrote into code, direct:         7 of 7",
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
                    "[measured] host-tool-call route: 42.40s, 3616 tokens",
                    "[measured] host-tool-call route: 42.40s, 0 tokens",
                )
            )
        )

    def test_a_token_count_of_none_fails(self):
        """Usage reporting disappearing is a run that measured nothing, not a passing one."""
        assert (
            check.assess(
                _swap(
                    "[measured] host-tool-call route: 42.40s, 3616 tokens",
                    "[measured] host-tool-call route: 42.40s, None tokens",
                )
            )
            != []
        )


class TestTheCapBoundsEachProgram:
    """The cap is per `execute_code` run, so the route's total is bounded by cap × programs."""

    _REASON = "longer than the run could have made it"

    def test_more_lookups_than_the_cap_allows_fails(self):
        """A cap of 32 over the 2 programs that called a host tool allows 64, and this claims 65."""
        assert any(
            self._REASON in r
            for r in check.assess(
                _swap(
                    "[measured] host-tool-call route: 25 lookup(s) over 2 tool-calling round(s)",
                    "[measured] host-tool-call route: 65 lookup(s) over 2 tool-calling round(s)",
                )
            )
        )

    def test_spending_the_whole_budget_twice_over_is_allowed(self):
        """A program may spend its whole cap and the next one starts fresh, so the bound is the product."""
        assert not any(
            self._REASON in r
            for r in check.assess(
                _swap(
                    "[measured] host-tool-call route: 25 lookup(s) over 2 tool-calling round(s)",
                    "[measured] host-tool-call route: 64 lookup(s) over 2 tool-calling round(s)",
                )
            )
        )


class TestTheWholeWalkHappened:
    """A count and a shape do not say the fourth stage ran."""

    @pytest.mark.parametrize("route", ["host-tool-call route", "direct route"])
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

    @pytest.mark.parametrize("route", ["host-tool-call route", "direct route"])
    def test_a_missing_stages_line_fails(self, route: str):
        assert check.assess(_without(f"{route}: lookup stages exercised")) != []

    def test_a_host_tool_call_table_without_product_names_fails(self):
        """The model on that route never sees a name, so an unnamed table is a skipped stage."""
        assert any(
            "never receives a product name" in r
            for r in check.assess(
                _swap(
                    "[measured] host-tool-call route: product names in the table: 3 of 3",
                    "[measured] host-tool-call route: product names in the table: 0 of 3",
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
                    "[measured] host-tool-call cap for the run: 32 (the walk needs 12 at best, 21 written naively)",
                    "[measured] host-tool-call cap for the run: 32 (the walk needs 2 at best, 12 written naively)",
                )
            )
        )

    def test_a_cap_at_the_registry_default_fails(self):
        assert any(
            "the registry allows by default" in r
            for r in check.assess(
                _swap(
                    "[measured] host-tool-call cap for the run: 32 (the walk",
                    "[measured] host-tool-call cap for the run: 16 (the walk",
                )
            )
        )


class TestWhoCarriedTheFigures:
    def test_the_host_tool_call_route_writing_a_figure_fails(self):
        broken = _swap(
            "[measured] host-tool-call route: sales figures the model wrote into code: 0 of 12",
            "[measured] host-tool-call route: sales figures the model wrote into code: 4 of 12",
        ).replace(
            "[measured] sales figures the model wrote into code, host-tool-call: 0 of 12",
            "[measured] sales figures the model wrote into code, host-tool-call: 4 of 12",
        )
        assert any("before any host-tool call can answer" in r for r in check.assess(broken))

    def test_the_direct_route_writing_none_fails(self):
        broken = _swap(
            "[measured] direct route: sales figures the model wrote into code: 12 of 12",
            "[measured] direct route: sales figures the model wrote into code: 0 of 12",
        ).replace(
            "[measured] sales figures the model wrote into code, direct:         12 of 12",
            "[measured] sales figures the model wrote into code, direct:         0 of 12",
        )
        assert any("not the comparison this sample makes" in r for r in check.assess(broken))

    def test_act_four_has_to_agree_on_the_denominator_too(self):
        """`12 of 7` against `12 of 12` agrees on the numerator and describes another dataset."""
        assert any(
            "disagree" in r
            for r in check.assess(
                _swap(
                    "[measured] sales figures the model wrote into code, direct:         12 of 12",
                    "[measured] sales figures the model wrote into code, direct:         12 of 7",
                )
            )
        )

    def test_act_four_has_to_agree(self):
        assert any(
            "disagree" in r
            for r in check.assess(
                _swap(
                    "[measured] sales figures the model wrote into code, direct:         12 of 12",
                    "[measured] sales figures the model wrote into code, direct:         7 of 12",
                )
            )
        )

    def test_a_missing_restatement_fails(self):
        assert any(
            "act 4 host-tool-call restatement" in r
            for r in check.assess(
                _without("sales figures the model wrote into code, host-tool-call")
            )
        )


class TestATransportThatReclaimsItsOwn:
    """#434 gave the transport a cleanup, and a sample runs against what is published.

    Zero has to be exactly zero: a transport that removed part of what it owns is worse than one
    that removed none, because the next run in the sandbox can read the rest.
    """

    def test_a_reclaimed_run_passes(self):
        assert check.assess(_RECLAIMED) == []

    def test_reclaimed_call_directories_must_not_leave_run_directories(self):
        broken = _reclaimed(
            "call directories across both sandboxes: 0",
            "call directories across both sandboxes: 1",
        )
        assert any("survived framework reclamation" in r for r in check.assess(broken))

    def test_an_unreported_transport_fails(self):
        """Which transport ran decides what the counts below it mean, so it is not optional."""
        assert any(
            "transport cleanup" in r
            for r in check.assess(
                _HEALTHY.replace(
                    "  [measured] transport cleanup: left for the sandbox (#438)\n", ""
                )
            )
        )

    def test_traffic_surviving_a_reclaiming_transport_fails(self):
        assert any(
            "the cleanup having half worked" in r
            for r in check.assess(
                _transport_reclaimed(
                    "transport files left behind: 75, of which answered calls: 25",
                    "transport files left behind: 3, of which answered calls: 1",
                )
            )
        )

    def test_a_run_still_holding_its_transport_directory_fails(self):
        assert any(
            "removes the one it owns on every exit path" in r
            for r in check.assess(
                _transport_reclaimed(
                    "of those, runs that called a host tool: 2",
                    "of those, runs that called a host tool: 2",
                )
            )
        )

    def test_fewer_directories_than_programs_fails(self):
        """The runs are the kind's and survive, and the direct route's is one more."""
        assert any(
            "the only thing left saying the programs ran" in r
            for r in check.assess(
                _transport_reclaimed(
                    "call directories across both sandboxes: 3",
                    "call directories across both sandboxes: 2",
                )
            )
        )

    def test_the_gap_arithmetic_still_binds_without_the_guest(self):
        """The program count comes from the shape now; the ledger's own clock still checks it."""
        assert any(
            "observer saw" in r
            for r in check.assess(_reclaimed("round trip: 23 gap(s)", "round trip: 20 gap(s)"))
        )


class TestTheRunsLeftTheirTrafficBehind:
    """#302's per-run subdirectory, and the cleanup #438 says nobody can do."""

    def test_no_files_left_behind_fails(self):
        """The transport writes a file per call and nothing deletes it, so zero cannot be."""
        assert any(
            "does not write" in r
            for r in check.assess(
                _swap(
                    "[measured] transport files left behind: 75, of which answered calls: 25",
                    "[measured] transport files left behind: 0, of which answered calls: 0",
                )
            )
        )

    def test_no_run_host_tool_calling_fails(self):
        assert any(
            "no transport traffic" in r
            for r in check.assess(
                _swap(
                    "[measured] of those, runs that called a host tool: 2",
                    "[measured] of those, runs that called a host tool: 0",
                )
            )
        )

    def test_every_directory_calling_a_host_tool_fails(self):
        """The direct route's program leaves a run directory with no transport in it."""
        assert any(
            "one sandbox short" in r
            for r in check.assess(
                _swap(
                    "[measured] call directories across both sandboxes: 3",
                    "[measured] call directories across both sandboxes: 2",
                )
            )
        )

    def test_files_but_no_answered_call_fails(self):
        """Litter with no answer in it means nothing in the guest records a call being served."""
        assert any(
            "records a host-tool call having been served" in r
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

    def test_more_host_tool_call_runs_than_directories_fails(self):
        assert any(
            "not arithmetic" in r
            for r in check.assess(
                _swap(
                    "[measured] of those, runs that called a host tool: 2",
                    "[measured] of those, runs that called a host tool: 9",
                )
            )
        )

    @pytest.mark.parametrize(
        "line",
        [
            "call directories across both sandboxes",
            "of those, runs that called a host tool",
            "transport files left behind",
        ],
    )
    def test_each_line_is_required(self, line: str):
        assert check.assess(_without(line)) != []


class TestACountIsNotAMatch:
    """Holes a length check or a positive-count check leaves open."""

    def test_a_partial_direct_count_fails(self):
        """1-11 of 12 is not "the model carried the data", it is an unmeasured road."""
        broken = _swap(
            "[measured] direct route: sales figures the model wrote into code: 12 of 12",
            "[measured] direct route: sales figures the model wrote into code: 7 of 12",
        ).replace(
            "[measured] sales figures the model wrote into code, direct:         12 of 12",
            "[measured] sales figures the model wrote into code, direct:         7 of 12",
        )
        assert any("7 of 12" in r for r in check.assess(broken))

    def test_a_positive_but_inconsistent_gap_count_fails(self):
        """21 lookups yield exactly 20 gaps; one gap is a median over a twentieth of the run."""
        assert any(
            "observer saw" in r
            for r in check.assess(_swap("round trip: 23 gap(s)", "round trip: 1 gap(s)"))
        )

    def test_two_restatements_of_one_route_and_none_of_the_other_fails(self):
        """Both are two lines, so a length check passes while act 4 omits half the comparison."""
        doubled = _without("sales figures the model wrote into code, host-tool-call").replace(
            "  [measured] sales figures the model wrote into code, direct:         12 of 12",
            "  [measured] sales figures the model wrote into code, direct:         12 of 12\n"
            "  [measured] sales figures the model wrote into code, direct:         12 of 12",
        )
        reasons = check.assess(doubled)
        assert any("act 4 host-tool-call restatement" in r for r in reasons)
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

    def test_a_observed_program_count_contradicts_the_gap_count(self):
        """The observer-derived program count determines how many same-run gaps remain."""
        assert any(
            "observer saw 3" in r
            for r in check.assess(
                _swap("programs that called a host tool: 2", "programs that called a host tool: 3")
            )
        )

    def test_a_batched_program_shape_fails_the_observed_measurement(self):
        """A batched message can interleave runs, so it is not a valid sequential ledger."""
        probed = _swap(
            "[measured] host-tool-call route: 25 lookup(s) over 2 tool-calling round(s)",
            "[measured] host-tool-call route: 25 lookup(s) over 3 tool-calling round(s)",
        ).replace(
            "host-tool-call route: tool calls per round: [1, 1]",
            "host-tool-call route: tool calls per round: [1, 1, 1]",
        )
        assert any("shape describes 3 program(s)" in r for r in check.assess(probed))

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


class TestTheBoundariesTheSummaryRestsOn:
    """Observed run transitions, not latency ordering, define the boundaries."""

    _LINE = "[measured] host-tool-call route: program boundaries observed: 1, min 5.38s, max 5.38s"

    def test_a_missing_boundary_line_fails(self):
        assert any(
            "no program boundary was reported" in r
            for r in check.assess(_HEALTHY.replace(self._LINE, ""))
        )

    def test_a_boundary_count_that_is_not_one_per_program_fails(self):
        assert any(
            "run identity should produce 1" in r
            for r in check.assess(
                _swap("program boundaries observed: 1,", "program boundaries observed: 2,")
            )
        )

    def test_boundary_values_need_not_exceed_transport_values(self):
        changed = _swap("min 5.38s, max 5.38s", "min 0.90s, max 0.90s")
        assert check.assess(changed) == []

    def test_a_nonpositive_boundary_fails(self):
        assert any(
            "not positive and ordered" in r
            for r in check.assess(_swap("min 5.38s, max 5.38s", "min 0.00s, max 0.00s"))
        )

    def test_a_direct_boundary_line_fails(self):
        direct = _HEALTHY.replace(
            "== 4. What the round trips bought ==",
            "  [measured] direct route: program boundaries observed: 1, min 2.00s, max 2.00s\n"
            "== 4. What the round trips bought ==",
        )
        assert any("direct route" in reason for reason in check.assess(direct))

    def test_duplicate_host_tool_call_boundary_lines_fail(self):
        duplicate = _HEALTHY.replace(
            "== 4. What the round trips bought ==",
            "  [measured] host-tool-call route: program boundaries observed: 1, min 5.38s, max 5.38s\n"
            "== 4. What the round trips bought ==",
        )
        assert any("exactly one boundary summary" in reason for reason in check.assess(duplicate))

    def test_a_boundary_summary_with_one_program_fails(self):
        one_program = _HEALTHY.replace(
            "programs that called a host tool: 2", "programs that called a host tool: 1"
        ).replace("program boundaries observed: 1", "program boundaries observed: 0")
        assert any(
            "boundary requires at least two programs" in reason
            for reason in check.assess(one_program)
        )


class TestObservedProgramCount:
    def test_a_missing_observer_count_fails(self):
        assert any(
            "no tagged host-tool-call-route" in reason
            for reason in check.assess(_without("programs that called a host tool"))
        )

    def test_a_duplicate_observer_count_fails(self):
        duplicate = _HEALTHY.replace(
            "  [measured] host-tool-call route: round trip:",
            "  [measured] host-tool-call route: programs that called a host tool: 2\n"
            "  [measured] host-tool-call route: round trip:",
        )
        assert any("exactly one observer count" in reason for reason in check.assess(duplicate))

    def test_a_direct_observer_count_fails(self):
        direct = _HEALTHY.replace(
            "== 4. What the round trips bought ==",
            "  [measured] direct route: programs that called a host tool: 1\n"
            "== 4. What the round trips bought ==",
        )
        assert any("only the host-tool-call route" in reason for reason in check.assess(direct))


class TestTheCapWasBudgeted:
    def test_a_cap_below_the_minimum_fails(self):
        assert any(
            "cannot finish" in r
            for r in check.assess(
                _swap(
                    "[measured] host-tool-call cap for the run: 32 (the walk needs 12",
                    "[measured] host-tool-call cap for the run: 8 (the walk needs 12",
                )
            )
        )

    def test_a_missing_cap_line_fails(self):
        assert any("host-tool-call cap" in r for r in check.assess(_without("host-tool-call cap")))


class TestWhatIsRecordedAndNeverBounded:
    """A slow control plane, an expensive run or a chatty program is a finding, not a grade."""

    def test_a_very_slow_expensive_run_passes(self):
        assert (
            check.assess(
                _swap(
                    "[measured] host-tool-call route: 42.40s, 3616 tokens (in 2960, cached 1024, out 656)",
                    "[measured] host-tool-call route: 240.00s, 99999 tokens (in 99000, cached 0, out 999)",
                )
            )
            == []
        )

    @pytest.mark.parametrize(
        ("route", "seconds"), [("host-tool-call route", "42.40s"), ("direct route", "14.60s")]
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
                    "[measured] host-tool-call route: 25 lookup(s) over 2 tool-calling round(s)",
                    "[measured] host-tool-call route: 29 lookup(s) over 2 tool-calling round(s)",
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
        forged = _without("host-tool-call route: sales figures the model wrote into code").replace(
            "== 4. What the round trips bought ==",
            "> [measured] host-tool-call route: sales figures the model wrote into code: 0 of 12\n"
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
        assert "5 tool-calling rounds" in out and "2 on the host-tool-call route" in out

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
        "host-tool-call cap for the run",
        "host-tool-call route: 25 lookup(s)",
        "direct route: 12 lookup(s)",
        "direct route: tool calls per round",
        "host-tool-call route: state totals",
        "direct route: state totals",
        "host-tool-call route: sales figures the model wrote",
        "direct route: sales figures the model wrote",
        "sales figures the model wrote into code, direct",
        "round trip:",
        "call directories across both sandboxes",
        "transport files left behind",
        "Disposed",
    ],
)
def test_every_measured_line_is_load_bearing(line: str):
    """Removing any one of them fails the check, so none is decoration."""
    assert check.assess(_without(line)) != []


#: A docker run (sample 15 with `SAMPLE_BACKEND=docker`): the same walk on `maf_sandbox_docker`,
#: which declares no `FILES_LIST` and so prints no act-5 leftover lines. Derived from `_HEALTHY` by
#: dropping that act, so the two fixtures cannot drift on anything the docker run shares with ACAS.
_ACT_FIVE_MARKERS = (
    "== 5. What the runs left",
    "transport cleanup:",
    "call directory cleanup:",
    "call directories across both sandboxes:",
    "of those, runs that called a host tool:",
    "transport files left behind:",
)
_DOCKER = (
    "\n".join(row for row in _HEALTHY.splitlines() if not any(m in row for m in _ACT_FIVE_MARKERS))
    + "\n"
)


class TestTheDockerSampleHasNoActFive:
    """On docker the backend declares no FILES_LIST, so `--docker` drops act 5, keeps the rest."""

    def test_the_docker_run_passes_in_docker_mode(self):
        assert check.assess(_DOCKER, docker=True) == []

    def test_the_same_run_fails_in_acas_mode(self):
        """The five missing leftover lines are five failures, so the flag is load-bearing."""
        assert check.assess(_DOCKER) != []

    def test_a_stray_leftover_line_is_ignored_in_docker_mode(self):
        """Docker mode does not read act 5 at all, so one appearing does not change the verdict."""
        with_line = _DOCKER.replace(
            "  [measured] Disposed 2 sandbox(es).",
            "  [measured] transport files left behind: 0, of which answered calls: 0\n"
            "  [measured] Disposed 2 sandbox(es).",
        )
        assert check.assess(with_line, docker=True) == []

    def test_the_host_tool_call_finding_still_binds(self):
        """Act 5 is gone; the point of the sample is not. The host-tool-call route wrote no figure."""
        broken = _DOCKER.replace(
            "host-tool-call route: sales figures the model wrote into code: 0 of 12",
            "host-tool-call route: sales figures the model wrote into code: 4 of 12",
        ).replace(
            "sales figures the model wrote into code, host-tool-call: 0 of 12",
            "sales figures the model wrote into code, host-tool-call: 4 of 12",
        )
        assert any(
            "before any host-tool call can answer" in r for r in check.assess(broken, docker=True)
        )

    def test_a_leaked_container_still_fails(self):
        broken = _DOCKER.replace("Disposed 2 sandbox(es).", "Disposed 1 sandbox(es).")
        assert any("until it is explicitly removed" in r for r in check.assess(broken, docker=True))

    def test_the_cli_docker_flag_selects_docker_mode(self, tmp_path: Path, capsys):
        path = tmp_path / "out.txt"
        path.write_text(_DOCKER, encoding="utf-8")
        assert check.main(["check", "--docker", str(path)]) == 0
        assert "host-tool-call" in capsys.readouterr().out

    def test_without_the_flag_the_cli_rejects_the_docker_run(self, tmp_path: Path, capsys):
        path = tmp_path / "out.txt"
        path.write_text(_DOCKER, encoding="utf-8")
        assert check.main(["check", str(path)]) == 1
        assert "FAIL" in capsys.readouterr().err


class TestWhichHalfFailedIsInTheExitStatus:
    """A model's off attempt and a broken transport are different claims and statuses."""

    def _status(self, tmp_path: Path, output: str) -> int:
        path = tmp_path / "out.txt"
        path.write_text(output, encoding="utf-8")
        return check.main(["check", str(path)])

    def test_a_healthy_run_exits_zero(self, tmp_path: Path):
        assert self._status(tmp_path, _HEALTHY) == 0

    def test_a_table_of_zeros_asks_for_another_attempt(self, tmp_path: Path):
        """The whole walk served and every figure missing: the shape the split exists for."""
        zeroed = _swap(
            "[measured] host-tool-call route: state totals the program printed: 2 of 2",
            "[measured] host-tool-call route: state totals the program printed: 0 of 2",
        )
        for line, printed in (("product totals", 0), ("table rows", 0)):
            zeroed = zeroed.replace(
                f"[measured] host-tool-call route: {line} the program printed: 6 of 6",
                f"[measured] host-tool-call route: {line} the program printed: {printed} of 6",
            )
        assert self._status(tmp_path, zeroed) == check.MODEL_DID_NOT_CONVERGE

    def test_a_measurement_this_suite_owns_does_not(self, tmp_path: Path):
        """A sandbox left running is not something a second attempt can mend."""
        broken = _swap("Disposed 2 sandbox(es).", "Disposed 1 sandbox(es).")
        assert self._status(tmp_path, broken) == 1

    def test_one_hard_failure_among_the_model_s_is_enough_to_forbid_a_retry(self, tmp_path: Path):
        both = _swap(
            "[measured] host-tool-call route: state totals the program printed: 2 of 2",
            "[measured] host-tool-call route: state totals the program printed: 0 of 2",
        ).replace("Disposed 2 sandbox(es).", "Disposed 1 sandbox(es).")
        assert self._status(tmp_path, both) == 1

    def test_the_exit_line_does_not_blame_the_transport(self, tmp_path: Path, capsys):
        """The same claim the workflow acts on, and people read it first."""
        self._status(
            tmp_path,
            _swap(
                "[measured] host-tool-call route: state totals the program printed: 2 of 2",
                "[measured] host-tool-call route: state totals the program printed: 0 of 2",
            ),
        )
        said = capsys.readouterr().err
        assert "Exiting 3" in said, said
        assert "the host served the lookups it was asked for" in said, said

    def test_the_class_travels_with_the_message_rather_than_being_matched_from_it(self):
        """A reworded failure must keep its class, so nothing downstream parses prose."""
        zeroed = check.assess(
            _swap(
                "[measured] host-tool-call route: state totals the program printed: 2 of 2",
                "[measured] host-tool-call route: state totals the program printed: 0 of 2",
            )
        )
        assert zeroed and all(isinstance(r, check._TheModelsHalf) for r in zeroed)
        disposed = check.assess(_swap("Disposed 2 sandbox(es).", "Disposed 1 sandbox(es)."))
        assert disposed and not any(isinstance(r, check._TheModelsHalf) for r in disposed)

    def test_a_run_that_never_finished_is_not_the_model_s_half(self, tmp_path: Path):
        """A sample that died before measuring anything is not a bad walk."""
        assert self._status(tmp_path, _HEALTHY.split("== 2.")[0]) == 1

    def test_the_docker_run_splits_the_same_way(self, tmp_path: Path):
        """`--docker` drops act 5; which half owns a failure is not a backend question."""
        path = tmp_path / "out.txt"
        path.write_text(
            _DOCKER.replace(
                "[measured] host-tool-call route: state totals the program printed: 2 of 2",
                "[measured] host-tool-call route: state totals the program printed: 0 of 2",
            ),
            encoding="utf-8",
        )
        assert check.main(["check", "--docker", str(path)]) == check.MODEL_DID_NOT_CONVERGE


#: One tamper per `_TheModelsHalf` branch, so each is proved to reach exit 3 rather than
#: only to produce a message. Some need a second edit to keep a line this suite owns
#: consistent with the first, which is the point: only the model's reason may fire.
_MODEL_OWNED = {
    "stages exercised": (("host-tool-call route: lookup stages exercised: 4 of 4", "...: 3 of 4"),),
    "product names": (("host-tool-call route: product names in the table: 3 of 3", "...: 2 of 3"),),
    "state totals": (
        ("host-tool-call route: state totals the program printed: 2 of 2", "...: 1 of 2"),
    ),
    "product cells": (
        ("host-tool-call route: product totals the program printed: 6 of 6", "...: 5 of 6"),
    ),
    "table rows": (
        ("host-tool-call route: table rows the program printed: 6 of 6", "...: 5 of 6"),
    ),
    # Edited on the direct side: the host-tool-call shape has to sum to the observer's program
    # count, and no five positive entries sum to two.
    "direct paid no more rounds": (
        (
            "direct route: 12 lookup(s) over 5 tool-calling",
            "direct route: 12 lookup(s) over 2 tool-calling",
        ),
        ("direct route: tool calls per round: [2, 2, 5, 3, 1]", "...: [7, 6]"),
    ),
    "walk too short": (
        ("host-tool-call route: 25 lookup(s)", "host-tool-call route: 5 lookup(s)"),
        ("host-tool-call route: round trip: 23 gap(s)", "...: 3 gap(s)"),
    ),
    "two programs in one message": (
        (
            "host-tool-call route: 25 lookup(s) over 2 tool-calling",
            "host-tool-call route: 25 lookup(s) over 1 tool-calling",
        ),
        ("host-tool-call route: tool calls per round: [1, 1]", "...: [2]"),
    ),
    "direct batched across stages": (
        (
            "direct route: 12 lookup(s) over 5 tool-calling",
            "direct route: 12 lookup(s) over 3 tool-calling",
        ),
        ("direct route: tool calls per round: [2, 2, 5, 3, 1]", "...: [2, 2, 9]"),
    ),
    "direct carried too few figures": (
        ("direct route: sales figures the model wrote into code: 12 of 12", "...: 11 of 12"),
        ("into code, direct:         12 of 12", "into code, direct:         11 of 12"),
    ),
}


def _tamper(edits) -> str:
    """Apply one branch's edits, keeping `[measured]` prefixes the `...` shorthand stands for."""
    out = _HEALTHY
    for old, new in edits:
        if new.startswith("..."):
            new = old[: old.rindex(":")] + new[3:]
        assert old in out, f"{old!r} is not in the fixture"
        out = out.replace(old, new)
    assert out != _HEALTHY
    return out


class TestEveryModelOwnedBranchReachesTheRetryStatus:
    """Message presence is not the contract; the exit status is.

    A branch that lost its `_TheModelsHalf` wrapper would still print the same reason and
    would silently exit 1, turning a documented retry into a red release.
    """

    @pytest.mark.parametrize("branch", sorted(_MODEL_OWNED))
    def test_it_is_the_model_s_half(self, branch: str, tmp_path: Path):
        output = _tamper(_MODEL_OWNED[branch])
        reasons = check.assess(output)
        assert reasons, f"{branch} tripped nothing"
        assert all(isinstance(r, check._TheModelsHalf) for r in reasons), [
            r for r in reasons if not isinstance(r, check._TheModelsHalf)
        ]
        path = tmp_path / "out.txt"
        path.write_text(output, encoding="utf-8")
        assert check.main(["check", str(path)]) == check.MODEL_DID_NOT_CONVERGE

    def test_every_branch_in_the_script_has_a_case_here(self):
        """A new `_TheModelsHalf` without a case would ship untested."""
        source = _SCRIPT.read_text(encoding="utf-8")
        wrapped = source.count("_TheModelsHalf(\n")
        assert wrapped == len(_MODEL_OWNED), (
            f"{wrapped} model-owned branches in the script, {len(_MODEL_OWNED)} cases here"
        )
