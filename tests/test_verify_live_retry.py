"""Exercise every bounded, model-only retry through the production Python implementation."""

from __future__ import annotations

import contextlib
import dataclasses
import io
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from _workflow_commands import RETRYING, Retrying, command_arguments, retry_step

pytestmark = pytest.mark.workflow

_ROOT = Path(__file__).resolve().parent.parent
_WORKFLOW = _ROOT / ".github" / "workflows" / "verify-live.yml"


_search_path = sys.path.copy()
try:
    sys.path.insert(0, str(_ROOT / "scripts"))
    import retry_live_sample as runner
finally:
    sys.path[:] = _search_path

check = RETRYING[0].check
_EACH = pytest.mark.parametrize("retrying", RETRYING, ids=lambda r: r.label)


def _budget(retrying: Retrying = RETRYING[0]) -> int:
    """The attempt ceiling, read off the step so a deliberate change does not red these tests."""
    arguments = command_arguments(retry_step(retrying)["run"], "retry_live_sample.py", {})
    return int(arguments[arguments.index("--allowed") + 1])


def _looping(workflow: dict) -> list[str]:
    """Every step carrying a retry loop, counted rather than deduplicated."""
    return sorted(
        step.get("name", "?")
        for job in workflow["jobs"].values()
        for step in job.get("steps", [])
        if "retry_live_sample.py" in step.get("run", "") or "attempts=" in step.get("run", "")
    )


def _declared() -> list[str]:
    return sorted(retry_step(retrying)["name"] for retrying in RETRYING)


@dataclasses.dataclass(frozen=True)
class _Ran:
    """What one execution of the step said: its status, its output, and how many loops it ran."""

    status: int
    stdout: str
    stderr: str
    summary: str
    attempts: int


def _run(
    tmp_path: Path,
    codes: list[int],
    *,
    sample_status: int = 0,
    retrying: Retrying = RETRYING[0],
    allowed: int | None = None,
) -> _Ran:
    """Drive the production retry policy with controlled sample and checker results."""
    arguments = command_arguments(retry_step(retrying)["run"], "retry_live_sample.py", {})
    attempts = 0
    checks = iter(codes)
    summary = tmp_path / "summary.md"
    stdout, stderr = io.StringIO(), io.StringIO()

    def sample() -> int:
        nonlocal attempts
        attempts += 1
        print("sample output")
        return sample_status

    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        status = runner.retry(
            arguments[0],
            _budget(retrying) if allowed is None else allowed,
            summary,
            sample,
            lambda: next(checks),
        )
    return _Ran(status, stdout.getvalue(), stderr.getvalue(), summary.read_text("utf-8"), attempts)


@_EACH
class TestTheLoopRetriesTheModelAndNothingElse:
    def test_a_first_attempt_that_passes_is_the_whole_job(self, tmp_path: Path, retrying: Retrying):
        finished = _run(tmp_path, [0], retrying=retrying)
        assert finished.status == 0, finished.stderr
        assert finished.attempts == 1
        assert "::warning" not in finished.stdout

    def test_the_model_half_earns_a_second_loop(self, tmp_path: Path, retrying: Retrying):
        finished = _run(tmp_path, [retrying.check.MODEL_DID_NOT_CONVERGE, 0], retrying=retrying)
        assert finished.status == 0, finished.stderr
        assert finished.attempts == 2

    def test_a_failure_this_suite_owns_is_not_retried(self, tmp_path: Path, retrying: Retrying):
        """1 is "something here is broken", and a second live model cannot mend it."""
        finished = _run(tmp_path, [1, 0], retrying=retrying)
        assert finished.status == 1
        assert finished.attempts == 1, "a plumbing failure must not spend a second container"

    def test_the_budget_is_the_ceiling(self, tmp_path: Path, retrying: Retrying):
        """Every attempt fails, so the loop stops on the budget rather than on a verdict."""
        allowed = _budget(retrying)
        gave_up = [retrying.check.MODEL_DID_NOT_CONVERGE] * (allowed + 1)
        finished = _run(tmp_path, gave_up + [0], retrying=retrying)
        assert finished.status == retrying.check.MODEL_DID_NOT_CONVERGE
        assert finished.attempts == allowed

    def test_the_budget_is_spent_only_as_far_as_it_has_to_be(
        self, tmp_path: Path, retrying: Retrying
    ):
        """A pass on the last allowed attempt is still a pass, and costs no more than it took."""
        allowed = _budget(retrying)
        gave_up = [retrying.check.MODEL_DID_NOT_CONVERGE] * (allowed - 1)
        finished = _run(tmp_path, gave_up + [0], retrying=retrying)
        assert finished.status == 0, finished.stderr
        assert finished.attempts == allowed


@_EACH
class TestARetryIsNeverSilent:
    """A retry nobody can see is how a sample that fails half the time reads as healthy."""

    def test_the_retry_is_annotated(self, tmp_path: Path, retrying: Retrying):
        finished = _run(tmp_path, [retrying.check.MODEL_DID_NOT_CONVERGE, 0], retrying=retrying)
        annotations = [
            line for line in finished.stdout.splitlines() if line.startswith("::warning")
        ]
        assert len(annotations) == 1, finished.stdout
        assert "attempt 1" in annotations[0]

    def test_the_annotation_names_the_half_it_is_retrying(self, tmp_path: Path, retrying: Retrying):
        """A bare "retried" leaves a reader to guess what earned it."""
        finished = _run(tmp_path, [retrying.check.MODEL_DID_NOT_CONVERGE, 0], retrying=retrying)
        note = next(line for line in finished.stdout.splitlines() if line.startswith("::warning"))
        assert "model's half" in note, note

    def test_the_annotation_names_the_step_it_came_from(self, tmp_path: Path, retrying: Retrying):
        """Their warnings land in one log."""
        finished = _run(tmp_path, [retrying.check.MODEL_DID_NOT_CONVERGE, 0], retrying=retrying)
        note = next(line for line in finished.stdout.splitlines() if line.startswith("::warning"))
        assert f"title={retrying.label} retried" in note, note

    def test_the_attempt_count_reaches_the_step_summary(self, tmp_path: Path, retrying: Retrying):
        finished = _run(tmp_path, [retrying.check.MODEL_DID_NOT_CONVERGE, 0], retrying=retrying)
        assert "2 attempt(s)" in finished.summary, finished.summary

    def test_a_run_that_needed_one_attempt_says_so_too(self, tmp_path: Path, retrying: Retrying):
        """Otherwise its absence is what a reader has to notice."""
        finished = _run(tmp_path, [0], retrying=retrying)
        assert "1 attempt(s)" in finished.summary, finished.summary

    def test_the_summary_is_written_even_when_the_job_fails(
        self, tmp_path: Path, retrying: Retrying
    ):
        finished = _run(tmp_path, [1], retrying=retrying)
        assert "exit 1 after 1 attempt(s)" in finished.summary, finished.summary

    def test_the_summary_names_the_step_it_describes(self, tmp_path: Path, retrying: Retrying):
        """One summary carries all of them, so an unattributed line describes nobody."""
        finished = _run(tmp_path, [0], retrying=retrying)
        assert finished.summary.startswith("samples/"), finished.summary


class TestTheFixLoopAnnotationDoesNotBlameOneTurn:
    """Status 3 covers turn 1 as well, so naming the fix turn can be a false statement."""

    def test_it_names_neither_turn(self, tmp_path: Path):
        finished = _run(tmp_path, [check.MODEL_DID_NOT_CONVERGE, 0])
        note = next(line for line in finished.stdout.splitlines() if line.startswith("::warning"))
        assert "fix turn" not in note, note


@_EACH
class TestASampleThatNeverRanIsNotTheModelsHalf:
    """A crash before the check measured nothing, so it neither retries nor goes unrecorded."""

    def test_a_crashing_sample_is_not_retried(self, tmp_path: Path, retrying: Retrying):
        finished = _run(tmp_path, [0], sample_status=7, retrying=retrying)
        assert finished.attempts == 1
        assert finished.status == 7

    def test_the_attempt_count_survives_it(self, tmp_path: Path, retrying: Retrying):
        finished = _run(tmp_path, [0], sample_status=7, retrying=retrying)
        assert "exit 7 after 1 attempt(s)" in finished.summary, finished.summary

    def test_the_run_says_the_sample_never_reached_the_check(
        self, tmp_path: Path, retrying: Retrying
    ):
        finished = _run(tmp_path, [0], sample_status=7, retrying=retrying)
        errors = [line for line in finished.stdout.splitlines() if line.startswith("::error")]
        assert len(errors) == 1, finished.stdout
        assert "exited 7" in errors[0], errors

    def test_a_sample_exiting_the_retryable_status_still_does_not_retry(
        self, tmp_path: Path, retrying: Retrying
    ):
        """3 from the *sample* is a crash that shares a number, not a verdict about a repair."""
        status = retrying.check.MODEL_DID_NOT_CONVERGE
        finished = _run(tmp_path, [0], sample_status=status, retrying=retrying)
        assert finished.attempts == 1


@_EACH
class TestTheBudgetIsWrittenOnce:
    """A loop bounded at one figure while the summary claims another reports a run nobody had."""

    @pytest.mark.parametrize("allowed", [1, 2, 4])
    def test_budget_changes_drive_the_loop_notice_and_summary(
        self,
        tmp_path: Path,
        retrying: Retrying,
        allowed: int,
    ):
        finished = _run(
            tmp_path,
            [retrying.check.MODEL_DID_NOT_CONVERGE] * allowed,
            retrying=retrying,
            allowed=allowed,
        )
        assert finished.attempts == allowed
        assert f"{allowed} allowed." in finished.summary
        warnings = [line for line in finished.stdout.splitlines() if line.startswith("::warning")]
        assert len(warnings) == allowed - 1
        for attempt, line in enumerate(warnings, 1):
            assert f"attempt {attempt} of {allowed}" in line

    def test_a_budget_of_one_is_the_retry_removed(self, retrying: Retrying):
        """That is #421 undone rather than tuned, and it would pass every test above."""
        assert _budget(retrying) >= 2

    def test_the_sample_readme_states_the_number_the_workflow_allows(self, retrying: Retrying):
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
            f"{retrying.readme.relative_to(_ROOT).as_posix()} does not say the sample is attempted "
            f"{words[allowed]} at most; the workflow allows {allowed} for {retrying.label}"
        )


class TestTheTwoFilesAgreeOnWhatIsRetryable:
    @_EACH
    def test_the_workflow_keys_on_the_status_the_check_returns(self, retrying: Retrying):
        """Renumbering `MODEL_DID_NOT_CONVERGE` would otherwise disable the retry in silence."""
        arguments = command_arguments(retry_step(retrying)["run"], "retry_live_sample.py", {})
        assert runner.PROFILES[arguments[0]].retryable == retrying.check.MODEL_DID_NOT_CONVERGE

    @_EACH
    def test_that_status_is_not_one_the_check_uses_for_anything_else(self, retrying: Retrying):
        assert retrying.check.MODEL_DID_NOT_CONVERGE not in (0, 1, 2)

    def test_no_other_live_sample_retries(self):
        """Only a step whose live model writes what the check grades has earned a loop."""
        assert _looping(yaml.safe_load(_WORKFLOW.read_text("utf-8"))) == _declared()

    def test_a_copied_loop_reusing_an_existing_step_name_is_still_caught(self):
        """Step names need not be unique, so a set would collapse the copy and pass."""
        workflow = yaml.safe_load(_WORKFLOW.read_text("utf-8"))
        workflow["jobs"]["invented"] = {"steps": [dict(retry_step(RETRYING[1]))]}
        assert _looping(workflow) != _declared()

    def test_each_retrying_step_is_a_distinct_step(self):
        """Two entries resolving to one step would leave a real one undriven."""
        names = [retry_step(r)["name"] for r in RETRYING]
        assert len(set(names)) == len(names), names


@_EACH
def test_the_entry_point_runs_the_samples_and_checkers_from_the_workflow(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    retrying: Retrying,
):
    arguments = command_arguments(retry_step(retrying)["run"], "retry_live_sample.py", {})
    output = tmp_path / "sample output café.txt"
    arguments[arguments.index("--output") + 1] = str(output)
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path / "summary.md"))
    samples, checks = [], []

    def sample(command, destination):
        samples.append((command, destination))
        return 0

    def check_command(command):
        checks.append(command)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(runner, "run_sample", sample)
    monkeypatch.setattr(runner.subprocess, "run", check_command)
    assert runner.main(arguments) == 0
    assert samples == [
        (
            [
                "uv",
                "run",
                "--no-project",
                str(retrying.readme.parent.relative_to(_ROOT) / "agent.py").replace("\\", "/"),
            ],
            output,
        )
    ]
    assert len(checks) == 1
    assert checks[0][0] == sys.executable
    assert retrying.check.__file__ is not None
    assert Path(checks[0][1]).name == Path(retrying.check.__file__).name
    assert checks[0][2:] == (["--docker"] if "docker" in retrying.label else []) + [str(output)]


@pytest.mark.parametrize("status", [0, 3, 7])
def test_sample_stdout_is_teed_and_its_native_status_is_preserved(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    status: int,
):
    script = tmp_path / "sample with spaces.py"
    script.write_text(
        f"import sys\nprint('café 🌻')\nprint('diagnostic', file=sys.stderr)\nsys.exit({status})\n",
        encoding="utf-8",
    )
    output = tmp_path / "output with spaces.txt"
    assert runner.run_sample([sys.executable, "-X", "utf8", str(script)], output) == status
    assert output.read_text("utf-8") == "café 🌻\n"
    assert capsys.readouterr().out.splitlines() == ["café 🌻"]


@pytest.mark.parametrize("phase", ["sample", "check"])
@pytest.mark.parametrize("status", [-15, -9])
def test_a_signal_failure_keeps_the_shell_exit_status(tmp_path: Path, phase: str, status: int):
    summary = tmp_path / "summary.md"
    result = runner.retry(
        "sample13",
        6,
        summary,
        lambda: status if phase == "sample" else 0,
        lambda: status if phase == "check" else pytest.fail("the checker must not run"),
    )
    assert result == 128 - status
    assert f"exit {128 - status} after 1 attempt(s)" in summary.read_text("utf-8")


@pytest.mark.parametrize("phase", ["sample", "check"])
def test_a_missing_command_fails_once_and_still_writes_the_summary(tmp_path: Path, phase: str):
    summary = tmp_path / "summary.md"

    def missing():
        raise FileNotFoundError("command is unavailable")

    result = runner.retry(
        "sample13", 6, summary, missing if phase == "sample" else lambda: 0, missing
    )
    assert result == 127
    assert "exit 127 after 1 attempt(s)" in summary.read_text("utf-8")


@pytest.mark.parametrize("payload", [b"sample\xff output\n", b"prefix\r  [measured] value\r\n"])
@pytest.mark.parametrize("status", [0, 7])
def test_sample_bytes_survive_tee_and_failure_reporting(
    tmp_path: Path,
    capsysbinary: pytest.CaptureFixture[bytes],
    payload: bytes,
    status: int,
):
    output, summary = tmp_path / "sample.bin", tmp_path / "summary.md"
    command = [
        sys.executable,
        "-c",
        f"import sys; sys.stdout.buffer.write({payload!r}); sys.exit({status})",
    ]

    def check_bytes():
        assert output.read_bytes() == payload
        return 0

    assert (
        runner.retry(
            "sample13",
            6,
            summary,
            lambda: runner.run_sample(command, output),
            check_bytes,
        )
        == status
    )
    assert output.read_bytes() == payload
    captured = capsysbinary.readouterr().out
    assert captured.startswith(payload)
    assert f"exit {status} after 1 attempt(s)" in summary.read_text("utf-8")
    if status:
        assert b"::error title=sample 13 did not run::" in captured


def test_new_profile_supplies_commands_and_reporting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
):
    monkeypatch.setitem(
        runner.PROFILES,
        "custom",
        runner.Profile(
            "custom sample",
            "custom_directory",
            "custom_checker.py",
            9,
            "custom action",
            " on custom",
            ("--custom",),
        ),
    )
    summary = tmp_path / "summary.md"
    output = tmp_path / "sample.bin"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    samples, checks = [], []

    def sample(command, destination):
        samples.append((command, destination))
        return 0

    def check_command(command):
        checks.append(command)
        return subprocess.CompletedProcess(command, 9 if len(checks) == 1 else 0)

    monkeypatch.setattr(runner, "run_sample", sample)
    monkeypatch.setattr(runner.subprocess, "run", check_command)
    assert runner.main(["custom", "--allowed", "2", "--output", str(output)]) == 0
    assert (
        samples
        == [(["uv", "run", "--no-project", "samples/custom_directory/agent.py"], output)] * 2
    )
    assert len(checks) == 2
    assert Path(checks[0][1]).name == "custom_checker.py"
    assert checks[0][2:] == ["--custom", str(output)]
    assert "custom action runs again" in capsys.readouterr().out
    assert "samples/custom_directory on custom: exit 0 after 2 attempt(s)" in summary.read_text(
        "utf-8"
    )


def test_invalid_budget_is_rejected_before_running_anything(tmp_path: Path):
    def unexpected():
        pytest.fail("invalid configuration must not execute a command")

    summary = tmp_path / "summary.md"
    with pytest.raises(ValueError, match="allowed must be positive"):
        runner.retry("sample13", 0, summary, unexpected, unexpected)
    with pytest.raises(SystemExit) as error:
        runner.main(["sample13", "--allowed", "0", "--output", str(tmp_path / "output")])
    assert error.value.code == 2
    assert not summary.exists()


def test_sample_bytes_follow_preceding_retry_messages(tmp_path: Path):
    driver = tmp_path / "driver.py"
    driver.write_text(
        "import sys\nfrom pathlib import Path\n"
        f"sys.path.insert(0, {str(_ROOT / 'scripts')!r})\n"
        "from retry_live_sample import run_sample\n"
        "print('retry annotation')\n"
        "run_sample([sys.executable, '-c', \"print('sample output')\"], Path('output.bin'))\n",
        encoding="utf-8",
    )
    result = subprocess.run([sys.executable, str(driver)], cwd=tmp_path, capture_output=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [b"retry annotation", b"sample output"]
