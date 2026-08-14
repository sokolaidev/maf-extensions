"""The loose-match logic behind the live sample check.

`scripts/check_live_sample.py` is what the live workflow runs on a real `samples/01_acas_bicep`
run to decide whether the published stack actually validated the file. Its `assess` is a pure
function, so the matching itself is tested here — for free, on every PR — while the billable
run that feeds it happens only on dispatch and after a release.

These pin the two things that make the check worth having: it passes on a healthy run whose
diagnostics carry the day counts and version lists that drift on their own, and it fails —
naming the reason — on the shapes a broken stack produces: no diagnostics, no sandbox, and a
run that linted against the CLI's built-in defaults because `bicepconfig.json` was never found
(#308). That last one is the only shape here that looks entirely healthy, so it is the one the
unit test has to carry: it costs nothing to catch here and a billable run to catch anywhere else.
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

# The same run against an image whose `bicepconfig.json` is not at the root the tool writes to.
# Bicep finds that file only by walking up from the source, so it is simply never read and every
# rule falls back to its built-in default: `no-unused-params` reports at `warning` instead of the
# configured `error`, and `use-recent-api-versions` — which the config switches on — is gone. The
# compiler ran, SARIF parsed, diagnostics rendered, the sandbox came up and went away again. The
# prose says "error" the way a model summarising two warnings would, so even the loose severity
# match is satisfied. Nothing about this output is unhealthy except the rule set behind it.
_STALE_IMAGE = """\
I validated main.bicep and the Bicep compiler returned two diagnostics. Neither one is an error.

- [warning] no-unused-params @ main.bicep:21 — Parameter "environmentName" is declared but never used.
- [warning] BCP035 @ main.bicep:31 — The "resource" declaration is missing the required property "sku".

Disposed 1 sandbox(es).
"""


class TestHealthyRun:
    def test_a_real_looking_run_passes(self):
        assert check.assess(_HEALTHY) == []

    def test_the_day_count_and_version_are_not_matched(self):
        """Changing the drifting parts must not change the verdict."""
        drifted = _HEALTHY.replace("1287 days", "3650 days").replace("2023-01-01", "2019-04-01")
        assert check.assess(drifted) == []


class TestABrokenStackFails:
    def test_no_sandbox_created_fails(self):
        # `Disposed 0` is the happy-path failure the whole live check exists to catch: the
        # stack answered from the model alone, never creating a sandbox.
        reasons = check.assess(
            _HEALTHY.replace("Disposed 1 sandbox(es).", "Disposed 0 sandbox(es).")
        )
        assert any(
            "no sandbox was ever created" in r.lower() or "0 sandbox" in r for r in reasons
        ), reasons

    def test_missing_diagnostics_fail_by_name(self):
        cleaned = "The file looks fine to me.\n\nDisposed 1 sandbox(es).\n"
        reasons = check.assess(cleaned)
        # Keyed on the missing-rule wording rather than on the rule name alone: this output
        # also trips the config check, whose message names `no-unused-params` too, and a bare
        # substring test would pass on that reason even if this loop stopped running.
        assert any("diagnostic 'no-unused-params' is not in the output" in r for r in reasons), (
            reasons
        )
        assert any("diagnostic 'BCP035' is not in the output" in r for r in reasons), reasons

    def test_a_dropped_rule_is_named(self):
        reasons = check.assess(_HEALTHY.replace("no-unused-params", "some-other-rule"))
        assert any("no-unused-params" in r for r in reasons), reasons
        assert not any("BCP035" in r for r in reasons), (
            "BCP035 was present and must not be reported"
        )

    def test_a_run_with_no_error_severity_is_caught(self):
        # Rule ids present but no severity rendered — the level is half of what an agent acts on.
        # Keyed on the severity check's own wording: the config check fails on this output too
        # and its message contains `[error]`, so `"error" in r` alone would prove nothing.
        reasons = check.assess("no-unused-params and BCP035\nDisposed 1 sandbox(es).\n")
        assert any("no 'error' severity anywhere in the output" in r for r in reasons), reasons

    def test_a_warning_alone_does_not_satisfy_the_severity_check(self):
        # It keys on `error`, not on any severity word, so a run that rendered only a warning
        # still fails.
        reasons = check.assess("[warning] BCP035 and no-unused-params\nDisposed 1 sandbox(es).\n")
        assert any("no 'error' severity anywhere in the output" in r for r in reasons), reasons

    def test_an_incomplete_run_has_no_disposed_line(self):
        reasons = check.assess("[error] no-unused-params\n[warning] BCP035\n")
        assert any("Disposed" in r or "run to completion" in r for r in reasons), reasons

    def test_empty_output_fails_rather_than_passing_vacuously(self):
        assert check.assess("") != []


class TestTheRuleSetTheRepositoryAskedFor:
    """#308: a run that found no `bicepconfig.json` and linted against built-in defaults.

    The failure the rest of this file cannot see. Sample 01 boots a disk image, which is a
    snapshot of a registry tag rather than a live reference to it, so an image built before the
    work-dir root moved keeps booting after the tool stops writing there. Nothing goes red on
    its own: the compiler is real, the diagnostics are real, and only the rule set is weaker
    than the repository asked for.
    """

    def test_a_stale_image_run_is_caught(self):
        reasons = check.assess(_STALE_IMAGE)
        assert any("bicepconfig.json was not discovered" in r for r in reasons), reasons

    def test_a_stale_image_run_passes_every_other_assertion(self):
        """Why this check had to exist rather than tightening one of the others.

        Both required rule ids come back, a sandbox is created and disposed, and the word
        `error` renders. Exactly one thing is wrong with the run, and before #308 nothing here
        was looking at it.
        """
        assert len(check.assess(_STALE_IMAGE)) == 1, check.assess(_STALE_IMAGE)

    def test_the_switched_on_rule_alone_is_enough(self):
        # The promotion is not visible — no `[error]` anywhere — but a rule the config switches
        # on was reported, which it could not be without one.
        assert check.config_was_discovered(
            "- [warning] use-recent-api-versions @ main.bicep:31 — '2023-01-01' is 1287 days old."
        )

    def test_naming_a_rule_is_not_reporting_it(self):
        """A stale run that *mentions* the missing rule must not read as a healthy one.

        The trap a substring test walks into. Without the config every one of these is a true
        sentence about the run, and each names the rule while reporting the opposite.
        """
        for prose in (
            "use-recent-api-versions is missing from the output.",
            "No use-recent-api-versions diagnostic was produced.",
            "There is no use-recent-api-versions finding, and no-unused-params is only a warning.",
        ):
            assert not check.config_was_discovered(prose), prose

    def test_a_stale_run_naming_the_missing_rule_still_fails(self):
        # End to end, not just the predicate: the whole assess() verdict must stay red.
        named = _STALE_IMAGE.replace(
            "Neither one is an error.",
            "Neither one is an error, and use-recent-api-versions was not reported.",
        )
        assert any("bicepconfig.json was not discovered" in r for r in check.assess(named)), (
            check.assess(named)
        )

    def test_the_promoted_severity_alone_is_enough(self):
        # The mirror case: the drift-prone rule is missing from the summary and the promotion is
        # still visible. A healthy run must not go red for dropping the one rule this check has
        # never been allowed to depend on.
        dropped = (
            "\n".join(
                line for line in _HEALTHY.splitlines() if "use-recent-api-versions" not in line
            )
            + "\n"
        )
        assert check.config_was_discovered(dropped)
        assert check.assess(dropped) == []

    def test_a_reformatted_table_row_still_counts(self):
        # The workload renders `[<level>] <rule> @ <loc>`, but a model asked to list diagnostics
        # may tabulate them. Line-scoped rather than adjacent, so both shapes read the same.
        assert check.config_was_discovered(
            "| no-unused-params | [error] | main.bicep:21 | declared but never used |"
        )

    def test_prose_about_the_rule_does_not_count(self):
        """Words between a rule and a severity mean the two are not one diagnostic.

        Each of these is a sentence a run *without* the config would truthfully produce, and
        the second carries a real `[error]` on the same line — belonging to another rule.
        """
        for prose in (
            "- [warning] no-unused-params @ main.bicep:21 — not an error, only a warning.",
            "- [error] BCP057 @ main.bicep:9 — undefined name; no-unused-params stayed a warning.",
            "no-unused-params did not come back as an error this time.",
        ):
            assert not check.config_was_discovered(prose), prose

    def test_an_unbracketed_severity_is_not_a_rendered_diagnostic(self):
        # The workload always brackets the level, so a bare word beside a rule id is prose. This
        # is the one thing adjacency alone does not reject.
        assert not check.config_was_discovered("no-unused-params error")
        assert check.config_was_discovered("no-unused-params [error]")

    def test_an_error_on_another_line_does_not_count(self):
        # Some other diagnostic being an error says nothing about whether this one was promoted.
        assert not check.config_was_discovered(
            "- [error] BCP057 @ main.bicep:9 — the name 'foo' does not exist.\n"
            "- [warning] no-unused-params @ main.bicep:21 — declared but never used.\n"
        )

    def test_a_healthy_run_shows_both_tells(self):
        # The premise the either-or rests on: on a good run neither signal is doing the work
        # alone, so losing one to model formatting costs nothing.
        assert all(tell.search(_HEALTHY) for tell in check._CONFIG_TELLS)
