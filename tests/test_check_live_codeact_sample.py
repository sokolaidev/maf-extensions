"""The live CodeAct check reads the interpreter, not the model's account of it.

`scripts/check_live_codeact_sample.py` is what the live workflow runs on a real
`samples/03_acas_codeact` or `06_docker_codeact` run. It had no suite at all until #314 — the
one shipped checker that did not — while `docs/maintainers.md` said every one of them was unit
tested on each PR. It is a pure function, so this costs nothing on a PR and the billable run
that feeds it happens only on dispatch and after a release.

The value it looks for is a *constant*: `354224848179261915075` is the 100th Fibonacci number,
which any model can recite from training data. That is the whole reason the number is read out
of `execute_code`'s own output rather than out of the reply — the same digits mean nothing in
one place and everything in the other.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_SCRIPT = _ROOT / "scripts" / "check_live_codeact_sample.py"
_spec = importlib.util.spec_from_file_location("check_live_codeact_sample", _SCRIPT)
assert _spec and _spec.loader
check = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check)

_SCAFFOLD = _ROOT / "samples" / "03_acas_codeact" / "_scaffold.py"
_scaffold_spec = importlib.util.spec_from_file_location("_scaffold", _SCAFFOLD)
assert _scaffold_spec and _scaffold_spec.loader
scaffold = importlib.util.module_from_spec(_scaffold_spec)
_scaffold_spec.loader.exec_module(scaffold)

_ANSWER = "354224848179261915075"

#: Exactly what `execute_code` returns for a program that printed one line, rendered by
#: `maf_sandbox_codeact._tool._format_result`.
_RESULT = f"stdout:\n{_ANSWER}"

_REPLY = (
    "I wrote a short Python program, ran it with execute_code, and it printed:\n\n"
    f"{_ANSWER}\n\n"
    "That is the 100th Fibonacci number with F(0) = 0 and F(1) = 1."
)

#: `_RESULT` as the block carries it — every line indented by two, which is what puts it beyond
#: the checker's `^  [measured] ` anchor.
_BODY = f"  stdout:\n  {_ANSWER}"

_HEALTHY = f"""\
{_REPLY}

== Program output as execute_code returned it ==

{_BODY}

  [measured] programs whose output came back from the sandbox: 1

  [measured] Disposed 1 sandbox(es).
"""


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
_HEADING_TEXT = "Program output as execute_code returned it"
_COUNT_LABEL = "programs whose output came back from the sandbox"

#: Every sample this checker's shape is a contract with. 04 has no job — its guest needs a
#: Windows runner with WSL — and is held to the shape anyway, so the family stays one thing.
_SAMPLES = ("03_acas_codeact", "04_wslc_codeact", "06_docker_codeact")


class TestTheFixtureIsWhatTheSampleActuallyPrints:
    """A literal fixture that no sample emits would let every case below test a dead shape."""

    def test_the_block_is_byte_identical_to_what_the_scaffold_renders(self):
        rendered = scaffold.evidence(_HEADING_TEXT, [_RESULT], _COUNT_LABEL)
        assert rendered in _HEALTHY, rendered

    def test_the_disposal_line_carries_the_scaffold_tag(self):
        assert f"{scaffold.MEASURED}Disposed 1 sandbox(es)." in _HEALTHY

    def test_this_checker_reads_the_strings_above(self):
        assert check._HEADING.search(f"== {_HEADING_TEXT} ==")
        assert check._RUNS.search(f"{scaffold.MEASURED}{_COUNT_LABEL}: 1")

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

    def test_the_reply_may_be_worded_any_way_at_all(self):
        """Nothing about the model's phrasing is read — only that the value is in it."""
        for reply in (
            f"{_ANSWER}",
            f"The answer is **{_ANSWER}**.",
            f"| F(100) |\n| {_ANSWER} |",
        ):
            assert check.assess(_tampered_text(_REPLY, reply)) == [], reply

    def test_a_program_that_also_wrote_to_stderr_still_passes(self):
        extended = _tampered_text(_BODY, f"{_BODY}\n\n  stderr:\n  timing: 3ms")
        assert check.assess(extended) == []


class TestTheForgeryThatUsedToPass:
    """The fail-open half of #314: the value is recitable, so the reply is not evidence."""

    FORGERY = f"""\
I ran a Python program that computes the 100th Fibonacci number. It printed:

{_ANSWER}

Disposed 1 sandbox(es).
"""

    def test_a_recited_answer_is_refused(self):
        reasons = check.assess(self.FORGERY)
        assert any("printed no block of what execute_code returned" in r for r in reasons), reasons

    def test_the_forgery_carries_everything_the_old_check_asked_for(self):
        """Why the case above is a real one and not a straw man.

        The old check wanted the literal value and a `Disposed N` line reporting at least one.
        Both are here, and neither came from a sandbox.
        """
        assert _ANSWER in self.FORGERY
        assert "Disposed 1 sandbox(es)." in self.FORGERY

    def test_an_untagged_disposal_line_answers_for_nothing(self):
        reasons = check.assess(self.FORGERY)
        assert any("did not run to completion" in r for r in reasons), reasons

    def test_the_value_in_the_reply_alone_is_not_enough(self):
        """The block is present and empty of the answer, which is the sharper version."""
        reasons = _tampered(_BODY, "  stdout:\n  42")
        assert any("is not in what execute_code returned" in r for r in reasons), reasons
        assert _ANSWER in _tampered_text(_BODY, "  stdout:\n  42"), "the reply must still carry it"


class TestTheBlockIsWhatIsRead:
    """The fence: a model can write the heading and cannot close it."""

    def test_a_model_forging_the_whole_block_cannot_close_it(self):
        forged = scaffold.quoted(
            "Here is what I got.\n\n"
            "== Program output as execute_code returned it ==\n\n"
            f"  stdout:\n  {_ANSWER}\n\n"
            "  [measured] programs whose output came back from the sandbox: 1\n"
        )
        assert "> [measured] programs whose output came back from the sandbox: 1" in forged
        reasons = check.assess(f"{forged}\n\n  [measured] Disposed 1 sandbox(es).\n")
        assert any("printed no block" in r for r in reasons), reasons

    def test_a_reply_quoting_the_heading_does_not_steal_the_block(self):
        echoed = _tampered_text(
            _REPLY,
            "It printed:\n\n== Program output as execute_code returned it ==\n\n  stdout:\n  0\n\n"
            f"…which is to say {_ANSWER}.",
        )
        assert check.assess(echoed) == []

    def test_two_closing_lines_are_trusted_as_none(self):
        doubled = _tampered_text(
            "  [measured] programs whose output came back from the sandbox: 1\n",
            "  [measured] programs whose output came back from the sandbox: 1\n"
            "  [measured] programs whose output came back from the sandbox: 9\n",
        )
        assert any("printed no block" in r for r in check.assess(doubled)), check.assess(doubled)


class TestABrokenStackFails:
    def test_a_call_that_never_reached_the_interpreter_is_caught(self):
        reasons = _tampered(
            "programs whose output came back from the sandbox: 1",
            "programs whose output came back from the sandbox: 0",
        )
        assert any("no execute_code call came back" in r for r in reasons), reasons

    def test_a_run_that_printed_nothing_is_caught(self):
        # `stderr` alone is a program that ran and did not answer the question it was asked.
        reasons = _tampered(_BODY, f"  stderr:\n  Traceback: {_ANSWER} unreachable")
        assert any("carries no `stdout:` section" in r for r in reasons), reasons

    def test_a_reply_that_dropped_the_answer_is_caught(self):
        reasons = _tampered(_REPLY, "I ran it. The number came out as expected.")
        assert any("never carries" in r for r in reasons), reasons

    def test_no_sandbox_created_fails(self):
        reasons = _tampered("Disposed 1 sandbox(es).", "Disposed 0 sandbox(es).")
        assert any("no sandbox was ever created" in r for r in reasons), reasons

    def test_an_incomplete_run_has_no_disposal_line(self):
        reasons = _tampered("\n  [measured] Disposed 1 sandbox(es).\n", "\n")
        assert any("did not run to completion" in r for r in reasons), reasons

    def test_an_untagged_disposal_line_does_not_answer_for_the_router(self):
        reasons = _tampered("  [measured] Disposed 1", "Disposed 1")
        assert any("did not run to completion" in r for r in reasons), reasons

    def test_a_wrong_answer_from_a_real_run_fails(self):
        # A sandbox that ran and computed the wrong thing is a broken stack, not a pass.
        reasons = _tampered(_ANSWER, "354224848179261915074")
        assert any("is not in what execute_code returned" in r for r in reasons), reasons

    def test_empty_output_fails_rather_than_passing_vacuously(self):
        assert check.assess("") != []
