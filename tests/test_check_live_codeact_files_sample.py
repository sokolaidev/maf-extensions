"""The two-halved judgement behind the files-channel live check.

`scripts/check_live_codeact_files_sample.py` runs on a real `samples/08_docker_codeact_files`
run to decide whether the published stack moved a file in *and* a file out. Its `assess` is a
pure function, so the judgement is tested here — on every PR — while the run that feeds it
happens only on dispatch and after a release. The failing cases carry the weight: an
output-only check would pass a turn that computed the right total and landed nothing, which is
the regression this sample exists to catch.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "check_live_codeact_files_sample.py"
_spec = importlib.util.spec_from_file_location("check_live_codeact_files_sample", _SCRIPT)
assert _spec and _spec.loader
check = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check)

#: A representative healthy run: the model's reply, the disposal line, and the host's own
#: record of what reached the sink this turn.
_HEALTHY = """\
The grand total across all regions is 1124. I saved the per-region breakdown as summary.md.

Disposed 1 sandbox(es).
Delivered this turn into out/: ["summary.md"]
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

    @pytest.mark.parametrize(
        "wrong",
        ["1,390", "390,000", "1 390", "390 000", "1\u00a0390", "390\u202f000"],
        ids=["comma", "comma-after", "space", "space-after", "nbsp", "narrow-nbsp"],
    )
    def test_a_total_inside_a_larger_grouped_number_fails(self, wrong: str):
        """Tolerating a thousands separator reopened the substring hole one group over: a
        separator is not a token boundary, so `390` sat inside both `1,390` and `390,000`."""
        summary = _SUMMARY.replace("| north | 390 |", f"| north | {wrong} |")
        assert any("north" in reason for reason in check.assess(_HEALTHY, summary))

    @pytest.mark.parametrize("wrong", ["11,124", "1,124,000", "11 124"])
    def test_a_grand_total_inside_a_larger_grouped_number_fails(self, wrong: str):
        assert any(
            "grand total" in reason
            for reason in check.assess(_HEALTHY.replace("1124", wrong), _SUMMARY)
        )

    @pytest.mark.parametrize("shape", ["390", "390.0", "390 ", " 390"])
    def test_the_separator_guard_does_not_reject_an_ordinary_cell(self, shape: str):
        """The guards reject an adjacent *group*, not the whitespace a table puts around a
        value — which is the way a fix like this goes wrong."""
        summary = _SUMMARY.replace("| north | 390 |", f"| north |{shape}|")
        assert check.assess(_HEALTHY, summary) == []


class TestLettersAreNotABoundaryEither:
    @pytest.mark.parametrize("wrong", ["1124e3", "1124E3", "1124kg", "x1124"])
    def test_a_total_glued_to_letters_fails(self, wrong: str):
        """`1124e3` is 1,124,000 and contains `1124`, which is the whole problem."""
        assert any(
            "grand total" in reason
            for reason in check.assess(_HEALTHY.replace("1124", wrong), _SUMMARY)
        )

    @pytest.mark.parametrize("wrong", ["390kg", "usd390"])
    def test_a_region_total_glued_to_letters_fails(self, wrong: str):
        summary = _SUMMARY.replace("| north | 390 |", f"| north | {wrong} |")
        assert any("north" in reason for reason in check.assess(_HEALTHY, summary))

    @pytest.mark.parametrize("shape", ["(1124)", "**1124**", "1124", "`1124`"])
    def test_ordinary_punctuation_around_a_total_still_passes(self, shape: str):
        """The guard is about word characters, not about every neighbour a total can have."""
        assert check.assess(_HEALTHY.replace("1124", shape), _SUMMARY) == []


class TestASignBelongsToTheNumber:
    @pytest.mark.parametrize(
        "signed",
        ["-1124", "+1124", "\u22121124", "\uff0d1124", "\uff0b1124"],
        ids=["hyphen-minus", "plus", "minus-sign", "fullwidth-minus", "fullwidth-plus"],
    )
    def test_a_signed_grand_total_fails(self, signed: str):
        """`-1124` is not 1124, however much of it looks like it — and this checker reads
        model-authored prose, where a true minus is as likely as the ASCII one."""
        assert any(
            "grand total" in reason
            for reason in check.assess(_HEALTHY.replace("1124", signed), _SUMMARY)
        )

    @pytest.mark.parametrize("signed", ["-390", "\u2212390"], ids=["hyphen-minus", "minus-sign"])
    def test_a_signed_region_total_fails(self, signed: str):
        negative = _SUMMARY.replace("| north | 390 |", f"| north | {signed} |")
        assert any("north" in reason for reason in check.assess(_HEALTHY, negative))


class TestNamesAreWholeWords:
    def test_a_compound_region_label_is_not_read_as_two_regions(self):
        """As substrings, `northwest` contains both `north` and `west`, so a mislabelled row
        satisfied whichever of the two had no row of its own."""
        mislabelled = _SUMMARY.replace("| west | 450 |", "| northwest | 450 |")
        assert any(
            "does not mention the west region" in reason
            for reason in check.assess(_HEALTHY, mislabelled)
        )

    @pytest.mark.parametrize("wrong", ["not-summary.md", "summary.md.bak"])
    def test_a_delivery_line_naming_a_lookalike_fails(self, wrong: str):
        """The pairing that matters: an earlier run's `summary.md` still on disk, and this
        turn delivering something whose name merely contains the declared one."""
        reported = _HEALTHY.replace('["summary.md"]', json.dumps([wrong]))
        assert any(
            "did not reach the sink this turn" in reason
            for reason in check.assess(reported, _SUMMARY)
        )

    def test_a_delivery_line_naming_several_files_still_finds_the_declared_one(self):
        several = _HEALTHY.replace(
            '["summary.md"]', json.dumps(["notes.txt", "summary.md", "chart.png"])
        )
        assert check.assess(several, _SUMMARY) == []

    def test_one_delivery_whose_name_contains_a_comma_is_not_read_as_two(self):
        """A comma is legal in an artifact name, so a delivery called `notes, summary.md` is
        one file and not two — and reading it as two would find the declared name in a turn
        that never delivered it."""
        comma = _HEALTHY.replace('["summary.md"]', json.dumps(["notes, summary.md"]))
        assert any(
            "did not reach the sink this turn" in reason for reason in check.assess(comma, _SUMMARY)
        )

    def test_a_delivery_line_that_is_not_json_fails(self):
        unparseable = _HEALTHY.replace('["summary.md"]', "summary.md")
        assert any(
            "did not reach the sink this turn" in reason
            for reason in check.assess(unparseable, _SUMMARY)
        )


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
            "names the east region but not its total" in r for r in check.assess(_HEALTHY, partial)
        )

    def test_a_region_left_out_entirely_says_that_instead(self):
        missing = _SUMMARY.replace("| east | 84 |\n", "")
        assert any("does not mention the east region" in r for r in check.assess(_HEALTHY, missing))


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
            'Delivered this turn into out/: ["summary.md"]',
            "Delivered this turn into out/: []",
        )
        assert any("did not reach the sink this turn" in r for r in check.assess(nothing, _SUMMARY))

    def test_a_run_that_never_reached_its_final_report_fails(self):
        truncated = _HEALTHY.replace('Delivered this turn into out/: ["summary.md"]', "")
        assert any("did not reach its final report" in r for r in check.assess(truncated, _SUMMARY))


class TestTheRunThatNeverRanASandbox:
    def test_no_disposal_line_fails(self):
        without = _HEALTHY.replace("Disposed 1 sandbox(es).", "")
        assert any("did not run to completion" in r for r in check.assess(without, _SUMMARY))

    def test_disposing_none_fails(self):
        """Answering without ever creating a sandbox is the T0 behaviour, not a pass."""
        none_disposed = _HEALTHY.replace("Disposed 1", "Disposed 0")
        assert any(
            "no sandbox was ever created" in r for r in check.assess(none_disposed, _SUMMARY)
        )
