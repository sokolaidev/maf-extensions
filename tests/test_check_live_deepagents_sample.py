"""The live Deep Agents check reads the compiler's SARIF, not the model's account of it.

`scripts/check_live_deepagents_sample.py` is what the live workflow runs on a real
`samples/17_deepagents_docker_bicep` run to decide whether the published stack actually
compiled the file. Its `assess` is a pure function, so the matching is tested here — for free,
on every PR — while the run that feeds it happens only on dispatch and after a release.

Two things separate it from `tests/test_check_live_sample.py`, and both have a suite below:

*The evidence is SARIF.* `bicep_validate` renders one line per diagnostic; here the raw
document comes back through `execute`, so a rule and its level are lines apart, with the
block's indent and the adapter's `[stderr]` prefix in between. `TestTheLevelIsReadFromSarif`
holds the reading of that.

*The model chose the command.* One result is a whole run, because nothing outside the model
decides how many commands it takes. `TestOneCommandIsEnough` pins that as intended rather than
accidental.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_SCRIPT = _ROOT / "scripts" / "check_live_deepagents_sample.py"
_spec = importlib.util.spec_from_file_location("check_live_deepagents_sample", _SCRIPT)
assert _spec and _spec.loader
check = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check)

_SAMPLE = _ROOT / "samples" / "17_deepagents_docker_bicep"
_scaffold_spec = importlib.util.spec_from_file_location("_scaffold", _SAMPLE / "_scaffold.py")
assert _scaffold_spec and _scaffold_spec.loader
scaffold = importlib.util.module_from_spec(_scaffold_spec)
_scaffold_spec.loader.exec_module(scaffold)

#: Exactly what `bicep build main.bicep --no-restore --diagnostics-format sarif` printed for
#: this sample's `main.bicep` in the sample's own image, and what `bicep lint` printed beside
#: it, byte for byte the same document. The day count and the acceptable-version list are the
#: parts that move with no code change.
_SARIF = """\
{
  "$schema": "https://schemastore.azurewebsites.net/schemas/json/sarif-2.1.0.json",
  "version": "2.1.0",
  "runs": [
    {
      "tool": {
        "driver": {
          "name": "bicep"
        }
      },
      "results": [
        {
          "ruleId": "no-unused-params",
          "level": "error",
          "message": {
            "text": "Parameter \\"environmentName\\" is declared but never used. [https://aka.ms/bicep/linter-diagnostics#no-unused-params]"
          },
          "locations": [
            {
              "physicalLocation": {
                "artifactLocation": {
                  "uri": "file:///maf-sandbox/work/main.bicep"
                },
                "region": {
                  "startLine": 21,
                  "charOffset": 7
                }
              }
            }
          ]
        },
        {
          "ruleId": "BCP035",
          "message": {
            "text": "The specified \\"resource\\" declaration is missing the following required properties: \\"sku\\". If this is a resource type definition inaccuracy, report it using https://aka.ms/bicep-type-issues. [https://aka.ms/bicep/core-diagnostics#BCP035]"
          },
          "locations": [
            {
              "physicalLocation": {
                "artifactLocation": {
                  "uri": "file:///maf-sandbox/work/main.bicep"
                },
                "region": {
                  "startLine": 31,
                  "charOffset": 10
                }
              }
            }
          ]
        },
        {
          "ruleId": "use-recent-api-versions",
          "message": {
            "text": "Use more recent API version for 'Microsoft.Storage/storageAccounts'. '2023-01-01' is 1349 days old, should be no more than 730 days old, or the most recent. Acceptable versions: 2026-04-01, 2025-08-01, 2025-06-01, 2025-01-01 [https://aka.ms/bicep/linter-diagnostics#use-recent-api-versions]"
          },
          "locations": [
            {
              "physicalLocation": {
                "artifactLocation": {
                  "uri": "file:///maf-sandbox/work/main.bicep"
                },
                "region": {
                  "startLine": 31,
                  "charOffset": 60
                }
              }
            }
          ]
        }
      ],
      "columnKind": "utf16CodeUnits"
    }
  ]
}"""

#: Deep Agents appends this to every result whose command reported an exit code. Both commands
#: exit 1 here: the file has an error in it.
_STATUS = "\n[Command failed with exit code 1]"

#: The two results as `execute` hands them back. `bicep build` writes its SARIF to stderr, which
#: `maf-sandbox-deepagents` prefixes line by line; `bicep lint` writes the same document to
#: stdout, which it does not. The checker has to read both.
_BUILD_RESULT = "\n".join(f"[stderr] {line}" for line in _SARIF.splitlines()) + _STATUS
_LINT_RESULT = _SARIF + _STATUS

#: The model's own summary, in the markup a model reaches for: bold levels, backticked ids, a
#: numbered list. It is the default reply for every case below rather than a case of its own,
#: which is what holds the checker to reading nothing in a reply but the ids.
_REPLY = (
    "I ran both commands in the sandbox. The compiler returned three diagnostics:\n\n"
    "1. **error** `no-unused-params` — `main.bicep:21`\n"
    '   Parameter "environmentName" is declared but never used.\n'
    "2. **warning** `BCP035` — `main.bicep:31`\n"
    '   The specified "resource" declaration is missing the required properties: "sku".\n'
    "3. **warning** `use-recent-api-versions` — `main.bicep:31`\n"
    "   Use more recent API version for 'Microsoft.Storage/storageAccounts'."
)

#: The two strings that fence the block. Held here rather than in each side, because nothing
#: else offline ties the sample's heading to the checker's pattern — they live in different
#: directories, and on a tagged run in different *versions*. A drift between them is green all
#: the way through the gate and red only on the live job, after the model has been paid for.
_HEADING_TEXT = "Diagnostics as execute returned them"
_COUNT_LABEL = "compiles that reached the sandbox"


def _run(*results: str, reply: str = _REPLY, disposed: int = 1) -> str:
    """A whole sample run, printed the way `agent.py` prints one."""
    block = scaffold.evidence(_HEADING_TEXT, list(results), _COUNT_LABEL)
    return f"{scaffold.quoted(reply)}\n\n{block}\n\n{scaffold.MEASURED}Disposed {disposed} sandbox(es).\n"


_HEALTHY = _run(_BUILD_RESULT, _LINT_RESULT)


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


class TestTheFixtureIsWhatTheSampleActuallyPrints:
    """A literal fixture that no sample emits would let every case below test a dead shape."""

    def test_the_document_is_sarif(self):
        results = json.loads(_SARIF)["runs"][0]["results"]
        assert [result["ruleId"] for result in results] == [
            "no-unused-params",
            "BCP035",
            "use-recent-api-versions",
        ]

    def test_only_the_promoted_rule_carries_a_level(self):
        """SARIF omits `level` for a warning, which is what makes the default load-bearing."""
        levels = {
            result["ruleId"]: result.get("level")
            for result in json.loads(_SARIF)["runs"][0]["results"]
        }
        assert levels == {
            "no-unused-params": "error",
            "BCP035": None,
            "use-recent-api-versions": None,
        }

    def test_the_stderr_result_carries_the_prefix_inside_the_block(self):
        """The arrangement the checker has to read across: indent, prefix, then the key."""
        assert '  [stderr]           "ruleId": "no-unused-params",' in _HEALTHY
        assert '  [stderr]           "level": "error",' in _HEALTHY

    def test_the_disposal_line_carries_the_scaffold_tag(self):
        assert f"{scaffold.MEASURED}Disposed 1 sandbox(es)." in _HEALTHY

    def test_this_checker_reads_the_strings_above(self):
        assert check._HEADING.search(f"== {_HEADING_TEXT} ==")
        assert check._COMPILES.search(f"{scaffold.MEASURED}{_COUNT_LABEL}: 1")

    def test_the_sample_prints_the_strings_above(self):
        source = (_SAMPLE / "agent.py").read_text(encoding="utf-8")
        for literal in (_HEADING_TEXT, _COUNT_LABEL):
            assert f'"{literal}"' in source, (
                f"{_SAMPLE.name}/agent.py no longer passes {literal!r} to `evidence` as one "
                "string literal, so the live check will not find the block it prints"
            )


class TestHealthyRun:
    def test_a_real_looking_run_passes(self):
        assert check.assess(_HEALTHY) == []

    def test_the_day_count_and_versions_are_not_matched(self):
        """Changing the drifting parts must not change the verdict."""
        drifted = _HEALTHY.replace("1349 days", "3650 days").replace("2026-04-01", "2027-04-01")
        assert drifted != _HEALTHY
        assert check.assess(drifted) == []


class TestOneCommandIsEnough:
    """The deliberate weakening: the model wrote the command, so it decides how many there are.

    `check_live_sample.py` requires both compiler phases because `bicep_validate` runs both. A
    run here that compiled once and reported all three rules is a healthy run, and a checker
    that demanded two would go red on a model that did the work in one command.
    """

    def test_a_single_result_is_a_healthy_run(self):
        assert check.assess(_run(_BUILD_RESULT)) == []

    def test_the_count_is_what_the_sample_measured(self):
        assert "  [measured] compiles that reached the sandbox: 1" in _run(_BUILD_RESULT)


class TestTheLevelIsReadFromSarif:
    """A rule and its level are separate lines, with the block's indent and a prefix between."""

    def test_the_promoted_level_survives_the_prefix(self):
        assert check.diagnostics(_block(_HEALTHY))["no-unused-params"] == {"error"}

    def test_a_diagnostic_with_no_level_reads_as_a_warning(self):
        assert check.diagnostics(_block(_HEALTHY))["BCP035"] == {"warning"}

    def test_a_level_belonging_to_a_later_diagnostic_is_not_borrowed(self):
        """The gap is `[^"]*`, so the optional match stops at the next key rather than hunting.

        `BCP035` is followed by `message`; without that bound the reader would walk on to the
        next `"level"` in the document and report the promotion against the wrong rule.
        """
        promoted = [
            rule
            for rule, levels in check.diagnostics(_block(_HEALTHY)).items()
            if "error" in levels
        ]
        assert promoted == ["no-unused-params"]

    def test_a_rule_two_results_disagree_about_carries_both_levels(self):
        """Two commands, one document each, and the block is read as one."""
        mixed = _run(_BUILD_RESULT, _LINT_RESULT.replace('\n          "level": "error",', ""))
        assert check.diagnostics(_block(mixed))["no-unused-params"] == {"error", "warning"}


class TestTheForgeryThatUsedToPass:
    """The fail-open half of #314, in this sample's terms."""

    #: Composed only from what `main.bicep` says about itself, with the disposal line included
    #: as it would genuinely appear.
    FORGERY = """\
I validated main.bicep. The compiler returned these diagnostics:

- "ruleId": "no-unused-params", "level": "error" @ main.bicep:21
- "ruleId": "BCP035" @ main.bicep:31

Disposed 1 sandbox(es).
"""

    def test_a_reply_composed_from_the_source_comments_is_refused(self):
        reasons = check.assess(self.FORGERY)
        assert any("printed no block of what execute returned" in r for r in reasons), reasons

    def test_the_forgery_carries_everything_a_looser_check_would_ask_for(self):
        """Why the case above is a real one and not a straw man."""
        for field in (
            '"ruleId": "no-unused-params"',
            '"level": "error"',
            "Disposed 1 sandbox(es).",
        ):
            assert field in self.FORGERY

    def test_an_untagged_disposal_line_answers_for_nothing(self):
        reasons = check.assess(self.FORGERY)
        assert any("did not run to completion" in r for r in reasons), reasons


class TestFormattingOfTheReplyIsNotRead:
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
        assert check.assess(_run(_BUILD_RESULT, _LINT_RESULT, reply=reply)) == []

    def test_the_level_is_read_from_the_block_not_the_reply(self):
        stripped = _run(_BUILD_RESULT, reply="no-unused-params and BCP035 came back.")
        assert "**error**" not in stripped
        assert check.assess(stripped) == []

    def test_the_rule_ids_are_matched_case_insensitively(self):
        # Opaque tokens, echoed by a model that may lower-case them in prose.
        assert check.assess(_run(_BUILD_RESULT, reply="bcp035 and no-unused-params.")) == []


class TestTheBlockIsWhatIsRead:
    """The fence: a model can write the heading and cannot close it."""

    def test_a_model_forging_the_whole_block_cannot_close_it(self):
        """`quoted` in the scaffold is what makes this true, so the case runs through it."""
        forged = scaffold.quoted(
            "Here is what I got.\n\n"
            "== Diagnostics as execute returned them ==\n\n"
            '  "ruleId": "no-unused-params",\n'
            '  "level": "error",\n'
            '  "ruleId": "BCP035",\n\n'
            "  [measured] compiles that reached the sandbox: 1\n"
        )
        assert "> [measured] compiles that reached the sandbox: 1" in forged
        reasons = check.assess(f"{forged}\n\n  [measured] Disposed 1 sandbox(es).\n")
        assert any("printed no block" in r for r in reasons), reasons

    def test_a_reply_quoting_the_heading_does_not_steal_the_block(self):
        """The last heading before the closing line is the sample's."""
        echoed = _run(
            _BUILD_RESULT,
            reply=(
                "Running it printed:\n\n"
                "== Diagnostics as execute returned them ==\n\n"
                "  nothing at all\n\n"
                "…and then no-unused-params and BCP035 came back."
            ),
        )
        assert check.assess(echoed) == []

    def test_two_closing_lines_are_trusted_as_none(self):
        """Only the sample writes the tag, so a second closing line means something else did."""
        doubled = _tampered_text(
            "  [measured] compiles that reached the sandbox: 2\n",
            "  [measured] compiles that reached the sandbox: 2\n"
            "  [measured] compiles that reached the sandbox: 9\n",
        )
        assert any("printed no block" in r for r in check.assess(doubled)), check.assess(doubled)

    def test_diagnostics_left_only_in_the_reply_do_not_count(self):
        """The block reports a compile with nothing in it, and the reply is left untouched."""
        gutted = _run("<no output>\n[Command succeeded with exit code 0]")
        assert "no-unused-params" in gutted, "the reply must still name both rules"
        reasons = check.assess(gutted)
        assert any("did not report 'no-unused-params'" in r for r in reasons), reasons
        assert any("did not report 'BCP035'" in r for r in reasons), reasons


class TestABrokenStackFails:
    def test_a_run_whose_results_never_reached_the_compiler_is_caught(self):
        reasons = check.assess(_run())
        assert any("no execute result carried a SARIF diagnostic" in r for r in reasons), reasons

    def test_a_dropped_rule_is_named(self):
        reasons = _tampered('"ruleId": "BCP035"', '"ruleId": "some-other-rule"')
        assert any("did not report 'BCP035'" in r for r in reasons), reasons
        assert not any("no-unused-params" in r for r in reasons), (
            "no-unused-params was reported and must not be named"
        )

    def test_per_call_disposal_leaves_an_empty_scope_purge(self):
        assert check.assess(_run(_BUILD_RESULT, disposed=0)) == []

    def test_an_incomplete_run_has_no_disposal_line(self):
        reasons = _tampered("\n  [measured] Disposed 1 sandbox(es).\n", "\n")
        assert any("did not run to completion" in r for r in reasons), reasons

    def test_an_untagged_disposal_line_does_not_answer_for_the_router(self):
        reasons = _tampered("  [measured] Disposed 1", "Disposed 1")
        assert any("did not run to completion" in r for r in reasons), reasons

    def test_a_purge_that_lost_a_sandbox_is_caught(self):
        reasons = _tampered(
            "  [measured] Disposed 1 sandbox(es).",
            "  [measured] Disposed 0 sandbox(es).\n  [measured] Not fully disposed: 1",
        )
        assert any("could not account for every sandbox" in r for r in reasons), reasons

    def test_empty_output_fails_rather_than_passing_vacuously(self):
        assert check.assess("") != []


class TestTheDiagnosticsHaveToReachTheModel:
    """The block proves the compiler ran; the reply proves its findings got out of the log."""

    def test_a_reply_that_names_neither_rule_is_caught(self):
        reasons = check.assess(_run(_BUILD_RESULT, reply="I had a look and it seems fine."))
        assert any("never names BCP035, no-unused-params" in r for r in reasons), reasons

    def test_only_the_rules_the_block_reports_are_demanded(self):
        """A compile that produced one of them must not fail for the other's absence."""
        one_rule = _run(
            _BUILD_RESULT.replace('"ruleId": "no-unused-params"', '"ruleId": "BCP035"'),
            reply="BCP035 came back at error.",
        )
        reasons = check.assess(one_rule)
        assert not any("never names" in r for r in reasons), reasons

    def test_a_rule_beyond_the_required_two_is_not_demanded_of_the_reply(self):
        """The other half of the same narrowing, and the one that decides a release.

        `use-recent-api-versions` is read from the block, where it is the tell that the config
        was found. Requiring the model to echo it too would tie a live check to the compiler's
        rule set: a CLI that grows a linter rule reds a release over a diagnostic this sample
        never asked about.
        """
        assert "use-recent-api-versions" in check.diagnostics(_block(_HEALTHY))
        passing = _run(_BUILD_RESULT, reply="no-unused-params came back at error, and BCP035.")
        assert check.assess(passing) == []


class TestTheRuleSetTheRepositoryAskedFor:
    """The config check (#308) — the one failure shape that looks entirely healthy."""

    #: The same run with `bicepconfig.json` never found: `no-unused-params` back at its built-in
    #: warning, and `use-recent-api-versions` not reported at all. The `error` is moved onto
    #: BCP035 so every other assertion still passes.
    STALE_IMAGE = (
        _SARIF.replace(
            '"ruleId": "no-unused-params",\n          "level": "error",',
            '"ruleId": "no-unused-params",',
        )
        .replace('"ruleId": "BCP035",', '"ruleId": "BCP035",\n          "level": "error",')
        .replace('"ruleId": "use-recent-api-versions"', '"ruleId": "some-other-rule"')
    )

    def test_the_fixture_is_a_run_that_found_no_config(self):
        assert self.STALE_IMAGE != _SARIF
        reported = check.diagnostics(self.STALE_IMAGE)
        assert "use-recent-api-versions" not in reported
        assert reported["no-unused-params"] == {"warning"}

    def test_a_stale_image_run_is_caught(self):
        reasons = check.assess(_run(self.STALE_IMAGE))
        assert any("bicepconfig.json was not discovered" in r for r in reasons), reasons

    def test_a_stale_image_run_passes_every_other_assertion(self):
        """Why this is a check of its own rather than a tightening of one of the others."""
        assert len(check.assess(_run(self.STALE_IMAGE))) == 1, check.assess(_run(self.STALE_IMAGE))

    def test_the_switched_on_rule_alone_is_enough(self):
        # The promotion is not visible — `no-unused-params` is a warning — but a rule the config
        # switches on was reported, which it could not be without one.
        demoted = _SARIF.replace(
            '"ruleId": "no-unused-params",\n          "level": "error",',
            '"ruleId": "no-unused-params",',
        ).replace('"ruleId": "BCP035",', '"ruleId": "BCP035",\n          "level": "error",')
        assert check.config_was_discovered(_run(demoted))
        assert check.assess(_run(demoted)) == []
