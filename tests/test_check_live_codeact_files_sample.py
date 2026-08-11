"""The two-halved judgement behind the files-channel live check.

`scripts/check_live_codeact_files_sample.py` runs on a real `samples/08_docker_codeact_files`
run to decide whether the published stack moved a file in *and* a file out. Its `assess` is a
pure function, so the judgement is tested here — on every PR — while the run that feeds it
happens only on dispatch and after a release.

What these pin is the half that is easy to lose: an output-only check would pass a turn that
computed the right total and landed nothing, and that turn is precisely the regression this
sample exists to catch. So the failing cases matter more than the healthy one — and two whole
classes of them come from a first draft that matched numbers as substrings and checked region
names and totals independently, which let `11240` stand in for `1124` and a summary with every
value swapped pass intact.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_SCRIPT = (
    Path(__file__).resolve().parent.parent
    / "scripts"
    / "check_live_codeact_files_sample.py"
)
_spec = importlib.util.spec_from_file_location(
    "check_live_codeact_files_sample", _SCRIPT
)
assert _spec and _spec.loader
check = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check)

#: A representative healthy run: the model's reply, the disposal line, and the host's own
#: record of what reached the sink this turn.
_HEALTHY = """\
The grand total across all regions is 1124. I saved the per-region breakdown as summary.md.

Disposed 1 sandbox(es).
Delivered this turn into out/: summary.md
"""

_SUMMARY = """\
| Region | Revenue |
| --- | --- |
| north | 390 |
| south | 200 |
| east | 84 |
| west | 450 |
"""


class TestHealthyRun:
    def test_a_real_looking_run_passes(self):
        assert check.assess(_HEALTHY, _SUMMARY) == []

    def test_a_program_that_computed_in_floats_still_passes(self):
        """The arithmetic is not under test here — the channel is. `390.0` is still 390."""
        floated = _SUMMARY.replace("390", "390.0").replace("450", "450.0")
        assert check.assess(_HEALTHY.replace("1124", "1124.0"), floated) == []

    def test_a_thousands_separator_in_the_models_prose_still_passes(self):
        """The total is read out of the model's own sentence, so `1,124` is a formatting
        choice rather than a broken stack. The program itself prints no separator."""
        assert check.assess(_HEALTHY.replace("1124", "1,124"), _SUMMARY) == []

    def test_the_summary_may_be_shaped_however_the_model_shaped_it(self):
        prose = "north made 390, south 200, east 84 and west 450 in the period."
        assert check.assess(_HEALTHY, prose) == []


class TestNumbersAreWholeTokensNotSubstrings:
    """A substring test passes the wrong magnitude, which is worse than passing nothing."""

    @pytest.mark.parametrize("wrong", ["11240", "21124", "1124.5", "112"])
    def test_a_total_that_merely_contains_the_digits_fails(self, wrong: str):
        assert any(
            "grand total" in reason
            for reason in check.assess(_HEALTHY.replace("1124", wrong), _SUMMARY)
        )

    def test_a_region_total_an_order_of_magnitude_out_fails(self):
        """`840` contains `84` and is not it."""
        wrong = _SUMMARY.replace("| east | 84 |", "| east | 840 |")
        assert any("east" in reason for reason in check.assess(_HEALTHY, wrong))


class TestATotalBelongsToItsOwnRegion:
    def test_swapped_values_fail(self):
        """Every expected string is still present, which is exactly why checking them
        independently was not a check at all."""
        swapped = _SUMMARY.replace("| north | 390 |", "| north | 200 |").replace(
            "| south | 200 |", "| south | 390 |"
        )
        failures = check.assess(_HEALTHY, swapped)
        assert any("north" in reason for reason in failures)
        assert any("south" in reason for reason in failures)

    def test_swapped_values_in_prose_fail_too(self):
        prose = "north made 200, south 390, east 84 and west 450 in the period."
        assert len(check.assess(_HEALTHY, prose)) == 2

    def test_a_region_named_without_its_total_says_so(self):
        partial = _SUMMARY.replace("| east | 84 |", "| east | (pending) |")
        assert any(
            "names the east region but not its total" in r
            for r in check.assess(_HEALTHY, partial)
        )

    def test_a_region_left_out_entirely_says_that_instead(self):
        missing = _SUMMARY.replace("| east | 84 |\n", "")
        assert any(
            "does not mention the east region" in r
            for r in check.assess(_HEALTHY, missing)
        )


class TestTheRunThatAnsweredAndSavedNothing:
    """The whole reason this checker takes a second argument."""

    def test_a_perfect_answer_with_no_file_fails(self):
        assert any("not on disk" in reason for reason in check.assess(_HEALTHY, None))

    def test_an_empty_file_fails(self):
        assert any("empty" in reason for reason in check.assess(_HEALTHY, "   \n"))

    def test_a_turn_that_delivered_nothing_fails_even_with_a_file_on_disk(self):
        """The stale-artifact case: `out/` holds an earlier run's summary, and the host's
        record of *this* turn is what settles it."""
        nothing = _HEALTHY.replace(
            "Delivered this turn into out/: summary.md",
            "Delivered this turn into out/: nothing",
        )
        assert any(
            "did not reach the sink this turn" in r
            for r in check.assess(nothing, _SUMMARY)
        )

    def test_a_run_that_never_reached_its_final_report_fails(self):
        truncated = _HEALTHY.replace("Delivered this turn into out/: summary.md", "")
        assert any(
            "did not reach its final report" in r
            for r in check.assess(truncated, _SUMMARY)
        )


class TestTheRunThatNeverRanASandbox:
    def test_no_disposal_line_fails(self):
        without = _HEALTHY.replace("Disposed 1 sandbox(es).", "")
        assert any(
            "did not run to completion" in r for r in check.assess(without, _SUMMARY)
        )

    def test_disposing_none_fails(self):
        """Answering without ever creating a sandbox is the T0 behaviour, not a pass."""
        none_disposed = _HEALTHY.replace("Disposed 1", "Disposed 0")
        assert any(
            "no sandbox was ever created" in r
            for r in check.assess(none_disposed, _SUMMARY)
        )
