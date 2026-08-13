"""The loose-match logic behind the live sample 09 check.

`scripts/check_live_inprocess_sample.py` is what the live workflow runs on a real
`samples/09_inprocess_bicep` run to decide whether the scripted SARIF actually came back
through the in-process fake and the agent. Its `assess` is a pure function, so the matching
itself is tested here — for free, on every PR — while the billable run that feeds it happens
only on dispatch and after a release.

These pin the two things that make the check worth having: it passes on a healthy run, and it
fails — naming the reason — on the shapes a broken stack produces: no diagnostic, no severity,
and no sandbox.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

_SCRIPT = (
    Path(__file__).resolve().parent.parent
    / "scripts"
    / "check_live_inprocess_sample.py"
)
_spec = importlib.util.spec_from_file_location("check_live_inprocess_sample", _SCRIPT)
assert _spec and _spec.loader
check = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check)

# A representative healthy run: the model's prose around the one diagnostic the fake scripts
# `bicep lint` to return, then the line the sample prints after disposing the in-process
# sandbox. The message text is the part the model may rewrap; the rule id and severity are not.
_HEALTHY = """\
I validated main.bicep and the Bicep compiler returned one diagnostic.

- [warning] no-hardcoded-location @ main.bicep:6 — Resource location should not be a \
hard-coded string; use a parameter, a variable, or an expression like resourceGroup().location.

Disposed 1 sandbox(es).
"""


class TestHealthyRun:
    def test_a_real_looking_run_passes(self):
        assert check.assess(_HEALTHY) == []

    def test_reworded_message_text_does_not_change_the_verdict(self):
        """The message is prose the model may rewrap; only the rule id and severity are matched."""
        reworded = _HEALTHY.replace(
            "Resource location should not be a hard-coded string",
            "Locations must not be hardcoded",
        )
        assert check.assess(reworded) == []


class TestABrokenStackFails:
    def test_no_sandbox_created_fails(self):
        # `Disposed 0` is the happy-path failure the whole live check exists to catch: the
        # stack answered from the model alone, never creating a sandbox.
        reasons = check.assess(
            _HEALTHY.replace("Disposed 1 sandbox(es).", "Disposed 0 sandbox(es).")
        )
        assert any(
            "no sandbox was ever created" in r.lower() or "0 sandbox" in r
            for r in reasons
        ), reasons

    def test_missing_diagnostic_fails_by_name(self):
        cleaned = "The file looks fine to me.\n\nDisposed 1 sandbox(es).\n"
        reasons = check.assess(cleaned)
        assert any("no-hardcoded-location" in r for r in reasons), reasons

    def test_a_dropped_rule_is_named(self):
        reasons = check.assess(
            _HEALTHY.replace("no-hardcoded-location", "some-other-rule")
        )
        assert any("no-hardcoded-location" in r for r in reasons), reasons

    def test_a_run_with_no_severity_is_caught(self):
        # Rule id present but the level dropped — the level is half of what an agent acts on.
        reasons = check.assess(
            "no-hardcoded-location in main.bicep\nDisposed 1 sandbox(es).\n"
        )
        assert any("warning" in r.lower() for r in reasons), reasons

    def test_an_incomplete_run_has_no_disposed_line(self):
        reasons = check.assess("[warning] no-hardcoded-location @ main.bicep:6\n")
        assert any("Disposed" in r or "run to completion" in r for r in reasons), (
            reasons
        )

    def test_empty_output_fails_rather_than_passing_vacuously(self):
        assert check.assess("") != []
