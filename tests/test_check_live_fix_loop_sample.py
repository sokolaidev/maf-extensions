"""The match logic behind `scripts/check_live_fix_loop_sample.py`, tested on every PR.

`_HEALTHY` is a real run's output, trimmed — checked against one rather than written from
memory, since a fixture that has drifted makes every assertion below pass against a fiction.

Every tamper asserts `tampered != _HEALTHY` first. A substitution that matches nothing produces
a test that passes while testing the unmodified fixture, which is the one failure a green run
cannot show you.

The last class does not test the checker at all — it tests the sample's own `faults_left`,
because that is where a repair can be miscounted before the checker ever sees the output.
"""

from __future__ import annotations

import ast
import importlib.util
import re
import subprocess
from pathlib import Path

import pytest

#: Every way this file could start a process. `subprocess.run` alone is not the set: an async
#: sample reaches for `asyncio.create_subprocess_exec`, and `os` is already imported here.
#:
#: Lexical, and therefore best-effort. It binds "no *direct* call spawns a process outside
#: `containers()`", which is the drift a refactor writes by accident. Reaching a spawner through
#: an alias, a local variable or `getattr` defeats it, and no amount of pattern-matching over
#: call sites can bind the runtime property — that would need the sample importable, and this
#: workspace does not install `agent-framework-openai`.
_SPAWNS = frozenset(
    {
        "subprocess.run",
        "subprocess.Popen",
        "subprocess.call",
        "subprocess.check_call",
        "subprocess.check_output",
        "subprocess.getoutput",
        "subprocess.getstatusoutput",
        "os.system",
        "os.popen",
        "os.execv",
        "os.spawnv",
        "asyncio.create_subprocess_exec",
        "asyncio.create_subprocess_shell",
    }
)

#: The exception names the sample may name, resolved so a handler can be asked "would this be
#: caught" rather than "is it spelled this way". Anything unlisted is ignored, which fails
#: closed: an unrecognised handler covers nothing.
_EXCEPTIONS: dict[str, type[BaseException]] = {
    "OSError": OSError,
    "FileNotFoundError": FileNotFoundError,
    "PermissionError": PermissionError,
    "Exception": Exception,
    "BaseException": BaseException,
    "subprocess.CalledProcessError": subprocess.CalledProcessError,
    "subprocess.SubprocessError": subprocess.SubprocessError,
}


class _Unmatchable(BaseException):
    """Stands in for a handler naming nothing `_EXCEPTIONS` knows, so it covers nothing."""


_ROOT = Path(__file__).resolve().parent.parent
_SCRIPT = _ROOT / "scripts" / "check_live_fix_loop_sample.py"
_spec = importlib.util.spec_from_file_location("check_live_fix_loop_sample", _SCRIPT)
assert _spec and _spec.loader
check = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check)

_HEALTHY = """\
== Turn 1: author main.bicep, then validate it ==

Validation complete. Here are the 3 diagnostics, one line each:

1. `no-unused-params` — error — line 3 (`environmentName` declared but never used)
2. `BCP035` — warning — line 5 (resource missing required `sku` property)
3. `use-recent-api-versions` — warning — line 5 (`2023-01-01` is over the 730-day guideline)

  [measured] bicep_validate calls in turn 1: 1
  [measured] containers after turn 1: 1

== What the compiler says about the file turn 1 wrote ==

  build(main.bicep): 3 diagnostic(s)
    [error] no-unused-params @ main.bicep:3: Parameter "environmentName" is never used.
    [warning] BCP035 @ main.bicep:5: The "resource" declaration is missing "sku".
    [warning] use-recent-api-versions @ main.bicep:5: '2023-01-01' is 1322 days old.
  lint(main.bicep): 3 diagnostic(s)
    [error] no-unused-params @ main.bicep:3: Parameter "environmentName" is never used.
    [warning] BCP035 @ main.bicep:5: The "resource" declaration is missing "sku".
    [warning] use-recent-api-versions @ main.bicep:5: '2023-01-01' is 1322 days old.

  [measured] tracked faults in the authored file: 2 — no-unused-params; BCP035
  [measured] containers after the baseline compile: 1

== Turn 2: fix, then validate again ==

Validation is clean — zero diagnostics. What changed:

1. `no-unused-params` — `environmentName` is now used in the storage account's tags.
2. `BCP035` — added a `sku` block with a `skuName` parameter.
3. `use-recent-api-versions` — bumped the API version to `2025-01-01`.

  [measured] bicep_validate calls in turn 2: 1
  [measured] containers after turn 2: 1

== What the compiler says about the file the model left ==

  build(main.bicep): no diagnostics
  lint(main.bicep): no diagnostics

  [measured] containers after the check: 1

== The work product ==

  [measured] main.bicep authored in turn 1: True
  [measured] main.bicep changed by turn 2:  True
  [measured] storage account and output intact: True
  [measured] faults fixed:       2 — no-unused-params; BCP035
  [measured] faults remaining:   0 — none

  [measured] Disposed 1 sandbox(es) after 2 turns and a check. Containers left: 0.
"""

#: A run where the model fixed only `BCP035` and left the unused parameter. Both the tally and
#: the compiler say so, so it is *consistent* — the checker's job here is to accept it, since
#: the sample deliberately reports which fault was fixed rather than demanding both.
_PARTIAL = (
    _HEALTHY.replace(
        "  build(main.bicep): no diagnostics\n  lint(main.bicep): no diagnostics",
        "  build(main.bicep): 1 diagnostic(s)\n"
        "    [error] no-unused-params @ main.bicep:21: Parameter is declared but never used.\n"
        "  lint(main.bicep): 1 diagnostic(s)\n"
        "    [error] no-unused-params @ main.bicep:21: Parameter is declared but never used.",
    )
    .replace("faults fixed:       2 — no-unused-params; BCP035", "faults fixed:       1 — BCP035")
    .replace("faults remaining:   0 — none", "faults remaining:   1 — no-unused-params")
)


def _caught_by(handler: ast.ExceptHandler) -> tuple[type[BaseException], ...]:
    """The exception classes an `except` clause names, as a tuple `issubclass` can be asked."""
    if handler.type is None:
        return (BaseException,)
    named = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
    return tuple(_EXCEPTIONS[n] for e in named if (n := ast.unparse(e)) in _EXCEPTIONS) or (
        _Unmatchable,
    )


def _answers(handler: ast.ExceptHandler) -> bool:
    """Whether the handler itself returns, rather than containing something that does.

    A `return` inside a nested `def` in the handler is that function's answer, not this one's,
    so nested scopes are pruned. `ast.walk` cannot do that — it has already queued the
    children by the time you see the node — so the descent is written out.
    """
    stack: list[ast.AST] = list(handler.body)
    while stack:
        node = stack.pop()
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda | ast.ClassDef):
            continue
        if isinstance(node, ast.Return):
            return True
        stack.extend(ast.iter_child_nodes(node))
    return False


def _tampered(original: str, replacement: str, *, base: str | None = None) -> list[str]:
    """Assess a fixture with one substitution, refusing to run if it matched nothing."""
    source = _HEALTHY if base is None else base
    text = source.replace(original, replacement)
    assert text != source, f"the substitution matched nothing — the fixture moved: {original!r}"
    return check.assess(text)


class TestHealthyRuns:
    def test_a_real_run_passes(self):
        assert check.assess(_HEALTHY) == []

    def test_a_run_that_fixed_only_one_fault_passes(self):
        """The sample reports which fault was fixed rather than requiring both.

        This fixture is also the only one carrying rendered diagnostics through the whole
        pipeline, so it is what proves the sweep below tolerates a diagnostic the tally
        accounted for instead of rejecting every diagnostic it sees.
        """
        assert check.assess(_PARTIAL) == []


class TestTheSecondAcquireActuallyHappened:
    """A container count cannot show this, and inferring it from one is the trap.

    If the fix turn edits the file and never validates it, no second `acquire` is made at all —
    but turn 1's container is still there to be counted, so the count reads 1 and the run looks
    like reuse. The final compile passes too. CI would go green having never exercised the
    claim the sample exists to make.
    """

    def test_a_fix_turn_that_never_validated_is_caught(self):
        reasons = _tampered(
            "bicep_validate calls in turn 2: 1", "bicep_validate calls in turn 2: 0"
        )
        assert any("turn 2 never called bicep_validate" in r for r in reasons), reasons

    def test_a_first_turn_that_never_validated_is_caught(self):
        reasons = _tampered(
            "bicep_validate calls in turn 1: 1", "bicep_validate calls in turn 1: 0"
        )
        assert any("turn 1 never called bicep_validate" in r for r in reasons), reasons

    def test_a_run_that_does_not_report_call_counts_at_all_is_caught(self):
        # The regression if the sample stops printing them: every other assertion still passes,
        # and the check would silently go back to inferring the acquire from the count.
        reasons = _tampered("bicep_validate calls in turn 2: 1\n", "")
        assert any("did not report how many times" in r for r in reasons), reasons


class TestOneSandboxAcrossTheRun:
    def test_a_second_container_on_the_fix_turn_is_caught(self):
        reasons = _tampered("containers after turn 2: 1", "containers after turn 2: 2")
        assert any("after turn 2, expected exactly 1" in r for r in reasons), reasons

    def test_a_second_container_on_the_baseline_compile_is_caught(self):
        # The acquire between the two turns. It is the program's, not the model's, so a second
        # container here would mean get-or-create failed on a call nothing else covers.
        reasons = _tampered(
            "containers after the baseline compile: 1", "containers after the baseline compile: 2"
        )
        assert any("after the baseline compile, expected exactly 1" in r for r in reasons), reasons

    def test_a_second_container_on_the_final_compile_is_caught(self):
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


class TestTurnOneActuallyAuthoredTheFile:
    """The half #304 asks for that a pre-seeded file would fake.

    The store starts empty, so every later signal is about a file the model wrote. A run where
    turn 1 wrote nothing has no subject at all, and one where it wrote something already clean
    has nothing for turn 2 to repair — both would otherwise pass every assertion about reuse.
    """

    def test_a_turn_one_that_wrote_nothing_is_caught(self):
        reasons = _tampered(
            "main.bicep authored in turn 1: True", "main.bicep authored in turn 1: False"
        )
        assert any("turn 1 left no main.bicep" in r for r in reasons), reasons

    def test_a_run_that_does_not_report_authoring_is_caught(self):
        reasons = _tampered("  [measured] main.bicep authored in turn 1: True\n", "")
        assert any("whether turn 1 wrote main.bicep" in r for r in reasons), reasons

    def test_an_authored_file_with_no_fault_to_fix_is_caught(self):
        """A clean authored file means the brief did not do its job, not that the run went well.

        Nothing downstream would object: no tracked fault to fix, a clean final compile, and the
        tally adding to zero on both sides. The sample would report a successful fix loop having
        never run one.
        """
        reasons = _tampered(
            "  [measured] tracked faults in the authored file: 2 — no-unused-params; BCP035",
            "  [measured] tracked faults in the authored file: 0 — none",
        )
        assert any("had no tracked fault in it" in r for r in reasons), reasons

    def test_a_turn_two_echo_of_the_baseline_heading_does_not_lose_the_block(self):
        """The baseline block sits between the turns, so a turn-2 echo comes *after* it.

        Taking the last matching heading then lands on the echo, and the end marker is already
        behind it — which reads as a missing block and fails a healthy run. The rule has to be
        the last heading its end marker still follows.
        """
        echoed = _HEALTHY.replace(
            "  [measured] bicep_validate calls in turn 2: 1",
            "Recapping == What the compiler says about the file turn 1 wrote ==\n\n"
            "  [measured] bicep_validate calls in turn 2: 1",
        )
        assert echoed != _HEALTHY, "the substitution matched nothing — the fixture moved"
        assert check.assess(echoed) == [], check.assess(echoed)

    def test_a_missing_baseline_compile_is_caught(self):
        reasons = _tampered(
            "== What the compiler says about the file turn 1 wrote", "== something else"
        )
        assert any("without that baseline" in r for r in reasons), reasons

    def test_a_baseline_of_model_prose_rather_than_compiler_output_is_caught(self):
        # The block has to carry rendered `build(...)`/`lint(...)` output. Without that check the
        # sample could print the model's summary of its own validation and call it the baseline.
        prose = _HEALTHY.replace(
            "  build(main.bicep): 3 diagnostic(s)", "  I found three:"
        ).replace("  lint(main.bicep): 3 diagnostic(s)", "  and linting agreed:")
        assert prose != _HEALTHY, "the substitution matched nothing — the fixture moved"
        reasons = check.assess(prose)
        assert any("expected both build and lint" in r for r in reasons), reasons


class TestTheTallyNamesAgreeWithItsNumbers:
    """The counts and the rule ids beside them come from one list, and must still be checked.

    A substring test for each rule id cannot do it: with no name printed there is no rule to
    compare, the counts add up on their own, and a clean compile leaves the sweep nothing to
    reject.
    """

    def test_a_count_with_no_names_is_caught(self):
        reasons = _tampered(
            "  [measured] faults fixed:       2 — no-unused-params; BCP035",
            "  [measured] faults fixed:       2 — none",
        )
        assert any("says 2 but names 0" in r for r in reasons), reasons

    def test_invented_rule_names_are_caught(self):
        reasons = _tampered(
            "  [measured] faults fixed:       2 — no-unused-params; BCP035",
            "  [measured] faults fixed:       2 — BCP999; totally-made-up",
        )
        assert any("which this sample does not track" in r for r in reasons), reasons

    def test_a_rule_in_both_columns_is_caught(self):
        reasons = _tampered(
            "  [measured] faults remaining:   0 — none",
            "  [measured] faults remaining:   1 — BCP035",
        )
        assert any("listed as both fixed and remaining" in r for r in reasons), reasons


class TestTheBaselineIsReadFromItsOwnBlock:
    def test_a_baseline_missing_a_compile_phase_is_caught(self):
        """Both compiles demand both phases, and the baseline is the one that must.

        A passing build beside a failing lint would count as a clean baseline, so the authored
        file's fault count comes out short and turn 2 is measured from the wrong place.
        """
        lint_phase = (
            "  lint(main.bicep): 3 diagnostic(s)\n"
            '    [error] no-unused-params @ main.bicep:3: Parameter "environmentName" is never used.\n'
            '    [warning] BCP035 @ main.bicep:5: The "resource" declaration is missing "sku".\n'
            "    [warning] use-recent-api-versions @ main.bicep:5: '2023-01-01' is 1322 days old.\n"
        )
        reasons = _tampered(lint_phase, "")
        assert any("baseline compile reported" in r for r in reasons), reasons

    def test_a_model_narrating_the_authored_count_does_not_override_it(self):
        """And this one fails a *healthy* run, which is the worse direction.

        A turn-1 reply mentioning the phrase must not supply the number: a narrated 0 would
        make the sample look like it authored a clean file, and fail the run for it.
        """
        narrated = _HEALTHY.replace(
            "  [measured] bicep_validate calls in turn 1: 1",
            "For the record: tracked faults in the authored file: 0 — none\n\n"
            "  [measured] bicep_validate calls in turn 1: 1",
        )
        assert narrated != _HEALTHY, "the substitution matched nothing — the fixture moved"
        assert check.assess(narrated) == [], check.assess(narrated)


class TestTheModelActuallyEdited:
    def test_an_unchanged_file_is_caught(self):
        """The whole reason the sample reads the file store instead of the model's prose.

        The fixture's turn 2 says "all three faults fixed". Leaving that in place while the file
        is untouched is exactly the run this assertion exists to fail — and the compiler cannot
        catch it, since a file nobody edited still compiles.
        """
        reasons = _tampered(
            "main.bicep changed by turn 2:  True", "main.bicep changed by turn 2:  False"
        )
        assert any("described a repair and did not make one" in r for r in reasons), reasons

    def test_a_change_that_fixed_nothing_is_caught(self):
        reasons = _tampered(
            "  [measured] faults fixed:       2 — no-unused-params; BCP035\n  [measured] faults remaining:   0 — none",
            "  [measured] faults fixed:       0 — none\n  [measured] faults remaining:   2 — no-unused-params; BCP035",
        )
        assert any("no fault was fixed" in r for r in reasons), reasons

    def test_a_tally_that_does_not_add_up_is_caught(self):
        reasons = _tampered("faults remaining:   0 — none", "faults remaining:   5 — invented")
        assert any("do not describe the same file" in r for r in reasons), reasons


class TestTheTemplateSurvived:
    """The one repair that satisfies every other signal at once.

    Replace `main.bicep` with an empty but valid file and: it changed, no tracked fault is
    reported, and both compile phases come back clean. Every other assertion here passes, and
    the run reports a successful repair over a file with the storage account deleted.
    """

    def test_a_deleted_template_is_caught(self):
        reasons = _tampered(
            "  [measured] storage account and output intact: True",
            "  [measured] storage account and output intact: False — missing "
            "Microsoft.Storage/storageAccounts; output storageAccountId",
        )
        assert any("deleting the template" in r for r in reasons), reasons

    def test_a_run_that_does_not_report_it_is_caught(self):
        reasons = _tampered("  [measured] storage account and output intact: True\n", "")
        assert any("whether the template survived" in r for r in reasons), reasons


class TestTheTallyIsReadFromTheSampleNotTheModel:
    """The model answers into the same stream, and its prose is above the closing block."""

    def test_a_model_narrating_its_own_tally_does_not_decide_the_result(self):
        """Turn 2 says the opposite of what the sample computed; the sample wins.

        An unscoped `search` finds the model's line first, because turn 2 is printed before the
        work-product block. The run would then fail on the narration of a healthy repair — or,
        with the numbers the other way round, pass on the narration of a broken one.
        """
        narrated = _HEALTHY.replace(
            "  [measured] bicep_validate calls in turn 2: 1",
            "main.bicep changed: False\nfaults fixed: 0 — none\n"
            "faults remaining: 2 — no-unused-params; BCP035\n\n"
            "  [measured] bicep_validate calls in turn 2: 1",
        )
        assert narrated != _HEALTHY, "the substitution matched nothing — the fixture moved"
        assert check.assess(narrated) == [], check.assess(narrated)

    def test_a_model_echoing_the_work_product_heading_does_not_hijack_the_block(self):
        """Scoping to a heading is no protection when the model can print the heading.

        The first match would then be the echo, and the block parsed from it is model prose all
        the way to the end of the output. The sample's block is always the last one, because it
        is printed after every turn has returned.
        """
        echoed = _HEALTHY.replace(
            "  [measured] bicep_validate calls in turn 2: 1",
            "Here is my summary:\n\n== The work product ==\n\n"
            "  main.bicep changed: False\n  [measured] storage account and output intact: False\n"
            "  [measured] faults fixed:       0 — none\n"
            "  [measured] faults remaining:   2 — no-unused-params; BCP035\n\n"
            "  [measured] bicep_validate calls in turn 2: 1",
        )
        assert echoed != _HEALTHY, "the substitution matched nothing — the fixture moved"
        assert check.assess(echoed) == [], check.assess(echoed)

    def test_a_model_echoing_the_compile_heading_does_not_hijack_the_diagnostics(self):
        """Same defect on the other sample-authored block, and it fails in the other direction.

        Diagnostics quoted under an echoed heading would be swept as if the compiler had just
        reported them, so a healthy run goes red on rule ids the model only mentioned.
        """
        echoed = _HEALTHY.replace(
            "  [measured] bicep_validate calls in turn 2: 1",
            "== What the compiler says about the file the model left ==\n\n"
            "  build(main.bicep): 1 diagnostic(s)\n"
            "    [error] BCP062 @ main.bicep:14: quoting an earlier error I already fixed.\n"
            "  lint(main.bicep): no diagnostics\n\n"
            "  [measured] bicep_validate calls in turn 2: 1",
        )
        assert echoed != _HEALTHY, "the substitution matched nothing — the fixture moved"
        assert check.assess(echoed) == [], check.assess(echoed)

    def test_a_missing_work_product_block_is_caught(self):
        reasons = _tampered("== The work product ==", "== something else ==")
        assert any("no work-product block" in r for r in reasons), reasons


class TestTheCompilerHasTheLastWord:
    def test_a_repair_that_breaks_something_else_is_caught(self):
        """The hole a tracked-rules-only comparison leaves open.

        Both original faults are gone and the tally is honest about it. The file now fails on
        something unrelated, which names neither tracked rule — so a per-rule comparison finds
        nothing to object to and reports a clean repair over a broken file.
        """
        reasons = _tampered(
            "  build(main.bicep): no diagnostics",
            "  build(main.bicep): 1 diagnostic(s)\n"
            "    [error] BCP062 @ main.bicep:14: The referenced declaration was not found.",
        )
        assert any("BCP062" in r and "does not account for" in r for r in reasons), reasons

    def test_the_age_rule_is_tolerated_either_way(self):
        """`use-recent-api-versions` fires on the calendar, so neither answer may be demanded.

        A model that leaves the API version alone compiles with that one diagnostic still
        reported. Nothing about the tracked faults changed, so the run must pass — otherwise
        this check would go red on its own, months after anyone touched it.
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

    def test_a_fault_called_fixed_that_the_compiler_still_reports_is_caught(self):
        reasons = _tampered(
            "  build(main.bicep): no diagnostics",
            "  build(main.bicep): 1 diagnostic(s)\n"
            "    [error] no-unused-params @ main.bicep:21: Parameter is declared but never used.",
        )
        assert any("counts no-unused-params as fixed" in r for r in reasons), reasons

    def test_a_fault_called_remaining_that_the_compiler_does_not_see_is_caught(self):
        reasons = _tampered(
            "  [measured] faults fixed:       1 — BCP035\n  [measured] faults remaining:   1 — no-unused-params",
            "  [measured] faults fixed:       1 — no-unused-params\n  [measured] faults remaining:   1 — BCP035",
            base=_PARTIAL,
        )
        assert any("counts BCP035 as remaining" in r for r in reasons), reasons

    def test_a_missing_compile_is_caught(self):
        reasons = _tampered("== What the compiler says about the file the model left ==", "== x ==")
        assert any("compiler was never run" in r for r in reasons), reasons

    def test_only_one_compile_phase_is_caught(self):
        # build and lint are separate passes and a file can pass one while failing the other,
        # so a run that printed only one has not shown the file is clean.
        reasons = _tampered("  lint(main.bicep): no diagnostics\n", "")
        assert any("expected both build and lint" in r for r in reasons), reasons


class TestTurnOneReportedRealDiagnostics:
    """Scoped to turn 1's prose, because the rule ids appear again in the sample's own tally."""

    def test_a_first_turn_that_named_no_rule_is_caught(self):
        reasons = _tampered("1. `no-unused-params` — error — line 3", "1. some problems")
        assert any("turn 1 did not name no-unused-params" in r for r in reasons), reasons

    def test_the_samples_own_tally_does_not_satisfy_the_first_turn(self):
        """The assertion that would rot silently if it searched the whole output.

        `no-unused-params` and `BCP035` are printed further down by the *sample*, in the fault
        tally, whatever the model said. Searching everything would pass on those literals even
        for a turn 1 that reported nothing at all — a check measuring its own harness.
        """
        gutted = _HEALTHY.replace(
            "1. `no-unused-params` — error — line 3 (`environmentName` declared but never used)\n"
            "2. `BCP035` — warning — line 5 (resource missing required `sku` property)\n"
            "3. `use-recent-api-versions` — warning — line 5 "
            "(`2023-01-01` is over the 730-day guideline)",
            "I could not run the validator.",
        )
        assert gutted != _HEALTHY, "the substitution matched nothing — the fixture moved"
        assert "no-unused-params" in gutted, "the tally should still carry the rule id"
        assert "BCP035" in gutted, "the tally should still carry the rule id"

        reasons = check.assess(gutted)
        assert any("turn 1 did not name" in r for r in reasons), reasons

    def test_a_run_with_no_first_turn_at_all_is_caught(self):
        reasons = _tampered("== Turn 1: author main.bicep", "== nothing ==")
        assert any("no turn 1 section" in r for r in reasons), reasons


# --- the sample's own tally, which the checker above can only see the output of ---------------

_SAMPLE = _ROOT / "samples" / "13_bicep_fix_loop" / "agent.py"


def _faults_left():
    """Lift `TRACKED_FAULTS` and `faults_left` out of the sample and run them in isolation.

    Importing the sample would be the obvious way and it does not work here: its module level
    pulls in agent-framework and the sandbox packages, and this workspace does not install
    `agent-framework-openai`, so `test_sample_modules_import.py` skips this sample. A skip is
    the wrong outcome for the one regression below — it is the whole reason the tally reads
    diagnostics instead of source text, and a test that never runs would not have caught it.

    These two nodes depend on nothing but the standard library, so taking them out of the parse
    tree and executing just those runs everywhere. The assertion below is what keeps it honest:
    if either is renamed or moved, this fails rather than quietly testing nothing.
    """
    tree = ast.parse(_SAMPLE.read_text(encoding="utf-8"))
    wanted: dict[str, ast.stmt] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "TRACKED_FAULTS"
            for target in node.targets
        ):
            wanted["TRACKED_FAULTS"] = node
        elif isinstance(node, ast.FunctionDef) and node.name == "faults_left":
            wanted["faults_left"] = node

    missing = {"TRACKED_FAULTS", "faults_left"} - wanted.keys()
    assert not missing, f"samples/13_bicep_fix_loop/agent.py no longer defines {sorted(missing)}"

    namespace: dict = {}
    module = ast.Module(body=[wanted["TRACKED_FAULTS"], wanted["faults_left"]], type_ignores=[])
    exec(compile(module, "<sample-13>", "exec"), namespace)  # noqa: S102 - this repo's own file
    return namespace["faults_left"]


class TestTheSampleAsksTheCompilerNotTheText:
    """`faults_left` reads diagnostics, and the difference is a repair passing or failing.

    A substring test over the source is the tempting version and it is wrong in one direction
    that matters: `no-unused-params` is satisfied by *using* the parameter as well as by
    deleting it, so `"param environmentName" in source` calls a valid repair unfixed. The
    compiler then reports nothing, the tally says it remains, and the run fails CI for a fix
    that worked.
    """

    def test_a_rule_the_compiler_reports_counts_as_remaining(self):
        diagnostics = (
            "build(main.bicep): 1 diagnostic(s)\n"
            "  [error] no-unused-params @ main.bicep:21: Parameter is declared but never used."
        )
        assert _faults_left()(diagnostics) == ["no-unused-params"]

    def test_a_clean_compile_leaves_nothing_remaining(self):
        assert _faults_left()("build(main.bicep): no diagnostics") == []

    def test_source_text_is_not_what_it_reads(self):
        """`faults_left` reads diagnostics, so source text tells it nothing.

        The fixture is a valid repair that keeps the declaration and uses the parameter — the
        shape a source-substring tally calls unfixed while the compiler calls the file clean.
        Passing it here is what separates the two: a substring version returns both faults,
        this one returns none.
        """
        repaired = "param environmentName string\ntags: { env: environmentName }"
        assert "param environmentName" in repaired, "the fixture lost the point of this test"
        assert "sku:" not in repaired, "the fixture must not satisfy the old BCP035 clause either"

        assert _faults_left()(repaired) == []

    def test_the_age_rule_is_not_tracked(self):
        diagnostics = (
            "build(main.bicep): 1 diagnostic(s)\n"
            "  [warning] use-recent-api-versions @ main.bicep:31: '2023-01-01' is 1322 days old"
        )
        assert _faults_left()(diagnostics) == []


class TestNarrationNeverSuppliesAMeasurement:
    """Every number the checker reads comes off a line the sample tagged `[measured]`.

    The model answers into the same stream and its reply is printed *before* the sample's own
    figures for that turn, so an untagged search finds the narration first. Each case below
    puts a plausible sentence in turn 2's reply and requires the run to still pass.
    """

    NARRATED = (
        "bicep_validate calls in turn 2: 0",
        "bicep_validate calls in turn 1: 0",
        "containers after turn 1: 4",
        "containers after the baseline compile: 4",
        "containers after turn 2: 2",
        "containers after the check: 9",
        "main.bicep authored in turn 1: False",
        "main.bicep changed by turn 2:  False",
        "storage account and output intact: False",
        "faults fixed:       0 — none",
        "faults remaining:   2 — no-unused-params; BCP035",
        "tracked faults in the authored file: 0 — none",
        "Disposed 0 sandbox(es) after 2 turns and a check. Containers left: 7.",
    )

    @pytest.mark.parametrize("sentence", NARRATED, ids=lambda text: text.split(":")[0])
    def test_the_model_quoting_a_measurement_is_ignored(self, sentence: str):
        narrated = _HEALTHY.replace(
            "== Turn 2: fix, then validate again ==",
            f"== Turn 2: fix, then validate again ==\n\nFor reference, {sentence}\n",
        )
        assert narrated != _HEALTHY, "the substitution matched nothing — the fixture moved"
        assert check.assess(narrated) == [], check.assess(narrated)

    def test_the_fixture_has_no_untagged_measurement_left(self):
        """Guards the cases above from passing because the phrase stopped being read at all.

        If the sample renamed a line, the narration would match nothing and every case would go
        green while testing an empty substitution. Each phrase must appear tagged in the
        fixture, which is what makes injecting an untagged copy meaningful.
        """
        for sentence in self.NARRATED:
            # The footer is the one line whose value is not the first colon-separated field.
            phrase = "Disposed" if sentence.startswith("Disposed") else sentence.split(":")[0]
            assert f"  [measured] {phrase}" in _HEALTHY, f"{phrase!r} is not a measured line"


class TestTheBaselineIsTheFloorNotAConstant:
    """How many tracked faults the authored file has is a measurement, not `_RULE_IDS`.

    The brief asks for an `environmentName` parameter "which a later change will use". A model
    that uses it immediately — a tag on the resource, which the README says models do — writes a
    file with one tracked fault. Turn 1 reports one, turn 2 repairs it, every count agrees. That
    is a fix loop, and demanding both rule ids anywhere would fail it.
    """

    ONE_FAULT = (
        _HEALTHY.replace(
            "1. `no-unused-params` — error — line 3 (`environmentName` declared but never used)\n"
            "2. `BCP035` — warning — line 5 (resource missing required `sku` property)",
            "1. `BCP035` — warning — line 5 (resource missing required `sku` property)",
        )
        .replace(
            '    [error] no-unused-params @ main.bicep:3: Parameter "environmentName" is never '
            "used.\n",
            "",
        )
        .replace("build(main.bicep): 3 diagnostic(s)", "build(main.bicep): 2 diagnostic(s)")
        .replace("lint(main.bicep): 3 diagnostic(s)", "lint(main.bicep): 2 diagnostic(s)")
        .replace(
            "tracked faults in the authored file: 2 — no-unused-params; BCP035",
            "tracked faults in the authored file: 1 — BCP035",
        )
        .replace(
            "faults fixed:       2 — no-unused-params; BCP035", "faults fixed:       1 — BCP035"
        )
    )

    def test_the_fixture_really_has_one_tracked_fault(self):
        assert self.ONE_FAULT != _HEALTHY
        baseline = self.ONE_FAULT.split("== Turn 2")[0].split("== What the compiler")[1]
        assert "no-unused-params" not in baseline, "the baseline should report only BCP035"

    def test_a_run_whose_authored_file_had_one_fault_passes(self):
        assert check.assess(self.ONE_FAULT) == [], check.assess(self.ONE_FAULT)

    def test_turn_one_is_still_held_to_the_fault_its_own_file_has(self):
        reasons = _tampered(
            "1. `BCP035` — warning — line 5 (resource missing required `sku` property)",
            "1. there is a problem",
            base=self.ONE_FAULT,
        )
        assert any("turn 1 did not name BCP035" in r for r in reasons), reasons

    def test_a_tally_naming_a_fault_the_authored_file_never_had_is_caught(self):
        reasons = _tampered(
            "faults fixed:       1 — BCP035",
            "faults fixed:       2 — BCP035; no-unused-params",
            base=self.ONE_FAULT,
        )
        assert any("do not describe the same file" in r for r in reasons), reasons


class TestTheBaselineTallyIsValidatedToo:
    """`tracked faults in the authored file` drives everything downstream, so it is held to the
    same rules as the two lines it is compared against."""

    def test_names_the_sample_does_not_track_are_caught(self):
        reasons = _tampered(
            "tracked faults in the authored file: 2 — no-unused-params; BCP035",
            "tracked faults in the authored file: 2 — garbage; nonsense",
        )
        assert any("which this sample does not track" in r for r in reasons), reasons

    def test_a_count_that_does_not_match_its_names_is_caught(self):
        reasons = _tampered(
            "tracked faults in the authored file: 2 — no-unused-params; BCP035",
            "tracked faults in the authored file: 2 — BCP035",
        )
        assert any("says 2 but names 1" in r for r in reasons), reasons

    def test_a_duplicated_name_inflating_a_count_is_caught(self):
        # The count is checked against the list and the arithmetic against the set, so a repeat
        # would otherwise let the printed number exceed the rules it describes.
        reasons = _tampered(
            "faults fixed:       2 — no-unused-params; BCP035",
            "faults fixed:       2 — BCP035; BCP035",
        )
        assert any("more than once" in r for r in reasons), reasons


class TestModelTextCannotImpersonateAMeasurement:
    """`quoted` is what makes the tag a barrier rather than a convention.

    Nothing puts `[measured]` in the model's context, so a collision is improbable — but the
    checker trusts that tag completely, and one pass over the reply makes it structural.
    """

    @staticmethod
    def _quoted():
        tree = ast.parse(_SAMPLE.read_text(encoding="utf-8"))
        wanted: dict[str, ast.stmt] = {}
        for node in tree.body:
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id in ("_TAG", "MEASURED"):
                        wanted[target.id] = node
            elif isinstance(node, ast.FunctionDef) and node.name == "quoted":
                wanted["quoted"] = node
        missing = {"_TAG", "MEASURED", "quoted"} - wanted.keys()
        assert not missing, (
            f"samples/13_bicep_fix_loop/agent.py no longer defines {sorted(missing)}"
        )
        namespace: dict = {}
        module = ast.Module(
            body=[wanted["_TAG"], wanted["MEASURED"], wanted["quoted"]], type_ignores=[]
        )
        exec(compile(module, "<sample-13>", "exec"), namespace)  # noqa: S102 - this repo's file
        return namespace["quoted"]

    def test_a_reply_impersonating_a_measurement_is_marked_as_a_quotation(self):
        reply = "I checked.\n  [measured] containers after turn 2: 2\nThat is all."
        out = self._quoted()(reply)
        assert "  [measured] containers after turn 2: 2" not in out
        assert "> [measured] containers after turn 2: 2" in out

    def test_ordinary_model_prose_is_untouched(self):
        reply = "Fixed BCP035.\n\n1. Added a `sku` block.\n  indented note\n"
        assert self._quoted()(reply) == reply.rstrip("\n")

    @pytest.mark.parametrize("spelling", ["[measured]", "[Measured]", "[MEASURED]"])
    def test_every_spelling_the_checker_would_accept_is_defanged(self, spelling: str):
        """A sanitizer narrower than its reader is a hole, not tolerance.

        The checker is deliberately lax about the phrase after the tag, so `quoted` has to be at
        least as broad as that on the tag itself.
        """
        line = f"  {spelling} containers after turn 2: 1"
        assert self._quoted()(line) != line, f"{spelling} slipped through"

    def test_the_checker_accepts_only_the_exact_tag(self):
        # The other half of the same rule: the sample emits one spelling, so widening what the
        # reader accepts only widens what has to be sanitized.
        assert re.search(
            check._M + r"containers", "  [measured] containers after turn 2: 1", check._F
        )
        assert not re.search(
            check._M + r"containers", "  [Measured] containers after turn 2: 1", check._F
        )

    def test_a_diagnostic_carrying_the_tag_is_defanged_too(self):
        """Bicep echoes source text in messages, and `\\n` in an identifier makes a real newline.

        So the compiler's output is a second channel a model can influence, and the sample runs
        it through `quoted` as well as the replies.
        """
        rendered = (
            "  build(main.bicep): 1 diagnostic(s)\n"
            '    [error] BCP037 @ main.bicep:4: The property "injected\n'
            '  [measured] containers after turn 2: 9" is not allowed.'
        )
        out = self._quoted()(rendered)
        assert "\n  [measured] containers after turn 2: 9" not in out
        assert "> [measured] containers after turn 2: 9" in out

    def test_the_checker_ignores_the_quoted_form(self):
        spoofed = _HEALTHY.replace(
            "== Turn 2: fix, then validate again ==",
            "== Turn 2: fix, then validate again ==\n\n> [measured] containers after turn 2: 2\n",
        )
        assert spoofed != _HEALTHY
        assert check.assess(spoofed) == [], check.assess(spoofed)


class TestTheWorkProductInvariantToleratesFormatting:
    """The model writes this file, so its spacing is the model's to choose.

    An exact substring test fails a valid template over a second space, and the live job is
    where that would land — after the model has already done the work.
    """

    @staticmethod
    def _work_missing():
        tree = ast.parse(_SAMPLE.read_text(encoding="utf-8"))
        wanted: dict[str, ast.stmt] = {}
        for node in tree.body:
            if isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id == "WORK_PRODUCT"
                for target in node.targets
            ):
                wanted["WORK_PRODUCT"] = node
            elif isinstance(node, ast.FunctionDef) and node.name == "work_missing":
                wanted["work_missing"] = node
        missing = {"WORK_PRODUCT", "work_missing"} - wanted.keys()
        assert not missing, (
            f"samples/13_bicep_fix_loop/agent.py no longer defines {sorted(missing)}"
        )
        namespace: dict = {"re": re}
        module = ast.Module(body=[wanted["WORK_PRODUCT"], wanted["work_missing"]], type_ignores=[])
        exec(compile(module, "<sample-13>", "exec"), namespace)  # noqa: S102 - this repo's file
        return namespace["work_missing"]

    RESOURCE = "resource sa 'Microsoft.Storage/storageAccounts@2023-01-01' = {}"

    def test_canonical_spacing_passes(self):
        source = f"{self.RESOURCE}\noutput storageAccountId string = sa.id"
        assert self._work_missing()(source) == []

    def test_extra_spacing_passes(self):
        source = f"{self.RESOURCE}\noutput  storageAccountId string = sa.id"
        assert self._work_missing()(source) == []

    def test_a_line_break_after_the_keyword_passes(self):
        source = f"{self.RESOURCE}\noutput\n  storageAccountId string = sa.id"
        assert self._work_missing()(source) == []

    def test_a_genuinely_missing_output_is_still_caught(self):
        assert self._work_missing()(self.RESOURCE) == ["output storageAccountId"]


class TestTheStaleContainerHint:
    """The early exit exists to be actionable, so its command has to run."""

    def test_the_printed_filter_carries_the_label_prefix(self):
        # `docker ps --filter maf-sandbox.thread=…` is rejected as an invalid filter; only
        # `--filter label=<key>=<value>` works, which is the form `containers()` itself uses.
        source = _SAMPLE.read_text(encoding="utf-8")
        assert "docker ps -aq --filter label={_LABEL_THREAD}={THREAD_ID}" in source

    def test_nothing_outside_containers_touches_subprocess(self):
        """The premise the test below rests on, asserted rather than assumed.

        If a later preflight shells out too, the *first* touch of Docker moves and guarding
        `containers()` stops being enough. Keyed on the `subprocess` module rather than on
        `subprocess.run`, because `check_output`, `Popen` and `from subprocess import run` all
        reach an engine just as well.
        """
        tree = ast.parse(_SAMPLE.read_text(encoding="utf-8"))
        containers_fn = next(
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "containers"
        )
        inside = {id(node) for node in ast.walk(containers_fn)}

        bare = {name.split(".", 1)[1] for name in _SPAWNS}
        spawns: list[ast.AST] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and ast.unparse(node.func) in _SPAWNS:
                spawns.append(node)
            elif isinstance(node, ast.ImportFrom) and node.module in {
                "subprocess",
                "os",
                "asyncio",
            }:
                # `from subprocess import run as _r` puts a spawner in reach of everything below.
                if any(alias.name in bare for alias in node.names):
                    spawns.append(node)

        assert spawns, "nothing spawns a process any more — this test has lost its subject"
        outside = [node for node in spawns if id(node) not in inside]
        assert not outside, (
            f"{[ast.unparse(node) for node in outside]} spawns a process outside containers(), "
            "so something may touch Docker before the guarded call"
        )

    def test_the_first_docker_call_is_answered_not_raised(self):
        """`containers()` uses `check=True`, and the guard before it probes no engine.

        Three properties, because two of them are easy to satisfy without the third: the call is
        inside a `try`, that `try` catches both shapes the failure takes, and its handler
        *answers* — a handler edited to log and re-raise would leave the sample tracebacking.
        """
        tree = ast.parse(_SAMPLE.read_text(encoding="utf-8"))
        run = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "run"
        )
        # Only `run` may call it. A helper — `def engine_ready(): return containers() >= 0` —
        # would otherwise move the first touch of Docker outside the guard while every
        # assertion below still passed, which is the cheapest way to lose this property.
        callers = {
            scope.name
            for scope in ast.walk(tree)
            if isinstance(scope, ast.FunctionDef | ast.AsyncFunctionDef)
            for node in ast.walk(scope)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "containers"
        }
        assert callers == {"run"}, f"containers() is also called from {sorted(callers - {'run'})}"

        calls = [
            node
            for node in ast.walk(run)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "containers"
        ]
        assert calls, "run() no longer calls containers()"
        first = min(calls, key=lambda call: (call.lineno, call.col_offset))

        # Innermost, not `ast.walk`'s first: that is breadth-first, so any enclosing `try` —
        # a `finally` wrapped round the whole block, say — would be picked instead.
        guarding = [
            node
            for node in ast.walk(run)
            if isinstance(node, ast.Try)
            and any(inner is first for stmt in node.body for inner in ast.walk(stmt))
        ]
        assert guarding, "the first containers() call is not inside a try"
        innermost = max(guarding, key=lambda node: node.lineno)

        # A missing binary raises FileNotFoundError and an unreadable one PermissionError, both
        # OSError; a refusing daemon exits non-zero, which `check=True` turns into
        # CalledProcessError — not an OSError. Asked as "would this be caught", so a broader
        # handler passes and a narrower one does not.
        failures = (FileNotFoundError, PermissionError, subprocess.CalledProcessError)
        for failure in failures:
            covering = [h for h in innermost.handlers if issubclass(failure, _caught_by(h))]
            assert covering, f"{failure.__name__} escapes this handler"

            # Both properties of the *same* handler: a sibling `except ValueError: return 2`
            # would otherwise supply the answer for a clause that re-raises.
            assert any(_answers(handler) for handler in covering), (
                f"{failure.__name__} is caught and not answered"
            )
