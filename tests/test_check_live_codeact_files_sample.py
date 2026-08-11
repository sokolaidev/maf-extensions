"""The two-halved judgement behind the files-channel live check.

`scripts/check_live_codeact_files_sample.py` runs on a real `samples/08_docker_codeact_files`
run to decide whether the published stack moved a file in *and* a file out. Its `assess` is a
pure function, so the judgement is tested here — on every PR — while the run that feeds it
happens only on dispatch and after a release.

What these pin is the half that is easy to lose: an output-only check would pass a turn that
computed the right total and landed nothing, and that turn is precisely the regression this
sample exists to catch. So the failing cases matter more than the healthy one.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

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
#: report of what reached the sink.
_HEALTHY = """\
The grand total across all regions is 1124. I saved the per-region breakdown as summary.md.

Disposed 1 sandbox(es).
Landed in out/: summary.md
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
        """The arithmetic is not under test here — the channel is. `390.0` carries `390`."""
        floated = _SUMMARY.replace("390", "390.0").replace("450", "450.0")
        assert check.assess(_HEALTHY.replace("1124", "1124.0"), floated) == []

    def test_the_summary_may_be_shaped_however_the_model_shaped_it(self):
        prose = "north made 390, south 200, east 84 and west 450 in the period."
        assert check.assess(_HEALTHY, prose) == []


class TestTheRunThatAnsweredAndSavedNothing:
    """The whole reason this checker takes a second argument."""

    def test_a_perfect_answer_with_no_file_fails(self):
        failures = check.assess(_HEALTHY, None)
        assert any("not on disk" in reason for reason in failures)

    def test_an_empty_file_fails(self):
        assert any("empty" in reason for reason in check.assess(_HEALTHY, "   \n"))

    def test_a_host_report_naming_nothing_fails(self):
        nothing_landed = _HEALTHY.replace(
            "Landed in out/: summary.md", "Landed in out/: nothing"
        )
        failures = check.assess(nothing_landed, None)
        assert any("does not include" in reason for reason in failures)

    def test_a_run_that_never_reached_its_final_report_fails(self):
        truncated = _HEALTHY.replace("Landed in out/: summary.md", "")
        assert any(
            "did not reach its final report" in r
            for r in check.assess(truncated, _SUMMARY)
        )


class TestTheRunThatNeverReadTheFile:
    def test_a_missing_grand_total_fails(self):
        wrong = _HEALTHY.replace("1124", "9999")
        assert any("grand total" in reason for reason in check.assess(wrong, _SUMMARY))

    def test_a_summary_missing_a_region_fails(self):
        partial = _SUMMARY.replace("| east | 84 |\n", "")
        assert any("east" in reason for reason in check.assess(_HEALTHY, partial))

    def test_a_summary_with_a_region_but_not_its_total_fails(self):
        wrong = _SUMMARY.replace("| east | 84 |", "| east | 12 |")
        failures = check.assess(_HEALTHY, wrong)
        assert any("east's total of 84" in reason for reason in failures)


class TestTheRunThatNeverRanASandbox:
    def test_no_disposal_line_fails(self):
        assert any(
            "did not run to completion" in reason
            for reason in check.assess(
                _HEALTHY.replace("Disposed 1 sandbox(es).", ""), _SUMMARY
            )
        )

    def test_disposing_none_fails(self):
        """Answering without ever creating a sandbox is the T0 behaviour, not a pass."""
        none_disposed = _HEALTHY.replace("Disposed 1", "Disposed 0")
        assert any(
            "no sandbox was ever created" in r
            for r in check.assess(none_disposed, _SUMMARY)
        )
