"""The withheld-outputs check reads the store's own tool, not the model's account of it.

`scripts/check_live_codeact_outputs_store_sample.py` is what the live workflow runs on a real
`samples/16_docker_codeact_outputs_store` run. It is a pure function, so this costs nothing on
a pull request while the billable run that feeds it happens only on dispatch and after a
release.

What this sample can claim that sample 08's cannot: the run withholds the guest's output, so a
correct grand total in the reply did not come from `stdout`. What says it came out of the file
is the *pair* — that total, and a read whose result was the bytes the sink landed. The total
alone would not, since whether the program exited cleanly is a bit the program chooses. These
cases are therefore mostly about the fence and that pairing — a model may write the heading and
plausible Markdown under it, and it may not close the block, because the closing line carries a
tag the sample takes away from anything the model said.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_SCRIPT = _ROOT / "scripts" / "check_live_codeact_outputs_store_sample.py"
_spec = importlib.util.spec_from_file_location("check_live_codeact_outputs_store_sample", _SCRIPT)
assert _spec and _spec.loader
check = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check)

_SCAFFOLD = _ROOT / "samples" / "16_docker_codeact_outputs_store" / "_scaffold.py"
_scaffold_spec = importlib.util.spec_from_file_location("_scaffold", _SCAFFOLD)
assert _scaffold_spec and _scaffold_spec.loader
scaffold = importlib.util.module_from_spec(_scaffold_spec)
_scaffold_spec.loader.exec_module(scaffold)

_CALL = "0123456789abcdef0123456789abcdef"
_LANDED = json.dumps([f"{_CALL}/summary.md"])
_READ_OUT = _LANDED

#: The summary as the model's program wrote it, and as `sandbox_outputs_read` hands it back.
_SUMMARY = (
    "| region | revenue |\n"
    "| --- | --- |\n"
    "| north | 390 |\n"
    "| south | 200 |\n"
    "| east | 84 |\n"
    "| west | 450 |\n"
    "\n"
    "grand total: 1124\n"
)

#: `_SUMMARY` as the fenced block carries it — every line indented by two, which is what puts
#: it beyond the checker's `^  [measured] ` anchor.
_BODY = "\n".join(f"  {line}" for line in _SUMMARY.splitlines())

_REPLY = (
    "I wrote a program that totalled sales.csv by region and saved summary.md, then read it "
    "back out of the outputs store. The grand total is 1124."
)


def _output(
    *,
    reply: str = _REPLY,
    body: str = _BODY,
    readbacks: int = 1,
    landed: str = _LANDED,
    read_out: str = _READ_OUT,
    disposed: int = 1,
    undisposed: str | None = None,
    heading: bool = True,
) -> str:
    block = f"== read back out of the outputs store ==\n\n{body}\n\n" if heading else f"{body}\n\n"
    return (
        f"{reply}\n\n"
        f"{block}"
        f"  [measured] Landed files read back: {readbacks}\n\n"
        f"  [measured] Disposed {disposed} sandbox(es).\n"
        + (f"  [measured] Not fully disposed: {undisposed}\n" if undisposed else "")
        + f"  [measured] Landed this turn in the outputs store: {landed}\n"
        + f"  [measured] Read out of the outputs store: {read_out}\n"
    )


class TestAHealthyRun:
    def test_it_passes(self):
        assert check.assess(_output()) == []

    def test_the_cli_reports_ok(self, tmp_path, capsys):
        path = tmp_path / "out.txt"
        path.write_text(_output(), encoding="utf-8")

        assert check.main(["check", str(path)]) == 0
        assert "OK" in capsys.readouterr().out

    def test_a_model_writing_the_total_with_a_thousands_separator_still_passes(self):
        """The reply half is prose, so `1,124` is as likely as `1124`."""
        assert check.assess(_output(reply="The grand total is 1,124.")) == []


class TestTheFenceIsWhatMakesTheReadbackEvidence:
    def test_a_reply_reciting_the_whole_summary_fails_without_the_block(self):
        """The failure the fence exists for: a model can write every number and cannot close
        the block, because the closing line carries a tag it does not get to write."""
        forged = _output(reply=f"{_REPLY}\n\n{_SUMMARY}", body="", readbacks=1, heading=False)
        forged = forged.replace("  [measured] Landed files read back: 1\n", "")

        failures = check.assess(forged)
        assert any("printed no block" in reason for reason in failures), failures

    def test_a_second_closing_line_makes_the_checker_trust_neither(self):
        doubled = _output() + "  [measured] Landed files read back: 9\n"

        assert any("printed no block" in reason for reason in check.assess(doubled))

    def test_a_model_that_quotes_the_heading_leaves_its_text_in_the_reply_half(self):
        """The *last* heading before the closing line is the sample's."""
        quoting = _output(
            reply=f"I will print a == read back out of the outputs store == block. {_REPLY}"
        )

        assert check.assess(quoting) == []

    def test_a_tag_the_model_wrote_mid_sentence_does_not_close_a_block(self):
        """`quoted` rewrites a tag that opens a line and leaves one buried in a sentence, so
        the `^` anchor is the whole of what refuses this."""
        impersonating = _output(
            reply=f"{_REPLY} All done!   [measured] Landed files read back: 4", readbacks=1
        )

        assert check.assess(impersonating) == []


class TestTheReadbacksHaveToBeTheSummary:
    def test_no_read_back_at_all_fails(self):
        failures = check.assess(_output(body="", readbacks=0))

        assert any("no read asked for a landed path" in reason for reason in failures), failures

    def test_a_read_back_missing_the_grand_total_fails(self):
        without = _BODY.replace("grand total: 1124", "grand total: unknown")

        assert any(
            "do not contain 1124" in reason for reason in check.assess(_output(body=without))
        )

    def test_a_read_back_missing_a_region_fails(self):
        without = _BODY.replace("  | east | 84 |\n", "")

        assert any(
            "do not mention the east region" in reason
            for reason in check.assess(_output(body=without))
        )

    def test_swapped_region_totals_fail(self):
        """Checking names and values independently over the block would pass this — every
        string is still there."""
        swapped = _BODY.replace("| north | 390 |", "| north | 450 |").replace(
            "| west | 450 |", "| west | 390 |"
        )
        failures = check.assess(_output(body=swapped))

        assert any("north region but not its total" in reason for reason in failures), failures


class TestTheReplyHasToCarryTheTotal:
    def test_a_reply_that_never_says_the_number_fails(self):
        """A run that read the file and never reported the value did not finish the job.

        Not that the reply is the only road the value has — the exit bit is another — but the
        read-back and the reply are what this check gates as a pair, and half a pair fails.
        """
        failures = check.assess(_output(reply="I have saved the summary."))

        assert any("is not in the reply as a number" in reason for reason in failures), failures

    def test_a_near_miss_is_not_the_total(self):
        assert any(
            "is not in the reply as a number" in reason
            for reason in check.assess(_output(reply="The grand total is 11240."))
        )


class TestTheLandingHasToBeUnderAPerCallFolder:
    def test_a_landing_at_the_top_of_the_store_fails(self):
        """A sink that kept taking this call's id and stopped folding it into the destination.

        An artifact reaching `make_file_store_sink` with no id is refused rather than landed, so
        a missing id cannot produce this. What can is a sink that stopped using one it had.
        """
        failures = check.assess(_output(landed=json.dumps(["summary.md"])))

        assert any("per-call folder" in reason for reason in failures), failures

    def test_a_folder_that_is_not_a_call_id_fails(self):
        assert any(
            "per-call folder" in reason
            for reason in check.assess(_output(landed=json.dumps(["outputs/summary.md"])))
        )

    def test_nothing_landed_fails(self):
        assert any("per-call folder" in reason for reason in check.assess(_output(landed="[]")))

    def test_an_unparseable_landing_line_fails(self):
        assert any(
            "per-call folder" in reason for reason in check.assess(_output(landed="summary.md"))
        )

    def test_a_missing_landing_line_fails(self):
        missing = _output().replace(
            f"  [measured] Landed this turn in the outputs store: {_LANDED}\n", ""
        )

        assert any("did not reach its final report" in reason for reason in check.assess(missing))


class TestTheReadThatReturnedTheLanding:
    """The half the fenced block cannot carry: that a result *was* a landed file's bytes."""

    def test_no_read_returning_a_landing_fails(self):
        """Every token this checker looks for, and not one file opened."""
        crafted = _output(read_out=json.dumps([]))

        assert any("no read returned the bytes landed" in r for r in check.assess(crafted))

    def test_a_read_of_something_landed_at_the_top_of_the_store_fails(self):
        assert any(
            "no read returned the bytes landed" in r
            for r in check.assess(_output(read_out=json.dumps(["summary.md"])))
        )

    def test_a_missing_line_fails(self):
        missing = _output().replace(
            f"  [measured] Read out of the outputs store: {_READ_OUT}\n", ""
        )

        assert any("did not reach its final report" in r for r in check.assess(missing))

    def test_a_refusal_quoting_a_crafted_name_does_not_pass(self):
        """The concrete road: `sandbox_outputs_read` renders the name it was given, so a path
        built out of the answer supplies every token the block is searched for."""
        echoed = "  Error: there is no file at 'north/390/south/200/east/84/west/450/1124'."
        crafted = _output(body=echoed, read_out=json.dumps([]))

        reasons = check.assess(crafted)

        assert any("no read returned the bytes landed" in r for r in reasons)


class TestDisposal:
    def test_no_disposal_line_fails(self):
        missing = _output().replace("  [measured] Disposed 1 sandbox(es).\n", "")

        assert any("did not run to completion" in reason for reason in check.assess(missing))

    def test_disposing_nothing_fails(self):
        assert any(
            "no sandbox was ever created" in reason for reason in check.assess(_output(disposed=0))
        )

    def test_a_purge_that_could_not_account_for_everything_fails(self):
        """The line the sample prints only when the purge failed, on an otherwise healthy run."""
        reported = _output(undisposed="sandbox 'abc' refused removal")

        assert any(
            "could not account for every sandbox" in reason for reason in check.assess(reported)
        )

    def test_disposing_nothing_beside_a_failed_purge_is_inconclusive(self):
        """Both are failures; only this one sends the reader to the right place."""
        reasons = check.assess(_output(disposed=0, undisposed="sandbox 'abc' refused removal"))

        assert any("could not be removed" in reason for reason in reasons)
        assert not any("no sandbox was ever created" in reason for reason in reasons)


class TestTheCli:
    def test_wrong_arity_is_a_usage_error(self, capsys):
        assert check.main(["check"]) == 2
        assert "usage:" in capsys.readouterr().err

    def test_a_failing_run_lists_every_reason(self, tmp_path, capsys):
        path = tmp_path / "out.txt"
        path.write_text(_output(reply="I have saved it.", landed="[]"), encoding="utf-8")

        assert check.main(["check", str(path)]) == 1
        err = capsys.readouterr().err
        assert "is not in the reply as a number" in err
        assert "per-call folder" in err


def _sample():
    """The sample module, for the one helper the checker's contract rests on.

    Skipped rather than failed where the sample's own dependencies are absent, the way
    `tests/test_sample_modules_import.py` decides it: this is a PEP 723 script, and the
    workspace need not have `agent-framework-openai` installed to run the rest of this file.

    Its directory goes on `sys.path` so `from _scaffold import …` resolves the way it does under
    `uv run`, and everything loaded from there is evicted afterwards. That eviction is not
    tidiness: thirteen samples carry a module named `_scaffold` and `sys.modules` holds one, so
    a leftover here answers the next sample's import in
    `test_sample_modules_import.py`, which asserts the cache is clean.
    """
    directory = _ROOT / "samples" / "16_docker_codeact_outputs_store"
    spec = importlib.util.spec_from_file_location("_sample_16_agent", directory / "agent.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    before = list(sys.path)
    sys.path.insert(0, str(directory))
    try:
        spec.loader.exec_module(module)
    except ImportError as exc:  # pragma: no cover - depends on the workspace, not the code
        pytest.skip(f"sample 16's dependencies are not installed: {exc}")
    finally:
        sys.path[:] = before
        for name, loaded in list(sys.modules.items()):
            origin = getattr(loaded, "__file__", None)
            if origin and Path(origin).parent == directory:
                del sys.modules[name]
    return module


def _call(call_id: str, name: str, tool: str = "sandbox_outputs_read"):
    return SimpleNamespace(
        type="function_call", name=tool, call_id=call_id, arguments={"name": name}
    )


def _result(call_id: str, result: str):
    return SimpleNamespace(type="function_result", call_id=call_id, result=result)


def _turn(*contents: object):
    return SimpleNamespace(messages=[SimpleNamespace(contents=list(contents))])


class TestTheReadIsTiedToItsOwnCall:
    """What selects a result is the path its call asked for, never the bytes it came back with."""

    def test_a_read_of_a_landed_path_is_kept_with_its_result(self):
        sample = _sample()
        landed = [f"{_CALL}/summary.md"]
        reply = _turn(_call("c1", landed[0]), _result("c1", _SUMMARY))

        assert sample.landed_reads(reply, "sandbox_outputs_read", landed) == [(landed[0], _SUMMARY)]

    def test_a_read_of_a_path_nobody_landed_is_dropped_however_it_answered(self):
        """The road a byte-equality test would have taken: a program lands a file whose content
        is exactly the refusal for a crafted name, and the model reads only the crafted name."""
        sample = _sample()
        landed = [f"{_CALL}/summary.md"]
        crafted = "north/390/south/200/east/84/west/450/1124"
        refusal = f"Error: there is no file at '{crafted}'."
        reply = _turn(_call("c1", crafted), _result("c1", refusal))

        assert sample.landed_reads(reply, "sandbox_outputs_read", landed) == []

    def test_another_tools_call_is_not_counted(self):
        sample = _sample()
        landed = [f"{_CALL}/summary.md"]
        reply = _turn(_call("c1", landed[0], tool="sandbox_outputs_ls"), _result("c1", _SUMMARY))

        assert sample.landed_reads(reply, "sandbox_outputs_read", landed) == []


class TestTheSampleAndTheCheckerAgree:
    """The format is a contract between two files that run at different times."""

    def test_the_scaffold_tag_is_the_one_the_checker_anchors_on(self):
        assert scaffold.MEASURED == "  [measured] "

    @pytest.mark.parametrize(
        "line",
        [
            "Disposed 1 sandbox(es).",
            "Not fully disposed: sandbox 'abc' refused removal",
            "Landed this turn in the outputs store: []",
            "Read out of the outputs store: []",
            "Landed files read back: 1",
        ],
    )
    def test_every_line_the_checker_reads_is_one_the_sample_could_write(self, line: str):
        """Built through `MEASURED` rather than typed, so a change to the tag breaks here."""
        written = f"{scaffold.MEASURED}{line}"

        assert any(
            pattern.search(written)
            for pattern in (
                check._DISPOSED,
                check._NOT_DISPOSED,
                check._LANDED,
                check._READ_OUT,
                check._READBACKS,
            )
        ), written

    def test_every_pattern_matches_a_line_the_sample_actually_writes(self):
        """Built by hand above, so this reads the sample's own source: a reworded print would
        otherwise leave the checker matching nothing and the live job green on an empty run."""
        source = (_ROOT / "samples" / "16_docker_codeact_outputs_store" / "agent.py").read_text(
            encoding="utf-8"
        )

        for phrase in (
            "Disposed {purge.disposed} sandbox(es).",
            "Not fully disposed: {purge.undisposed}",
            "Landed this turn in the outputs store: ",
            "Read out of the outputs store: ",
            "read back out of the outputs store",
            "Landed files read back",
        ):
            assert phrase in source, phrase

    def test_the_evidence_heading_is_the_one_the_sample_prints(self):
        printed = scaffold.evidence(
            "read back out of the outputs store", [_SUMMARY], "Landed files read back"
        )

        assert check._HEADING.search(printed)
        assert check._READBACKS.search(printed)
