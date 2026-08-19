"""Exercise sample 13's bounded, model-only retry by running the workflow's real shell block.

`uv` and `python3` are stubbed so tests can script check results without matching YAML syntax.
"""

from __future__ import annotations

import dataclasses
import importlib.util
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

_ROOT = Path(__file__).resolve().parent.parent
_WORKFLOW = _ROOT / ".github" / "workflows" / "verify-live.yml"
_SCRIPT = _ROOT / "scripts" / "check_live_fix_loop_sample.py"

_spec = importlib.util.spec_from_file_location("check_live_fix_loop_sample", _SCRIPT)
assert _spec and _spec.loader
check = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check)

_BASH = shutil.which("bash")


def _the_step() -> dict:
    """The step that runs sample 13, found by what it runs rather than by its name."""
    workflow = yaml.safe_load(_WORKFLOW.read_text("utf-8"))
    steps = [
        step
        for job in workflow["jobs"].values()
        for step in job.get("steps", [])
        if "samples/13_bicep_fix_loop/agent.py" in step.get("run", "")
    ]
    assert len(steps) == 1, f"expected one step to run sample 13, found {len(steps)}"
    return steps[0]


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


def _run(tmp_path: Path, codes: list[int], *, sample_status: int = 0) -> _Ran:
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
        [_BASH, "-c", _the_step()["run"]],
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
class TestTheLoopRunsTwiceForTheModelAndOnceForEverythingElse:
    def test_a_first_attempt_that_passes_is_the_whole_job(self, tmp_path: Path):
        finished = _run(tmp_path, [0])
        assert finished.status == 0, finished.stderr
        assert finished.attempts == 1
        assert "::warning" not in finished.stdout

    def test_the_model_half_earns_a_second_loop(self, tmp_path: Path):
        finished = _run(tmp_path, [check.MODEL_DID_NOT_CONVERGE, 0])
        assert finished.status == 0, finished.stderr
        assert finished.attempts == 2

    def test_a_failure_this_suite_owns_is_not_retried(self, tmp_path: Path):
        """1 is "something here is broken", and a second live model cannot mend it."""
        finished = _run(tmp_path, [1, 0])
        assert finished.status == 1
        assert finished.attempts == 1, "a plumbing failure must not spend a second container"

    def test_two_attempts_is_the_ceiling(self, tmp_path: Path):
        codes = [check.MODEL_DID_NOT_CONVERGE, check.MODEL_DID_NOT_CONVERGE, 0]
        finished = _run(tmp_path, codes)
        assert finished.status == check.MODEL_DID_NOT_CONVERGE
        assert finished.attempts == 2


@needs_bash
class TestARetryIsNeverSilent:
    """A retry nobody can see is how a sample that fails half the time reads as healthy."""

    def test_the_retry_is_annotated(self, tmp_path: Path):
        finished = _run(tmp_path, [check.MODEL_DID_NOT_CONVERGE, 0])
        annotations = [
            line for line in finished.stdout.splitlines() if line.startswith("::warning")
        ]
        assert len(annotations) == 1, finished.stdout
        assert "attempt 1" in annotations[0]

    def test_the_annotation_does_not_blame_one_turn(self, tmp_path: Path):
        """Status 3 covers turn 1 as well, so naming the fix turn can be a false statement."""
        finished = _run(tmp_path, [check.MODEL_DID_NOT_CONVERGE, 0])
        note = next(line for line in finished.stdout.splitlines() if line.startswith("::warning"))
        assert "fix turn" not in note, note
        assert "model's half" in note, note

    def test_the_attempt_count_reaches_the_step_summary(self, tmp_path: Path):
        finished = _run(tmp_path, [check.MODEL_DID_NOT_CONVERGE, 0])
        assert "2 attempt(s)" in finished.summary, finished.summary

    def test_a_run_that_needed_one_attempt_says_so_too(self, tmp_path: Path):
        """Otherwise the summary line only appears when something went wrong, and its absence
        is what a reader would have to notice."""
        finished = _run(tmp_path, [0])
        assert "1 attempt(s)" in finished.summary, finished.summary

    def test_the_summary_is_written_even_when_the_job_fails(self, tmp_path: Path):
        finished = _run(tmp_path, [1])
        assert "exit 1 after 1 attempt(s)" in finished.summary, finished.summary


@needs_bash
class TestASampleThatNeverRanIsNotTheModelsHalf:
    """A crash before the check measured nothing, so it neither retries nor goes unrecorded.

    `set -euo pipefail` used to end the step at the pipe, taking the attempt count with it, and
    the harness only ever made the sample succeed — so nothing here noticed.
    """

    def test_a_crashing_sample_is_not_retried(self, tmp_path: Path):
        finished = _run(tmp_path, [0], sample_status=7)
        assert finished.attempts == 1
        assert finished.status == 7

    def test_the_attempt_count_survives_it(self, tmp_path: Path):
        finished = _run(tmp_path, [0], sample_status=7)
        assert "exit 7 after 1 attempt(s)" in finished.summary, finished.summary

    def test_the_run_says_the_sample_never_reached_the_check(self, tmp_path: Path):
        finished = _run(tmp_path, [0], sample_status=7)
        errors = [line for line in finished.stdout.splitlines() if line.startswith("::error")]
        assert len(errors) == 1, finished.stdout
        assert "exited 7" in errors[0], errors

    def test_a_sample_exiting_the_retryable_status_still_does_not_retry(self, tmp_path: Path):
        """3 from the *sample* is a crash that shares a number, not a verdict about a repair."""
        finished = _run(tmp_path, [0], sample_status=check.MODEL_DID_NOT_CONVERGE)
        assert finished.attempts == 1


class TestTheTwoFilesAgreeOnWhatIsRetryable:
    def test_the_workflow_keys_on_the_status_the_check_returns(self):
        """Renumbering `MODEL_DID_NOT_CONVERGE` would otherwise disable the retry in silence."""
        run = _the_step()["run"]
        assert f'[ "$status" -eq {check.MODEL_DID_NOT_CONVERGE} ]' in run, run

    def test_that_status_is_not_one_the_check_uses_for_anything_else(self):
        assert check.MODEL_DID_NOT_CONVERGE not in (0, 1, 2)

    def test_no_other_live_sample_retries(self):
        """Sample 13 is the only job with an open-ended repair in it, so it is the only one
        with a case for a second live attempt. A job that grew one by being copied from this
        would be spending containers on a claim that does not need them."""
        workflow = yaml.safe_load(_WORKFLOW.read_text("utf-8"))
        looping = [
            step.get("name", "?")
            for job in workflow["jobs"].values()
            for step in job.get("steps", [])
            if "attempts=" in step.get("run", "")
        ]
        assert looping == [_the_step()["name"]], looping
