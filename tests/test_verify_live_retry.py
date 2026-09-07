"""Exercise every bounded, model-only retry by running the workflow's real shell blocks.

`uv` and `python3` are stubbed so tests can script check results without matching YAML syntax.

A step earns a retry when its live model does open-ended work the check grades: sample 13's
repair turn, and sample 15's two programs on either backend. Each is driven here, so a budget
that only one of them honours is a failure rather than a difference nobody looks at.
"""

from __future__ import annotations

import dataclasses
import importlib.util
import os
import re
import shutil
import subprocess
from pathlib import Path
from types import ModuleType

import pytest
import yaml

_ROOT = Path(__file__).resolve().parent.parent
_WORKFLOW = _ROOT / ".github" / "workflows" / "verify-live.yml"


def _load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, _ROOT / "scripts" / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


check = _load("check_live_fix_loop_sample")
_HOST_TOOLS = _load("check_live_host_tools_call_sample")

_BASH = shutil.which("bash")


@dataclasses.dataclass(frozen=True)
class _Retrying:
    """A live step that spends a second attempt on its model, and the check it keys on.

    `marks` is what identifies the step inside its own `run:` block: sample 15's two legs run
    one `agent.py` on two backends, so the path does not tell them apart and the file each
    tees to does.
    """

    label: str
    marks: str
    check: ModuleType
    readme: Path


#: Every step allowed to loop. The list is the claim: a job that grows a retry by being copied
#: from one of these is spending live sandboxes on something no model wrote, and
#: `test_no_other_live_sample_retries` is what makes that show up here rather than on a bill.
_RETRYING = (
    _Retrying(
        "sample 13",
        "tee /tmp/sample13-out.txt",
        check,
        _ROOT / "samples" / "13_bicep_fix_loop" / "README.md",
    ),
    _Retrying(
        "sample 15",
        "tee /tmp/sample15-out.txt",
        _HOST_TOOLS,
        _ROOT / "samples" / "15_acas_codeact_host_tools" / "README.md",
    ),
    _Retrying(
        "sample 15 on docker",
        "tee /tmp/sample15-docker-out.txt",
        _HOST_TOOLS,
        _ROOT / "samples" / "15_acas_codeact_host_tools" / "README.md",
    ),
)

_EACH = pytest.mark.parametrize("retrying", _RETRYING, ids=lambda r: r.label)


def _the_step(retrying: _Retrying = _RETRYING[0]) -> dict:
    """The step that runs a sample, found by what it runs rather than by its name."""
    workflow = yaml.safe_load(_WORKFLOW.read_text("utf-8"))
    steps = [
        step
        for job in workflow["jobs"].values()
        for step in job.get("steps", [])
        if retrying.marks in step.get("run", "")
    ]
    assert len(steps) == 1, f"expected one step for {retrying.label}, found {len(steps)}"
    return steps[0]


def _budget(retrying: _Retrying = _RETRYING[0]) -> int:
    """The attempt ceiling, read off the step so a deliberate change does not red these tests."""
    assignment = re.search(r"^\s*allowed=(\d+)$", _the_step(retrying)["run"], re.MULTILINE)
    assert assignment is not None, (
        f"the {retrying.label} step no longer assigns `allowed=`; the retry budget is meant to "
        "be one number its loop, its warning and its summary all read"
    )
    return int(assignment.group(1))


@dataclasses.dataclass(frozen=True)
class _Ran:
    """What one execution of the step said: its status, its output, and how many loops it ran."""

    status: int
    stdout: str
    stderr: str
    summary: str
    attempts: int


def _stub(path: Path, body: str) -> None:
    path.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    path.chmod(0o755)


def _run(
    tmp_path: Path,
    codes: list[int],
    *,
    sample_status: int = 0,
    retrying: _Retrying = _RETRYING[0],
) -> _Ran:
    """Drive the real shell block: the check returns ``codes``, the sample ``sample_status``."""
    binaries = tmp_path / "bin"
    binaries.mkdir()
    attempts = tmp_path / "attempts"
    attempts.write_text("", encoding="utf-8")
    # POSIX spellings: these are `sh` scripts, and a Windows path inside one is a broken
    # redirect rather than an error.
    tally = attempts.as_posix()

    _stub(binaries / "uv", f'printf "sample output\n"\nprintf "x" >> {tally}\nexit {sample_status}')
    _stub(
        binaries / "python3",
        f"n=$(wc -c < {tally})\n"
        f"set -- {' '.join(str(code) for code in codes)}\n"
        r'eval "code=\${$n}"' + "\n"
        'printf "check %s -> %s\n" "$n" "$code"\n'
        'exit "$code"',
    )

    summary = tmp_path / "summary.md"
    summary.touch()
    environment = {
        **os.environ,
        "PATH": f"{binaries}{os.pathsep}{os.environ['PATH']}",
        "HARNESS": str(tmp_path),
        "GITHUB_STEP_SUMMARY": str(summary),
    }
    assert _BASH is not None
    finished = subprocess.run(  # noqa: S603 - the repo's own workflow, stubbed binaries
        [_BASH, "-c", _the_step(retrying)["run"]],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=environment,
    )
    return _Ran(
        status=finished.returncode,
        stdout=finished.stdout,
        stderr=finished.stderr,
        summary=summary.read_text(encoding="utf-8"),
        attempts=len(attempts.read_text(encoding="utf-8")),
    )


needs_bash = pytest.mark.skipif(_BASH is None, reason="the step is a shell block")


@needs_bash
@_EACH
class TestTheLoopRetriesTheModelAndNothingElse:
    def test_a_first_attempt_that_passes_is_the_whole_job(
        self, tmp_path: Path, retrying: _Retrying
    ):
        finished = _run(tmp_path, [0], retrying=retrying)
        assert finished.status == 0, finished.stderr
        assert finished.attempts == 1
        assert "::warning" not in finished.stdout

    def test_the_model_half_earns_a_second_loop(self, tmp_path: Path, retrying: _Retrying):
        finished = _run(tmp_path, [retrying.check.MODEL_DID_NOT_CONVERGE, 0], retrying=retrying)
        assert finished.status == 0, finished.stderr
        assert finished.attempts == 2

    def test_a_failure_this_suite_owns_is_not_retried(self, tmp_path: Path, retrying: _Retrying):
        """1 is "something here is broken", and a second live model cannot mend it."""
        finished = _run(tmp_path, [1, 0], retrying=retrying)
        assert finished.status == 1
        assert finished.attempts == 1, "a plumbing failure must not spend a second container"

    def test_the_budget_is_the_ceiling(self, tmp_path: Path, retrying: _Retrying):
        """Every attempt fails, so the loop stops on the budget rather than on a verdict."""
        allowed = _budget(retrying)
        gave_up = [retrying.check.MODEL_DID_NOT_CONVERGE] * (allowed + 1)
        finished = _run(tmp_path, gave_up + [0], retrying=retrying)
        assert finished.status == retrying.check.MODEL_DID_NOT_CONVERGE
        assert finished.attempts == allowed

    def test_the_budget_is_spent_only_as_far_as_it_has_to_be(
        self, tmp_path: Path, retrying: _Retrying
    ):
        """A pass on the last allowed attempt is still a pass, and costs no more than it took."""
        allowed = _budget(retrying)
        gave_up = [retrying.check.MODEL_DID_NOT_CONVERGE] * (allowed - 1)
        finished = _run(tmp_path, gave_up + [0], retrying=retrying)
        assert finished.status == 0, finished.stderr
        assert finished.attempts == allowed


@needs_bash
@_EACH
class TestARetryIsNeverSilent:
    """A retry nobody can see is how a sample that fails half the time reads as healthy."""

    def test_the_retry_is_annotated(self, tmp_path: Path, retrying: _Retrying):
        finished = _run(tmp_path, [retrying.check.MODEL_DID_NOT_CONVERGE, 0], retrying=retrying)
        annotations = [
            line for line in finished.stdout.splitlines() if line.startswith("::warning")
        ]
        assert len(annotations) == 1, finished.stdout
        assert "attempt 1" in annotations[0]

    def test_the_annotation_names_the_half_it_is_retrying(
        self, tmp_path: Path, retrying: _Retrying
    ):
        """A warning that says only "retried" leaves a reader to guess what earned it."""
        finished = _run(tmp_path, [retrying.check.MODEL_DID_NOT_CONVERGE, 0], retrying=retrying)
        note = next(line for line in finished.stdout.splitlines() if line.startswith("::warning"))
        assert "model's half" in note, note

    def test_the_annotation_names_the_step_it_came_from(self, tmp_path: Path, retrying: _Retrying):
        """Three steps can retry, and their warnings land in one log."""
        finished = _run(tmp_path, [retrying.check.MODEL_DID_NOT_CONVERGE, 0], retrying=retrying)
        note = next(line for line in finished.stdout.splitlines() if line.startswith("::warning"))
        assert f"title={retrying.label} retried" in note, note

    def test_the_attempt_count_reaches_the_step_summary(self, tmp_path: Path, retrying: _Retrying):
        finished = _run(tmp_path, [retrying.check.MODEL_DID_NOT_CONVERGE, 0], retrying=retrying)
        assert "2 attempt(s)" in finished.summary, finished.summary

    def test_a_run_that_needed_one_attempt_says_so_too(self, tmp_path: Path, retrying: _Retrying):
        """Otherwise the summary line only appears when something went wrong, and its absence
        is what a reader would have to notice."""
        finished = _run(tmp_path, [0], retrying=retrying)
        assert "1 attempt(s)" in finished.summary, finished.summary

    def test_the_summary_is_written_even_when_the_job_fails(
        self, tmp_path: Path, retrying: _Retrying
    ):
        finished = _run(tmp_path, [1], retrying=retrying)
        assert "exit 1 after 1 attempt(s)" in finished.summary, finished.summary

    def test_the_summary_names_the_step_it_describes(self, tmp_path: Path, retrying: _Retrying):
        """One job summary carries all of them, so an unattributed line describes nobody."""
        finished = _run(tmp_path, [0], retrying=retrying)
        assert finished.summary.startswith("samples/"), finished.summary


@needs_bash
class TestTheFixLoopAnnotationDoesNotBlameOneTurn:
    """Status 3 covers turn 1 as well, so naming the fix turn can be a false statement."""

    def test_it_names_neither_turn(self, tmp_path: Path):
        finished = _run(tmp_path, [check.MODEL_DID_NOT_CONVERGE, 0])
        note = next(line for line in finished.stdout.splitlines() if line.startswith("::warning"))
        assert "fix turn" not in note, note


@needs_bash
@_EACH
class TestASampleThatNeverRanIsNotTheModelsHalf:
    """A crash before the check measured nothing, so it neither retries nor goes unrecorded.

    `set -euo pipefail` used to end the step at the pipe, taking the attempt count with it, and
    the harness only ever made the sample succeed — so nothing here noticed.
    """

    def test_a_crashing_sample_is_not_retried(self, tmp_path: Path, retrying: _Retrying):
        finished = _run(tmp_path, [0], sample_status=7, retrying=retrying)
        assert finished.attempts == 1
        assert finished.status == 7

    def test_the_attempt_count_survives_it(self, tmp_path: Path, retrying: _Retrying):
        finished = _run(tmp_path, [0], sample_status=7, retrying=retrying)
        assert "exit 7 after 1 attempt(s)" in finished.summary, finished.summary

    def test_the_run_says_the_sample_never_reached_the_check(
        self, tmp_path: Path, retrying: _Retrying
    ):
        finished = _run(tmp_path, [0], sample_status=7, retrying=retrying)
        errors = [line for line in finished.stdout.splitlines() if line.startswith("::error")]
        assert len(errors) == 1, finished.stdout
        assert "exited 7" in errors[0], errors

    def test_a_sample_exiting_the_retryable_status_still_does_not_retry(
        self, tmp_path: Path, retrying: _Retrying
    ):
        """3 from the *sample* is a crash that shares a number, not a verdict about a repair."""
        status = retrying.check.MODEL_DID_NOT_CONVERGE
        finished = _run(tmp_path, [0], sample_status=status, retrying=retrying)
        assert finished.attempts == 1


@_EACH
class TestTheBudgetIsWrittenOnce:
    """A loop bounded at one figure while the summary claims another reports a run nobody had."""

    def test_the_loop_is_bounded_by_the_variable(self, retrying: _Retrying):
        assert 'while [ "$attempts" -lt "$allowed" ]' in _the_step(retrying)["run"]

    def test_the_retry_notice_reads_the_variable(self, retrying: _Retrying):
        """Which attempt of how many, so a reader is not counting warnings to find out."""
        run = _the_step(retrying)["run"]
        assert 'if [ "$attempts" -lt "$allowed" ]' in run
        assert "attempt $attempts of $allowed" in run

    def test_the_summary_reads_the_variable(self, retrying: _Retrying):
        assert "after $attempts attempt(s), $allowed allowed." in _the_step(retrying)["run"]

    def test_a_budget_of_one_is_the_retry_removed(self, retrying: _Retrying):
        """That is #421 undone rather than tuned, and it would pass every test above."""
        assert _budget(retrying) >= 2

    def test_the_sample_readme_states_the_number_the_workflow_allows(self, retrying: _Retrying):
        """Raising one without the other leaves the documented behaviour disagreeing here."""
        words = {
            2: "twice",
            3: "three times",
            4: "four times",
            5: "five times",
            6: "six times",
            7: "seven times",
            8: "eight times",
        }
        allowed = _budget(retrying)
        assert allowed in words, f"add {allowed} to this table when raising the budget past 8"
        readme = retrying.readme.read_text("utf-8")
        assert f"**{words[allowed]} at most**" in readme, (
            f"{retrying.readme.relative_to(_ROOT).as_posix()} does not say the run happens "
            f"{words[allowed]} at most; the workflow allows {allowed} for {retrying.label}"
        )


class TestTheTwoFilesAgreeOnWhatIsRetryable:
    @_EACH
    def test_the_workflow_keys_on_the_status_the_check_returns(self, retrying: _Retrying):
        """Renumbering `MODEL_DID_NOT_CONVERGE` would otherwise disable the retry in silence."""
        run = _the_step(retrying)["run"]
        assert f'[ "$status" -eq {retrying.check.MODEL_DID_NOT_CONVERGE} ]' in run, run

    @_EACH
    def test_that_status_is_not_one_the_check_uses_for_anything_else(self, retrying: _Retrying):
        assert retrying.check.MODEL_DID_NOT_CONVERGE not in (0, 1, 2)

    def test_no_other_live_sample_retries(self):
        """A retry is earned by having a live model write something the check then grades, and
        only these steps do. One that grew a loop by being copied from them would be spending
        live sandboxes re-asking a question whose answer cannot change between attempts.
        """
        workflow = yaml.safe_load(_WORKFLOW.read_text("utf-8"))
        looping = {
            step.get("name", "?")
            for job in workflow["jobs"].values()
            for step in job.get("steps", [])
            if "attempts=" in step.get("run", "")
        }
        assert looping == {_the_step(r)["name"] for r in _RETRYING}, looping

    def test_each_retrying_step_is_a_distinct_step(self):
        """Two entries resolving to one step would leave a real one undriven and unnoticed."""
        names = [_the_step(r)["name"] for r in _RETRYING]
        assert len(set(names)) == len(names), names
