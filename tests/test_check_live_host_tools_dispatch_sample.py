"""What `scripts/check_live_host_tools_dispatch_sample.py` will and will not let through.

`_HEALTHY` is a real run of `samples/15_acas_codeact_host_tools` against a live ACAS sandbox
and a live model, verbatim apart from the two tables the model produced.

The suite is organised around what the check is *allowed* to fail a release for. Seven live
runs went into choosing that: the figures below moved between them — 18 to 29 lookups, 35s to
87s, two to four dispatched round trips — and what did not move is what is asserted.

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

  [measured] dispatch route: 21 lookup(s) over 3 tool-calling round(s)
  [measured] dispatch route: tool calls per round: [1, 1, 1]
  [measured] dispatch route: 48.07s, 6270 tokens (in 5101, cached 2560, out 1169)
  [measured] dispatch route: state totals the program printed: 2 of 2
  [measured] dispatch route: sales figures the model wrote into code: 0 of 12
  [measured] dispatch route: round trip: 20 gap(s), min 1.13s, median 1.34s, max 6.96s

== 3. The lookups happen in the model's tool loop ==

| Washington | TOTAL | 3564.55 |
| Oregon | TOTAL | 3514.35 |

  [measured] direct route: 12 lookup(s) over 5 tool-calling round(s)
  [measured] direct route: tool calls per round: [2, 2, 5, 3, 1]
  [measured] direct route: 14.60s, 6217 tokens (in 5559, cached 2048, out 658)
  [measured] direct route: state totals the program printed: 2 of 2
  [measured] direct route: sales figures the model wrote into code: 12 of 12

== 4. What the round trips bought ==

  [measured] sales figures the model handled, dispatched: 0 of 12
  [measured] sales figures the model handled, direct:     12 of 12

== 5. What the runs left in the guest ==

  [measured] run directories across both sandboxes: 3
  [measured] of those, runs that dispatched: 2
  [measured] transport files left behind: 63, of which answered calls: 21

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


class TestDirectPaysPerStage:
    """The structural comparison, and the reason the workload has four stages."""

    def test_direct_needing_no_more_round_trips_fails(self):
        assert any(
            "did not show it" in r
            for r in check.assess(
                _swap(
                    "[measured] direct route: 12 lookup(s) over 5 tool-calling round(s)",
                    "[measured] direct route: 12 lookup(s) over 3 tool-calling round(s)",
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
        broken = _swap(line, line.replace("21 lookup", "0 lookup").replace("12 lookup", "0 lookup"))
        assert any("no lookups at all" in r for r in check.assess(broken))

    def test_more_dispatched_round_trips_than_measured_is_fine(self):
        """Two to four was the live range; the check bounds the comparison, not the value."""
        assert (
            check.assess(
                _swap(
                    "[measured] dispatch route: 21 lookup(s) over 3 tool-calling round(s)",
                    "[measured] dispatch route: 29 lookup(s) over 4 tool-calling round(s)",
                )
                .replace("round trip: 20 gap(s)", "round trip: 28 gap(s)")
                .replace(
                    "dispatch route: tool calls per round: [1, 1, 1]",
                    "dispatch route: tool calls per round: [1, 1, 1, 1]",
                )
            )
            == []
        )


class TestTwoViewsOfOneListHaveToAgree:
    """The trip count and the shape are the same list counted twice."""

    @pytest.mark.parametrize(
        ("route", "shape"),
        [("dispatch route", "[1, 1, 1]"), ("direct route", "[2, 2, 5, 3, 1]")],
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
                "sales figures the model handled, dispatched: 0 of 12",
                "sales figures the model handled, dispatched: 0 of 0",
            ),
            (
                "sales figures the model handled, direct:     12 of 12",
                "sales figures the model handled, direct:     0 of 0",
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
                "sales figures the model handled, direct:     12 of 12",
                "sales figures the model handled, direct:     7 of 7",
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
                    "[measured] dispatch route: 48.07s, 6270 tokens",
                    "[measured] dispatch route: 48.07s, 0 tokens",
                )
            )
        )

    def test_a_token_count_of_none_fails(self):
        """Usage reporting disappearing is a run that measured nothing, not a passing one."""
        assert (
            check.assess(
                _swap(
                    "[measured] dispatch route: 48.07s, 6270 tokens",
                    "[measured] dispatch route: 48.07s, None tokens",
                )
            )
            != []
        )


class TestWhoCarriedTheFigures:
    def test_the_dispatched_route_writing_a_figure_fails(self):
        broken = _swap(
            "[measured] dispatch route: sales figures the model wrote into code: 0 of 12",
            "[measured] dispatch route: sales figures the model wrote into code: 4 of 12",
        ).replace(
            "[measured] sales figures the model handled, dispatched: 0 of 12",
            "[measured] sales figures the model handled, dispatched: 4 of 12",
        )
        assert any("before any dispatch can answer" in r for r in check.assess(broken))

    def test_the_direct_route_writing_none_fails(self):
        broken = _swap(
            "[measured] direct route: sales figures the model wrote into code: 12 of 12",
            "[measured] direct route: sales figures the model wrote into code: 0 of 12",
        ).replace(
            "[measured] sales figures the model handled, direct:     12 of 12",
            "[measured] sales figures the model handled, direct:     0 of 12",
        )
        assert any("not the comparison this sample makes" in r for r in check.assess(broken))

    def test_act_four_has_to_agree_on_the_denominator_too(self):
        """`12 of 7` against `12 of 12` agrees on the numerator and describes another dataset."""
        assert any(
            "disagree" in r
            for r in check.assess(
                _swap(
                    "[measured] sales figures the model handled, direct:     12 of 12",
                    "[measured] sales figures the model handled, direct:     12 of 7",
                )
            )
        )

    def test_act_four_has_to_agree(self):
        assert any(
            "disagree" in r
            for r in check.assess(
                _swap(
                    "[measured] sales figures the model handled, direct:     12 of 12",
                    "[measured] sales figures the model handled, direct:     7 of 12",
                )
            )
        )

    def test_a_missing_restatement_fails(self):
        assert any(
            "act 4 dispatched restatement" in r
            for r in check.assess(_without("sales figures the model handled, dispatched"))
        )


class TestTheRunsLeftTheirTrafficBehind:
    """#302's per-run subdirectory, and the cleanup #438 says nobody can do."""

    def test_no_files_left_behind_fails(self):
        """Zero means the sample looked in the wrong place, which is what it did."""
        assert any(
            "does not write" in r
            for r in check.assess(
                _swap(
                    "[measured] transport files left behind: 63, of which answered calls: 21",
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
                    "[measured] transport files left behind: 63, of which answered calls: 21",
                    "[measured] transport files left behind: 63, of which answered calls: 0",
                )
            )
        )

    def test_more_answers_than_files_fails(self):
        assert any(
            "not arithmetic" in r
            for r in check.assess(
                _swap(
                    "[measured] transport files left behind: 63, of which answered calls: 21",
                    "[measured] transport files left behind: 5, of which answered calls: 21",
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
            "[measured] sales figures the model handled, direct:     12 of 12",
            "[measured] sales figures the model handled, direct:     7 of 12",
        )
        assert any("7 of 12" in r for r in check.assess(broken))

    def test_a_positive_but_inconsistent_gap_count_fails(self):
        """21 lookups yield exactly 20 gaps; one gap is a median over a twentieth of the run."""
        assert any(
            "different set of calls" in r
            for r in check.assess(_swap("round trip: 20 gap(s)", "round trip: 1 gap(s)"))
        )

    def test_two_restatements_of_one_route_and_none_of_the_other_fails(self):
        """Both are two lines, so a length check passes while act 4 omits half the comparison."""
        doubled = _without("sales figures the model handled, dispatched").replace(
            "  [measured] sales figures the model handled, direct:     12 of 12",
            "  [measured] sales figures the model handled, direct:     12 of 12\n"
            "  [measured] sales figures the model handled, direct:     12 of 12",
        )
        reasons = check.assess(doubled)
        assert any("act 4 dispatched restatement" in r for r in reasons)
        assert any("appears 2 times" in r for r in reasons)


class TestTheRoundTripLine:
    def test_an_unordered_summary_fails(self):
        assert any(
            "not ordered" in r
            for r in check.assess(
                _swap("min 1.13s, median 1.34s, max 6.96s", "min 2.00s, median 1.34s, max 6.96s")
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
                    "[measured] dispatch route: 48.07s, 6270 tokens (in 5101, cached 2560, out 1169)",
                    "[measured] dispatch route: 240.00s, 99999 tokens (in 99000, cached 0, out 999)",
                )
            )
            == []
        )

    def test_a_chatty_program_passes(self):
        assert (
            check.assess(
                _swap(
                    "[measured] dispatch route: 21 lookup(s) over 3 tool-calling round(s)",
                    "[measured] dispatch route: 29 lookup(s) over 3 tool-calling round(s)",
                ).replace("round trip: 20 gap(s)", "round trip: 28 gap(s)")
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
        assert "5 tool-calling rounds" in out and "3 dispatched" in out

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
        "dispatch route: 21 lookup(s)",
        "direct route: 12 lookup(s)",
        "direct route: tool calls per round",
        "dispatch route: state totals",
        "direct route: state totals",
        "dispatch route: sales figures the model wrote",
        "direct route: sales figures the model wrote",
        "sales figures the model handled, direct",
        "round trip:",
        "run directories across both sandboxes",
        "transport files left behind",
        "Disposed",
    ],
)
def test_every_measured_line_is_load_bearing(line: str):
    """Removing any one of them fails the check, so none is decoration."""
    assert check.assess(_without(line)) != []
