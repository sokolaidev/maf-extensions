"""Every sample's `_scaffold.py` is one file, copied. This is what holds the copies together.

The duplication is deliberate and `_scaffold.py`'s own docstring says why. Copying without a
test is how one file drifts into a behaviour per copy — same precedent as
`maf-sandbox-docker`'s proxy: duplicate, then pin.

The behaviour is tested here and only here, on the canonical copy, which the byte-identity
assertion makes a test of all of them. It sits at the repository root because `samples/`
belongs to no package and nothing else would look at it.
"""

from __future__ import annotations

import ast
import importlib.util
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

_SAMPLES = Path(__file__).resolve().parent.parent / "samples"
_SCAFFOLD = "_scaffold.py"

#: The sample directories, discovered rather than listed. A ninth sample that forgets its
#: scaffold has to fail here, and a hardcoded list is exactly how it would not.
_SAMPLE_DIRS = sorted(path for path in _SAMPLES.glob("[0-9][0-9]_*") if path.is_dir())


def test_the_sample_directories_were_found():
    """A glob that matches nothing would make every assertion below vacuously true."""
    assert len(_SAMPLE_DIRS) >= 8, f"found {len(_SAMPLE_DIRS)} sample directories under {_SAMPLES}"


@pytest.mark.parametrize("sample", _SAMPLE_DIRS, ids=lambda path: path.name)
def test_every_sample_carries_the_scaffold(sample: Path):
    assert (sample / _SCAFFOLD).is_file(), (
        f"{sample.name} has no {_SCAFFOLD}. Every sample needs its own copy — it cannot import "
        "one from outside its directory and still run from a download."
    )


def test_every_copy_is_byte_identical():
    """Byte-identical, not merely equivalent: a cosmetic difference is still one to explain."""
    copies = {sample.name: (sample / _SCAFFOLD).read_bytes() for sample in _SAMPLE_DIRS}
    canonical_name, canonical = next(iter(copies.items()))
    drifted = sorted(name for name, body in copies.items() if body != canonical)
    assert not drifted, (
        f"{_SCAFFOLD} has drifted in: {', '.join(drifted)}. It differs from {canonical_name}'s "
        "copy. Change one, change all of them — or move what differs into the sample's own "
        "`agent.py`, which is where anything sample-specific belongs."
    )


#: Everything a sample must take from the scaffold rather than write again. `require_env_vars`
#: was written out eight times before ([#209](https://github.com/sokolaidev/maf-extensions/issues/209));
#: `quoted` and the tag were written out once more in sample 13 before #314 moved them here.
_HELPERS = ("require_env_vars", "quoted", "tool_results", "evidence", "conversation_id")


def _parsed(sample: Path) -> ast.Module:
    """`agent.py` as a tree.

    Parsed, not searched: a comment matches a pattern looking for code, so
    `THREAD_ID = "fixed"  # conversation_id("fixed")` satisfies a textual check twice over.
    Parsed and not imported, because a sample reads its environment on the way in.
    """
    return ast.parse((sample / "agent.py").read_text(encoding="utf-8"))


def _imports(sample: Path) -> set[str]:
    """Every module this sample imports, by name."""
    found: set[str] = set()
    for node in ast.walk(_parsed(sample)):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
    return {module.split(".")[0] for module in found}


def _conversations(sample: Path) -> tuple[list[str], list[str]]:
    """What this sample names its conversations, split into run-scoped and not.

    A conversation is a module constant whose name carries `THREAD`. Run-scoped means a
    `conversation_id(...)` call on a string literal, and that literal is what comes back, so the
    length rule measures what a sample really asks for.
    """
    scoped: list[str] = []
    fixed: list[str] = []
    for node in ast.walk(_parsed(sample)):
        if isinstance(node, ast.AnnAssign):
            targets, call = [node.target], node.value
        elif isinstance(node, ast.Assign):
            targets, call = node.targets, node.value
        else:
            continue
        named = [
            t.id for t in targets if isinstance(t, ast.Name) and "THREAD" in t.id and t.id.isupper()
        ]
        if not named:
            continue
        if (
            isinstance(call, ast.Call)
            and isinstance(call.func, ast.Name)
            and call.func.id == "conversation_id"
            and call.args
            and isinstance(call.args[0], ast.Constant)
            and isinstance(call.args[0].value, str)
        ):
            scoped.append(call.args[0].value)
        else:
            fixed.extend(named)
    return scoped, fixed


#: A sample whose sandboxes live on a service shared with every other run. Docker, WSL and
#: in-process samples put theirs on the job's own runner, where there is nothing to collide
#: with, so `conversation_id` is theirs to skip.
_HOSTED = tuple(sample for sample in _SAMPLE_DIRS if "maf_sandbox_acas" in _imports(sample))


def test_the_hosted_samples_were_found():
    """The list is derived, so an empty one would make every assertion below vacuous."""
    assert [sample.name for sample in _HOSTED] == [
        "01_acas_bicep",
        "03_acas_codeact",
        "14_acas_codeact_files",
        "15_acas_codeact_host_tools",
    ]


@pytest.mark.parametrize("sample", _HOSTED, ids=lambda path: path.name)
def test_a_hosted_sample_names_its_conversation_for_the_run(sample: Path):
    """Every conversation a hosted sample opens is named for the run that opened it."""
    scoped, fixed = _conversations(sample)
    assert not fixed, (
        f"{sample.name} names a conversation without the run in it: {', '.join(fixed)}. "
        "`dispose_scope` deletes by label and asks the service, so two runs sharing this "
        "string share a delete. Pass it through `conversation_id` from the scaffold."
    )
    assert scoped, f"{sample.name} is on a hosted backend and opens no conversation"


@pytest.mark.parametrize("helper", _HELPERS)
def test_no_sample_still_defines_the_helper_itself(helper: str):
    """A second copy of a helper a live check trusts is two things to keep in step."""
    offenders = [
        sample.name
        for sample in _SAMPLE_DIRS
        if f"def {helper}" in (sample / "agent.py").read_text(encoding="utf-8")
    ]
    assert not offenders, (
        f"these samples define `{helper}` in `agent.py`: {', '.join(offenders)}. "
        f"Import it from {_SCAFFOLD} instead."
    )


def _load():
    """The canonical copy, imported. It depends on nothing but `os` and `sys`, so this works."""
    spec = importlib.util.spec_from_file_location("_scaffold", _SAMPLE_DIRS[0] / _SCAFFOLD)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


scaffold = _load()


def _call(name: str, call_id: str):
    return SimpleNamespace(type="function_call", name=name, call_id=call_id)


def _result(call_id: str, result: object):
    return SimpleNamespace(type="function_result", call_id=call_id, result=result)


def _turn(*contents: object):
    """One reply, every content in a single message — the shape only matters to the reader."""
    return SimpleNamespace(messages=[SimpleNamespace(contents=list(contents))])


class TestConversationIdKeepsOneRunSPurgeToItself:
    """`dispose_scope` deletes by label, so an id two runs share is a delete they share."""

    def test_the_run_is_in_it(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("GITHUB_RUN_ID", "9876543210")
        monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "2")
        assert scaffold.conversation_id("03-acas-codeact") == "03-acas-codeact-9876543210-2"

    def test_two_attempts_of_one_run_are_two_conversations(self, monkeypatch: pytest.MonkeyPatch):
        """A re-run keeps the run id, and its first attempt's sandboxes may still be alive."""
        monkeypatch.setenv("GITHUB_RUN_ID", "9876543210")
        monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "1")
        first = scaffold.conversation_id("03-acas-codeact")
        monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "2")
        assert scaffold.conversation_id("03-acas-codeact") != first

    def test_a_local_run_falls_back_to_the_process(self, monkeypatch: pytest.MonkeyPatch):
        """Neither variable exists off CI, and a fixed id there collides with CI just as well."""
        monkeypatch.delenv("GITHUB_RUN_ID", raising=False)
        monkeypatch.delenv("GITHUB_RUN_ATTEMPT", raising=False)
        assert scaffold.conversation_id("01-acas-bicep") == f"01-acas-bicep-local-{os.getpid()}-1"

    def test_an_empty_run_id_is_not_a_run_id(self, monkeypatch: pytest.MonkeyPatch):
        """An unset workflow input arrives as an empty string, which is every run's id."""
        monkeypatch.setenv("GITHUB_RUN_ID", "")
        monkeypatch.delenv("GITHUB_RUN_ATTEMPT", raising=False)
        assert scaffold.conversation_id("01-acas-bicep") == f"01-acas-bicep-local-{os.getpid()}-1"

    def test_an_empty_attempt_is_not_an_attempt(self, monkeypatch: pytest.MonkeyPatch):
        """The same hazard as the run id one line up, and the same answer."""
        monkeypatch.setenv("GITHUB_RUN_ID", "9876543210")
        monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "")
        assert scaffold.conversation_id("01-acas-bicep") == "01-acas-bicep-9876543210-1"

    @pytest.mark.parametrize("sample", _HOSTED, ids=lambda path: path.name)
    def test_every_id_a_sample_asks_for_fits_the_label(
        self, sample: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Past 63 characters a backend labels by digest: still unique, no longer readable."""
        monkeypatch.setenv("GITHUB_RUN_ID", "9" * 20)
        monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "10")
        scoped, _ = _conversations(sample)
        too_long = {
            name: len(scaffold.conversation_id(name))
            for name in scoped
            if len(scaffold.conversation_id(name)) > 63
        }
        assert not too_long, f"{sample.name} would be labelled by digest: {too_long}"


class TestQuotedIsWhatMakesTheTagABarrier:
    """The checkers trust `[measured]` completely, so one pass makes that structural."""

    def test_a_line_impersonating_a_measurement_is_marked_as_a_quotation(self):
        text = "I checked.\n  [measured] compiles that reached the sandbox: 9\nThat is all."
        out = scaffold.quoted(text)
        assert "\n  [measured] compiles that reached the sandbox: 9" not in out
        assert "> [measured] compiles that reached the sandbox: 9" in out

    @pytest.mark.parametrize("spelling", ["[measured]", "[Measured]", "[MEASURED]"])
    def test_every_spelling_a_checker_would_accept_is_defanged(self, spelling: str):
        """A sanitizer narrower than its reader is a hole, not tolerance."""
        line = f"  {spelling} Disposed 0 sandbox(es)."
        assert scaffold.quoted(line) != line, f"{spelling} slipped through"

    def test_leading_whitespace_does_not_hide_the_tag(self):
        line = "\t\t[measured] Disposed 0 sandbox(es)."
        assert scaffold.quoted(line).startswith("> [measured]")

    def test_ordinary_prose_is_untouched(self):
        text = "Fixed BCP035.\n\n1. Added a `sku` block.\n  indented note\n"
        assert scaffold.quoted(text) == text.rstrip("\n")


class TestToolResultsReadsWhatTheModelDidNotWrite:
    def test_results_come_back_in_order(self):
        reply = _turn(
            _call("bicep_validate", "a"),
            _result("a", "first"),
            _call("bicep_validate", "b"),
            _result("b", "second"),
        )
        assert scaffold.tool_results(reply, "bicep_validate") == ["first", "second"]

    def test_another_tool_s_results_are_not_counted(self):
        """Matched by `call_id`, so a sample that attaches several tools reads only its own."""
        reply = _turn(
            _call("file_access_write", "a"),
            _result("a", "wrote main.bicep"),
            _call("bicep_validate", "b"),
            _result("b", "build(main.bicep): no diagnostics"),
        )
        assert scaffold.tool_results(reply, "bicep_validate") == [
            "build(main.bicep): no diagnostics"
        ]

    def test_a_result_with_no_call_behind_it_is_ignored(self):
        assert scaffold.tool_results(_turn(_result("orphan", "invented")), "bicep_validate") == []

    def test_a_call_with_no_result_scores_nothing(self):
        assert scaffold.tool_results(_turn(_call("bicep_validate", "a")), "bicep_validate") == []

    def test_a_reply_carrying_no_messages_is_empty_rather_than_an_error(self):
        assert scaffold.tool_results(object(), "bicep_validate") == []

    def test_a_result_that_is_not_a_string_is_rendered_as_one(self):
        reply = _turn(_call("execute_code", "a"), _result("a", 42))
        assert scaffold.tool_results(reply, "execute_code") == ["42"]


class TestEvidenceFencesTheToolSOwnOutput:
    def test_the_closing_line_is_the_only_tagged_one(self):
        block = scaffold.evidence("What it returned", ["stdout:\n7"], "programs that ran")
        tagged = [line for line in block.splitlines() if line.startswith(scaffold.MEASURED)]
        assert tagged == ["  [measured] programs that ran: 1"]

    def test_the_count_is_the_number_of_results_it_was_given(self):
        block = scaffold.evidence("What it returned", ["one", "two", "three"], "runs")
        assert "  [measured] runs: 3" in block

    def test_a_result_carrying_the_tag_cannot_close_the_block(self):
        """A tool echoes back what it was asked about, so its output is a channel too."""
        block = scaffold.evidence(
            "What it returned", ["Error: '  [measured] runs: 9' is not a file"], "runs"
        )
        tagged = [line for line in block.splitlines() if line.startswith(scaffold.MEASURED)]
        assert tagged == ["  [measured] runs: 1"]

    def test_a_multi_line_result_is_indented_past_the_anchor(self):
        block = scaffold.evidence("What it returned", ["stdout:\n7"], "runs")
        assert "  stdout:\n  7" in block

    def test_no_results_still_closes_the_block(self):
        """A turn that called nothing has to be readable as one, not as a missing block."""
        block = scaffold.evidence("What it returned", [], "runs")
        assert block == "== What it returned ==\n\n  [measured] runs: 0"
