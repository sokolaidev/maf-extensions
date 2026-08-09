"""The loose-match logic behind the live sample check.

`scripts/check_live_sample.py` is what the live workflow runs on a real `samples/01_acas_bicep`
run to decide whether the published stack actually validated the file. Its `assess` is a pure
function, so the matching itself is tested here — for free, on every PR — while the billable
run that feeds it happens only on dispatch and after a release.

These pin the two things that make the check worth having: it passes on a healthy run whose
diagnostics carry the day counts and version lists that drift on their own, and it fails —
naming the reason — on the two shapes a broken stack produces: no diagnostics, and no sandbox.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "check_live_sample.py"
_spec = importlib.util.spec_from_file_location("check_live_sample", _SCRIPT)
assert _spec and _spec.loader
check = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check)

# A representative healthy run: the model's prose around the three diagnostics the sample's
# main.bicep produces, then the line the sample prints after disposing the sandbox. The day
# count and the aged API version are exactly the parts that move with no code change.
_HEALTHY = """\
I validated main.bicep and the Bicep compiler returned three diagnostics.

- [error] no-unused-params @ main.bicep:21 — Parameter "environmentName" is declared but never used.
- [warning] BCP035 @ main.bicep:31 — The "resource" declaration is missing the required property "sku".
- [warning] use-recent-api-versions @ main.bicep:31 — '2023-01-01' is 1287 days old; use a newer API version.

Disposed 1 sandbox(es).
"""


class TestHealthyRun:
    def test_a_real_looking_run_passes(self):
        assert check.assess(_HEALTHY) == []

    def test_the_day_count_and_version_are_not_matched(self):
        """Changing the drifting parts must not change the verdict."""
        drifted = _HEALTHY.replace("1287 days", "3650 days").replace(
            "2023-01-01", "2019-04-01"
        )
        assert check.assess(drifted) == []


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

    def test_missing_diagnostics_fail_by_name(self):
        cleaned = "The file looks fine to me.\n\nDisposed 1 sandbox(es).\n"
        reasons = check.assess(cleaned)
        assert any("no-unused-params" in r for r in reasons), reasons
        assert any("BCP035" in r for r in reasons), reasons

    def test_a_dropped_rule_is_named(self):
        reasons = check.assess(_HEALTHY.replace("no-unused-params", "some-other-rule"))
        assert any("no-unused-params" in r for r in reasons), reasons
        assert not any("BCP035" in r for r in reasons), (
            "BCP035 was present and must not be reported"
        )

    def test_a_run_with_no_error_severity_is_caught(self):
        # Rule ids present but no severity rendered — the level is half of what an agent acts on.
        reasons = check.assess("no-unused-params and BCP035\nDisposed 1 sandbox(es).\n")
        assert any("error" in r.lower() for r in reasons), reasons

    def test_a_warning_alone_does_not_satisfy_the_severity_check(self):
        # It keys on `error`, not on any severity word, so a run that rendered only a warning
        # still fails — which is what keeps the check off the drift-prone `use-recent` warning.
        reasons = check.assess(
            "[warning] BCP035 and no-unused-params\nDisposed 1 sandbox(es).\n"
        )
        assert any("error" in r.lower() for r in reasons), reasons

    def test_an_incomplete_run_has_no_disposed_line(self):
        reasons = check.assess("[error] no-unused-params\n[warning] BCP035\n")
        assert any("Disposed" in r or "run to completion" in r for r in reasons), (
            reasons
        )

    def test_empty_output_fails_rather_than_passing_vacuously(self):
        assert check.assess("") != []
