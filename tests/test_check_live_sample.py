"""The live Bicep check reads the compiler, not the model's account of it.

`scripts/check_live_sample.py` is what the live workflow runs on a real `samples/01_acas_bicep`,
`05_docker_bicep` or `09_inprocess_bicep` run to decide whether the published stack actually
validated the file. Its `assess` is a pure function, so the matching itself is tested here — for
free, on every PR — while the billable run that feeds it happens only on dispatch and after a
release.

The suite is organised around the two directions #314 named, because the check used to be wrong
in both:

*Fail-open.* `main.bicep` states both rule ids, both severities and both line numbers in its own
comments, so a model that never called `bicep_validate` could write a summary that satisfied
every assertion made over prose. `TestTheForgeryThatUsedToPass` runs the exact text from the
issue and requires a red.

*Fail-closed.* Three healthy releases went red because a run rendered `**error**` where the
pattern wanted `[error]`. `TestFormattingOfTheReplyIsNotRead` requires those to be green — the
fixture's own reply is written in the markup that failed them.

Both are answered by the same change: the diagnostics are read out of the block the sample
prints from the tool result, and `TestTheBlockIsWhatIsRead` is what holds that line.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_SCRIPT = _ROOT / "scripts" / "check_live_sample.py"
_spec = importlib.util.spec_from_file_location("check_live_sample", _SCRIPT)
assert _spec and _spec.loader
check = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check)

_SCAFFOLD = _ROOT / "samples" / "01_acas_bicep" / "_scaffold.py"
_scaffold_spec = importlib.util.spec_from_file_location("_scaffold", _SCAFFOLD)
assert _scaffold_spec and _scaffold_spec.loader
scaffold = importlib.util.module_from_spec(_scaffold_spec)
_scaffold_spec.loader.exec_module(scaffold)

#: Exactly what `bicep_validate` returns for the sample's `main.bicep`: one line per phase, then
#: one per diagnostic, rendered by `maf_sandbox_bicep._sarif.format_diagnostics`. The day count
#: and the aged API version are the parts that move with no code change.
_RESULT = (
    "build(main.bicep): 1 diagnostic(s)\n"
    '  [warning] BCP035 @ main.bicep:31: The specified "resource" declaration is missing the '
    'required properties: "sku".\n'
    "lint(main.bicep): 2 diagnostic(s)\n"
    '  [error] no-unused-params @ main.bicep:21: Parameter "environmentName" is declared but '
    "never used.\n"
    "  [warning] use-recent-api-versions @ main.bicep:31: Use more recent API version for "
    "'Microsoft.Storage/storageAccounts'. '2023-01-01' is 1287 days old."
)

#: The model's own summary, written in the markup that failed three healthy releases. Keeping it
#: here rather than in a single regression case is the point: every green assertion in this file
#: is made over a reply the old check would have called prose.
_REPLY = (
    "I validated main.bicep with the bicep_validate tool. It returned three diagnostics:\n\n"
    "1. **error** `no-unused-params` — `main.bicep:21`\n"
    '   Parameter "environmentName" is declared but never used.\n'
    "2. **warning** `BCP035` — `main.bicep:31`\n"
    '   The specified "resource" declaration is missing the required properties: "sku".\n'
    "3. **warning** `use-recent-api-versions` — `main.bicep:31`\n"
    "   Use more recent API version for 'Microsoft.Storage/storageAccounts'."
)

#: `_RESULT` as the block carries it — every line indented by two, which is what puts it beyond
#: the checker's `^  [measured] ` anchor. Written out rather than rendered, so the literal shape
#: is pinned by something other than the code that produces it; the first test below is what ties
#: the two together.
_BODY = """\
  build(main.bicep): 1 diagnostic(s)
    [warning] BCP035 @ main.bicep:31: The specified "resource" declaration is missing the required properties: "sku".
  lint(main.bicep): 2 diagnostic(s)
    [error] no-unused-params @ main.bicep:21: Parameter "environmentName" is declared but never used.
    [warning] use-recent-api-versions @ main.bicep:31: Use more recent API version for 'Microsoft.Storage/storageAccounts'. '2023-01-01' is 1287 days old."""

_HEALTHY = f"""\
{_REPLY}

== Diagnostics as bicep_validate returned them ==

{_BODY}

  [measured] compiles that reached the sandbox: 1

  [measured] Disposed 1 sandbox(es).
"""


def _block(output: str) -> str:
    """What the checker itself reads as the tool's output — never the whole run."""
    split = check._split(output)
    assert split is not None, "the fixture has no readable block"
    return split[1]


def _tampered_text(old: str, new: str, base: str = _HEALTHY) -> str:
    """The fixture with one substitution, proven to have matched something."""
    text = base.replace(old, new)
    assert text != base, f"the substitution matched nothing — the fixture moved: {old[:60]!r}"
    return text


def _tampered(old: str, new: str, base: str = _HEALTHY) -> list[str]:
    """`assess` over `_tampered_text`."""
    return check.assess(_tampered_text(old, new, base))


#: The two strings that fence the block. Held here rather than in each side, because nothing
#: else offline ties the sample's heading to the checker's pattern — they live in different
#: directories, and on a tagged run in different *versions*. A drift between them is green all
#: the way through the gate and red only on the live job, after the model has been paid for.
_HEADING_TEXT = "Diagnostics as bicep_validate returned them"
_COUNT_LABEL = "compiles that reached the sandbox"

#: Every sample this checker's shape is a contract with. 02 has no job — its guest needs a
#: Windows runner with WSL — and is held to the shape anyway, so the family stays one thing.
_SAMPLES = ("01_acas_bicep", "02_wslc_bicep", "05_docker_bicep", "09_inprocess_bicep")


class TestTheFixtureIsWhatTheSampleActuallyPrints:
    """A literal fixture that no sample emits would let every case below test a dead shape."""

    def test_the_block_is_byte_identical_to_what_the_scaffold_renders(self):
        rendered = scaffold.evidence(_HEADING_TEXT, [_RESULT], _COUNT_LABEL)
        assert rendered in _HEALTHY, rendered

    def test_the_disposal_line_carries_the_scaffold_tag(self):
        assert f"{scaffold.MEASURED}Disposed 1 sandbox(es)." in _HEALTHY

    def test_this_checker_reads_the_strings_above(self):
        assert check._HEADING.search(f"== {_HEADING_TEXT} ==")
        assert check._COMPILES.search(f"{scaffold.MEASURED}{_COUNT_LABEL}: 1")

    @pytest.mark.parametrize("sample", _SAMPLES)
    def test_every_sample_prints_the_strings_above(self, sample: str):
        source = (_ROOT / "samples" / sample / "agent.py").read_text(encoding="utf-8")
        for literal in (_HEADING_TEXT, _COUNT_LABEL):
            assert f'"{literal}"' in source, (
                f"samples/{sample}/agent.py no longer passes {literal!r} to `evidence` as one "
                "string literal, so the live check will not find the block it prints"
            )


class TestHealthyRun:
    def test_a_real_looking_run_passes(self):
        assert check.assess(_HEALTHY) == []

    def test_the_day_count_and_version_are_not_matched(self):
        """Changing the drifting parts must not change the verdict."""
        drifted = _HEALTHY.replace("1287 days", "3650 days").replace("2023-01-01", "2019-04-01")
        assert drifted != _HEALTHY
        assert check.assess(drifted) == []


class TestTheForgeryThatUsedToPass:
    """The fail-open half of #314, run as the issue wrote it."""

    #: Verbatim from the issue: composed only from what `main.bicep` says about itself, with the
    #: disposal line included as it would genuinely appear. Against the old checker this scored
    #: `[]`.
    FORGERY = """\
I validated main.bicep. The compiler returned these diagnostics:

- [error] no-unused-params @ main.bicep:21 — Parameter "environmentName" is declared but never used.
- [warning] BCP035 @ main.bicep:31 — The "resource" declaration is missing the required property "sku".

Disposed 1 sandbox(es).
"""

    def test_a_reply_composed_from_the_source_comments_is_refused(self):
        reasons = check.assess(self.FORGERY)
        assert any("printed no block of what bicep_validate returned" in r for r in reasons), (
            reasons
        )

    def test_the_forgery_carries_everything_the_old_check_asked_for(self):
        """Why the case above is a real one and not a straw man.

        Every field the old checker read is present: both rule ids, a bracketed severity beside
        each, a location, and a disposal line. Nothing here is malformed — it is simply not from
        the compiler.
        """
        for field in (
            "[error] no-unused-params @ main.bicep:21",
            "[warning] BCP035 @ main.bicep:31",
            "Disposed 1 sandbox(es).",
        ):
            assert field in self.FORGERY

    def test_an_untagged_disposal_line_answers_for_nothing(self):
        """The other half of the same forgery: `Disposed N` is a sentence a model can write."""
        reasons = check.assess(self.FORGERY)
        assert any("did not run to completion" in r for r in reasons), reasons


class TestFormattingOfTheReplyIsNotRead:
    """The fail-closed half: three billable jobs went red over the model's choice of markup."""

    def test_the_run_that_failed_a_healthy_bicep_0_7_0_release(self):
        """Verbatim prose from run 31819345243, over a block from the same compile.

        That run had every reason to pass — `no-unused-params` promoted to error and
        `use-recent-api-versions` reported at all — and failed because the summary wrote
        `**error**` where the pattern wanted `[error]`.
        """
        assert check.assess(_HEALTHY) == []

    @pytest.mark.parametrize(
        "reply",
        [
            "Validated. See the tool output for no-unused-params and BCP035.",
            "| rule | level |\n| no-unused-params | error |\n| BCP035 | warning |",
            "Diagnostics: `no-unused-params`, `BCP035`, `use-recent-api-versions`.",
            "no-unused-params was reported, and so was BCP035. Nothing else to add.",
        ],
        ids=["plain", "table", "backticks", "sentence"],
    )
    def test_any_rendering_that_names_the_rules_passes(self, reply: str):
        assert check.assess(_HEALTHY.replace(_REPLY, reply)) == []

    def test_the_severity_is_read_from_the_block_not_the_reply(self):
        """A reply with no severity in it at all is still a healthy run."""
        stripped = _HEALTHY.replace(_REPLY, "no-unused-params and BCP035 came back.")
        assert "**error**" not in stripped
        assert check.assess(stripped) == []


class TestTheBlockIsWhatIsRead:
    """The fence: a model can write the heading and cannot close it."""

    def test_a_model_forging_the_whole_block_cannot_close_it(self):
        """`quoted` in the scaffold is what makes this true, so the case runs through it.

        The forged reply carries a heading, a plausible compile and a closing line. The sample
        prints the reply through `quoted`, which turns the model's tagged line into a quotation,
        and the checker then finds no block at all rather than the model's.
        """
        forged = scaffold.quoted(
            "Here is what I got.\n\n"
            "== Diagnostics as bicep_validate returned them ==\n\n"
            "  build(main.bicep): 1 diagnostic(s)\n"
            "    [error] no-unused-params @ main.bicep:21: declared but never used.\n"
            "  lint(main.bicep): 1 diagnostic(s)\n"
            "    [warning] BCP035 @ main.bicep:31: missing sku.\n\n"
            "  [measured] compiles that reached the sandbox: 1\n"
        )
        assert "> [measured] compiles that reached the sandbox: 1" in forged
        reasons = check.assess(f"{forged}\n\n  [measured] Disposed 1 sandbox(es).\n")
        assert any("printed no block" in r for r in reasons), reasons

    def test_a_reply_quoting_the_heading_does_not_steal_the_block(self):
        """The last heading before the closing line is the sample's, so a healthy run stays green."""
        echoed = _HEALTHY.replace(
            _REPLY,
            "Running it printed:\n\n"
            "== Diagnostics as bicep_validate returned them ==\n\n"
            "  nothing at all\n\n"
            "…and then no-unused-params and BCP035 came back.",
        )
        assert echoed != _HEALTHY
        assert check.assess(echoed) == []

    def test_two_closing_lines_are_trusted_as_none(self):
        """Only the sample writes the tag, so a second closing line means something else did."""
        doubled = _HEALTHY.replace(
            "  [measured] compiles that reached the sandbox: 1\n",
            "  [measured] compiles that reached the sandbox: 1\n"
            "  [measured] compiles that reached the sandbox: 9\n",
        )
        assert doubled != _HEALTHY
        assert any("printed no block" in r for r in check.assess(doubled)), check.assess(doubled)

    def test_diagnostics_left_only_in_the_reply_do_not_count(self):
        """The block reports a clean compile and the model's account of it is left untouched."""
        gutted = _tampered_text(
            _BODY, "  build(main.bicep): no diagnostics\n  lint(main.bicep): no diagnostics"
        )
        assert "no-unused-params" in gutted, "the reply must still name both rules"
        reasons = check.assess(gutted)
        assert any("did not report 'no-unused-params'" in r for r in reasons), reasons
        assert any("did not report 'BCP035'" in r for r in reasons), reasons


class TestABrokenStackFails:
    def test_a_call_that_never_reached_the_sandbox_is_caught(self):
        reasons = _tampered(
            "compiles that reached the sandbox: 1", "compiles that reached the sandbox: 0"
        )
        assert any("no bicep_validate call reached the sandbox" in r for r in reasons), reasons

    def test_a_missing_phase_is_caught(self):
        # Both required rules are one from each phase, so half a compile is half the evidence.
        reasons = _tampered("lint(main.bicep): 2 diagnostic(s)", "the lint phase was skipped")
        assert any("expected both build and lint" in r for r in reasons), reasons

    def test_a_dropped_rule_is_named(self):
        reasons = _tampered("[warning] BCP035 @", "[warning] some-other-rule @")
        assert any("did not report 'BCP035'" in r for r in reasons), reasons
        assert not any("no-unused-params" in r for r in reasons), (
            "no-unused-params was reported and must not be named"
        )

    def test_a_compile_with_no_error_is_caught(self):
        reasons = _tampered("    [error] no-unused-params", "    [warning] no-unused-params")
        assert any("no diagnostic came back at [error]" in r for r in reasons), reasons

    def test_no_sandbox_created_fails(self):
        reasons = _tampered("Disposed 1 sandbox(es).", "Disposed 0 sandbox(es).")
        assert any("no sandbox was ever created" in r for r in reasons), reasons

    def test_an_incomplete_run_has_no_disposal_line(self):
        reasons = _tampered("\n  [measured] Disposed 1 sandbox(es).\n", "\n")
        assert any("did not run to completion" in r for r in reasons), reasons

    def test_an_untagged_disposal_line_does_not_answer_for_the_router(self):
        reasons = _tampered("  [measured] Disposed 1", "Disposed 1")
        assert any("did not run to completion" in r for r in reasons), reasons

    def test_empty_output_fails_rather_than_passing_vacuously(self):
        assert check.assess("") != []


class TestTheDiagnosticsHaveToReachTheModel:
    """The block proves the compiler ran; the reply proves its findings got out of the log."""

    def test_a_reply_that_names_neither_rule_is_caught(self):
        reasons = _tampered(_REPLY, "I had a look and it seems fine.")
        assert any("never names BCP035, no-unused-params" in r for r in reasons), reasons

    def test_only_the_rules_the_block_reports_are_demanded(self):
        """A compile that produced one of them must not fail for the other's absence.

        The block is not a constant: this is measured against what the compiler actually said,
        the way `check_live_fix_loop_sample.py` measures its fix turn.
        """
        one_rule = _HEALTHY.replace(
            "  lint(main.bicep): 2 diagnostic(s)\n"
            '    [error] no-unused-params @ main.bicep:21: Parameter "environmentName" is declared but never used.\n',
            "  lint(main.bicep): 1 diagnostic(s)\n",
        ).replace(_REPLY, "BCP035 came back at error.")
        assert one_rule != _HEALTHY
        reasons = check.assess(one_rule)
        assert not any("never names" in r for r in reasons), reasons

    def test_the_rule_ids_are_matched_case_insensitively(self):
        # Opaque tokens, echoed by a model that may lower-case them in prose.
        assert check.assess(_HEALTHY.replace(_REPLY, "bcp035 and no-unused-params.")) == []


class TestTheRuleSetTheRepositoryAskedFor:
    """The config check (#308) — the one failure shape that looks entirely healthy."""

    #: `_HEALTHY` with the config never found: `no-unused-params` back at its built-in `warning`,
    #: and `use-recent-api-versions` gone. Everything else about the run is intact.
    STALE_IMAGE = _HEALTHY.replace(
        "  lint(main.bicep): 2 diagnostic(s)\n"
        '    [error] no-unused-params @ main.bicep:21: Parameter "environmentName" is declared but never used.\n'
        "    [warning] use-recent-api-versions @ main.bicep:31: Use more recent API version for 'Microsoft.Storage/storageAccounts'. '2023-01-01' is 1287 days old.\n",
        "  lint(main.bicep): 1 diagnostic(s)\n"
        '    [warning] no-unused-params @ main.bicep:21: Parameter "environmentName" is declared but never used.\n',
    ).replace(
        '  [warning] BCP035 @ main.bicep:31: The specified "resource" declaration is missing the required properties: "sku".\n',
        '  [error] BCP035 @ main.bicep:31: The specified "resource" declaration is missing the required properties: "sku".\n',
    )

    def test_the_fixture_is_a_run_that_found_no_config(self):
        assert self.STALE_IMAGE != _HEALTHY
        assert "use-recent-api-versions" not in _block(self.STALE_IMAGE)
        assert "[error] no-unused-params" not in self.STALE_IMAGE

    def test_a_stale_image_run_is_caught(self):
        reasons = check.assess(self.STALE_IMAGE)
        assert any("bicepconfig.json was not discovered" in r for r in reasons), reasons

    def test_a_stale_image_run_passes_every_other_assertion(self):
        """Why this is a check of its own rather than a tightening of one of the others.

        The `[error]` was moved onto BCP035 so the severity check is satisfied too — the only
        thing wrong with this run is the rule set it linted against.
        """
        assert len(check.assess(self.STALE_IMAGE)) == 1, check.assess(self.STALE_IMAGE)

    def test_the_switched_on_rule_alone_is_enough(self):
        # The promotion is not visible — `no-unused-params` is a warning — but a rule the config
        # switches on was reported, which it could not be without one.
        demoted = _HEALTHY.replace(
            "[error] no-unused-params", "[warning] no-unused-params"
        ).replace(
            '  [warning] BCP035 @ main.bicep:31: The specified "resource" declaration is missing the required properties: "sku".\n',
            '  [error] BCP035 @ main.bicep:31: The specified "resource" declaration is missing the required properties: "sku".\n',
        )
        assert demoted != _HEALTHY
        assert check.config_was_discovered(demoted)
        assert check.assess(demoted) == []

    def test_the_promoted_severity_alone_is_enough(self):
        # The mirror case: the drift-prone rule is missing and the promotion is still visible. A
        # healthy run must not go red for dropping the one rule this check has never been allowed
        # to depend on.
        dropped = _HEALTHY.replace(
            "    [warning] use-recent-api-versions @ main.bicep:31: Use more recent API version for 'Microsoft.Storage/storageAccounts'. '2023-01-01' is 1287 days old.\n",
            "",
        ).replace("lint(main.bicep): 2 diagnostic(s)", "lint(main.bicep): 1 diagnostic(s)")
        assert dropped != _HEALTHY
        assert check.config_was_discovered(dropped)
        assert check.assess(dropped) == []

    def test_naming_a_rule_in_the_reply_is_not_reporting_it(self):
        """The prose half of #308, which no longer has any way in.

        Each of these is a true sentence about a run that found no config, and the last three
        quote a whole severity-and-rule pair. None of them is in the block, so none counts.
        """
        for prose in (
            "use-recent-api-versions is missing from the output.",
            "There is no use-recent-api-versions finding, and no-unused-params is only a warning.",
            "Expected [warning] use-recent-api-versions was not reported.",
            "I did not see [error] no-unused-params in the output.",
        ):
            spoiled = self.STALE_IMAGE.replace(_REPLY, f"{prose} BCP035 and no-unused-params.")
            assert spoiled != self.STALE_IMAGE
            assert not check.config_was_discovered(spoiled), prose

    def test_a_run_with_no_block_reports_no_config(self):
        assert not check.config_was_discovered(TestTheForgeryThatUsedToPass.FORGERY)

    def test_a_healthy_run_shows_both_tells(self):
        # The premise the either-or rests on: on a good run neither signal is doing the work
        # alone, so losing one to a compiler change costs nothing.
        reported = check.diagnostics(_block(_HEALTHY))
        assert "use-recent-api-versions" in reported
        assert "error" in reported["no-unused-params"]
