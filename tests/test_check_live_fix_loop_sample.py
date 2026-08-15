"""The match logic behind `scripts/check_live_fix_loop_sample.py`, tested on every PR.

`_HEALTHY` is a real run's output, trimmed — checked against one rather than written from
memory, since a fixture that has drifted makes every assertion below pass against a fiction.

Every tamper asserts `tampered != _HEALTHY` first. A substitution that matches nothing produces
a test that passes while testing the unmodified fixture, which is the one failure a green run
cannot show you.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "check_live_fix_loop_sample.py"
_spec = importlib.util.spec_from_file_location("check_live_fix_loop_sample", _SCRIPT)
assert _spec and _spec.loader
check = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check)

_FIXED_LINE = (
    "  faults fixed:       2 — no-unused-params: unused environmentName; "
    "BCP035: storageAccount without sku"
)

_HEALTHY = f"""\
== Turn 1: validate ==

Here are the diagnostics from validating `main.bicep`:

1. **no-unused-params** — Error — line 21
2. **BCP035** — Warning — line 31
3. **use-recent-api-versions** — Warning — line 31

  containers after turn 1: 1

== Turn 2: fix, then validate again ==

All three diagnostics are now resolved. Here's what changed:

1. **no-unused-params (line 21)** — Removed the unused `environmentName` parameter declaration.
2. **BCP035 (line 31)** — Added the required `sku` property to the storage account resource.
3. **use-recent-api-versions (line 31)** — Updated the API version to `2025-01-01`.

  containers after turn 2: 1

== What the file actually says now ==

  main.bicep changed: True
{_FIXED_LINE}
  faults remaining:   0 — none

== Independent check: compile what the model left ==

  build(main.bicep): no diagnostics
  lint(main.bicep): no diagnostics

  containers after the check: 1

Disposed 1 sandbox(es) after 2 turns and a check. Containers left: 0.
"""


def _tampered(original: str, replacement: str) -> list[str]:
    """Assess `_HEALTHY` with one substitution, refusing to run if it matched nothing."""
    text = _HEALTHY.replace(original, replacement)
    assert text != _HEALTHY, f"the substitution matched nothing — the fixture moved: {original!r}"
    return check.assess(text)


class TestHealthyRun:
    def test_a_real_run_passes(self):
        assert check.assess(_HEALTHY) == []


class TestOneSandboxAcrossTheRun:
    """The claim #304 asked for: the second acquire finds the first one's sandbox."""

    def test_a_second_container_on_the_fix_turn_is_caught(self):
        reasons = _tampered("containers after turn 2: 1", "containers after turn 2: 2")
        assert any("after turn 2, expected exactly 1" in r for r in reasons), reasons

    def test_a_second_container_on_the_independent_check_is_caught(self):
        # The third acquire. Turn 2 finding the sandbox warm could be a fluke of two calls
        # landing close together; the check runs after all the model's work and must still find
        # the same one.
        reasons = _tampered("containers after the check: 1", "containers after the check: 2")
        assert any("after the check, expected exactly 1" in r for r in reasons), reasons

    def test_a_container_left_behind_is_caught(self):
        reasons = _tampered("Containers left: 0.", "Containers left: 1.")
        assert any("left behind" in r for r in reasons), reasons

    def test_disposing_nothing_is_caught(self):
        reasons = _tampered("Disposed 1 sandbox(es)", "Disposed 0 sandbox(es)")
        assert any("reported disposing 0" in r for r in reasons), reasons

    def test_a_run_that_died_before_the_footer_is_caught(self):
        reasons = _tampered("Disposed 1 sandbox(es)", "Traceback (most recent call last)")
        assert any("did not run to completion" in r for r in reasons), reasons


class TestTheModelActuallyEdited:
    def test_an_unchanged_file_is_caught(self):
        """The whole reason the sample reads the file store instead of the model's prose.

        The fixture's turn 2 says "all three diagnostics are now resolved". Leaving that in place
        while the file is untouched is exactly the run this assertion exists to fail.
        """
        reasons = _tampered("main.bicep changed: True", "main.bicep changed: False")
        assert any("described a fix and did not make one" in r for r in reasons), reasons

    def test_a_change_that_fixed_nothing_is_caught(self):
        reasons = _tampered(
            _FIXED_LINE + "\n  faults remaining:   0 — none",
            "  faults fixed:       0 — none\n  faults remaining:   2 — "
            "no-unused-params: unused environmentName; BCP035: storageAccount without sku",
        )
        assert any("no fault was fixed" in r for r in reasons), reasons

    def test_a_tally_that_does_not_add_up_is_caught(self):
        reasons = _tampered("faults remaining:   0 — none", "faults remaining:   5 — invented")
        assert any("do not account for the 2 faults" in r for r in reasons), reasons


class TestTheCompilerHasTheLastWord:
    """The tally is a substring search; these hold it to a compiler that ran afterwards."""

    def test_a_fault_called_fixed_that_still_compiles_dirty_is_caught(self):
        reasons = _tampered(
            "  build(main.bicep): no diagnostics",
            "  build(main.bicep): 1 diagnostic(s)\n"
            "    [error] no-unused-params @ main.bicep:21: Parameter is declared but never used.",
        )
        assert any("counts no-unused-params as fixed" in r for r in reasons), reasons

    def test_a_fault_called_remaining_that_the_compiler_does_not_see_is_caught(self):
        reasons = _tampered(
            _FIXED_LINE + "\n  faults remaining:   0 — none",
            "  faults fixed:       1 — BCP035: storageAccount without sku\n"
            "  faults remaining:   1 — no-unused-params: unused environmentName",
        )
        assert any("counts no-unused-params as remaining" in r for r in reasons), reasons

    def test_a_missing_independent_compile_is_caught(self):
        reasons = _tampered("== Independent check: compile what the model left ==", "== gone ==")
        assert any("independent compile printed nothing" in r for r in reasons), reasons

    def test_only_one_compile_phase_is_caught(self):
        # build and lint are separate passes and a file can pass one while failing the other,
        # so a run that printed only one has not shown the file is clean.
        reasons = _tampered("  lint(main.bicep): no diagnostics\n", "")
        assert any("expected both build and lint" in r for r in reasons), reasons

    def test_the_age_rule_is_not_required_either_way(self):
        """`use-recent-api-versions` fires on the calendar, so neither answer may be demanded.

        A model that leaves the API version alone compiles with that one diagnostic still
        reported. Nothing about the two tracked faults changed, so the run must still pass —
        otherwise this check would go red on its own, months after anyone touched it.
        """
        reasons = _tampered(
            "  build(main.bicep): no diagnostics\n  lint(main.bicep): no diagnostics",
            "  build(main.bicep): 1 diagnostic(s)\n"
            "    [warning] use-recent-api-versions @ main.bicep:31: '2023-01-01' is 1322 days "
            "old, should be no more than 730 days old\n"
            "  lint(main.bicep): 1 diagnostic(s)\n"
            "    [warning] use-recent-api-versions @ main.bicep:31: '2023-01-01' is 1322 days "
            "old, should be no more than 730 days old",
        )
        assert reasons == [], reasons


class TestTurnOneReportedRealDiagnostics:
    """Scoped to turn 1's prose, because the rule ids appear again in the sample's own tally."""

    def test_a_first_turn_that_named_no_rule_is_caught(self):
        reasons = _tampered("1. **no-unused-params** — Error — line 21", "1. some problems")
        assert any("turn 1 did not name no-unused-params" in r for r in reasons), reasons

    def test_the_samples_own_tally_does_not_satisfy_the_first_turn(self):
        """The assertion that would rot silently if it searched the whole output.

        `no-unused-params` and `BCP035` are printed further down by the *sample*, in the fault
        tally, whatever the model said. Searching everything would pass on those literals even
        for a turn 1 that reported nothing at all — a check measuring its own harness.
        """
        gutted = _HEALTHY.replace(
            "1. **no-unused-params** — Error — line 21\n"
            "2. **BCP035** — Warning — line 31\n"
            "3. **use-recent-api-versions** — Warning — line 31",
            "I could not run the validator.",
        )
        assert gutted != _HEALTHY, "the substitution matched nothing — the fixture moved"
        assert "no-unused-params" in gutted, "the tally should still carry the rule id"
        assert "BCP035" in gutted, "the tally should still carry the rule id"

        reasons = check.assess(gutted)
        assert any("turn 1 did not name" in r for r in reasons), reasons

    def test_a_run_with_no_first_turn_at_all_is_caught(self):
        reasons = _tampered("== Turn 1: validate ==", "== nothing ==")
        assert any("no turn 1 section" in r for r in reasons), reasons
