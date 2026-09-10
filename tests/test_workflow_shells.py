"""Native PowerShell boundaries and Linux-only integration of production Bash commands."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import venv
from pathlib import Path

import pytest
import yaml
from _workflow_commands import (
    PUBLISH_WORKFLOW,
    RETRYING,
    ROOT,
    execute_release_step,
    isolated_retry_block,
    release_outputs,
    run_block,
    run_release,
)

pytestmark = pytest.mark.workflow

linux_bash = pytest.mark.skipif(
    sys.platform != "linux", reason="production Bash integration runs only on Linux"
)


def test_release_commands_receive_the_package_and_version_they_report():
    workflow = yaml.safe_load(PUBLISH_WORKFLOW.read_text("utf-8"))
    steps = [
        step
        for job in workflow["jobs"].values()
        for step in job.get("steps", [])
        if "release_workflow.py" in step.get("run", "")
    ]
    assert len(steps) == 5
    for step in steps:
        assert {"PACKAGE", "VERSION"} <= step["env"].keys(), step["name"]


@pytest.mark.parametrize("mode", ["build", "dispatch"])
@pytest.mark.parametrize(
    "verdict", ["", "live_check=unknown\n", "live_check=unknown\nlive_check=run\n"]
)
def test_missing_or_invalid_release_verdict_fails_closed(tmp_path: Path, mode: str, verdict: str):
    step = (
        "Verify the published dependents import against this core"
        if mode == "build"
        else "Decide the live-check dispatch after the upload"
    )
    result = execute_release_step(tmp_path, step, verdict)
    assert result.returncode == 1
    assert b"::error::" in result.stderr
    assert release_outputs(tmp_path) == ("", "")


@pytest.mark.parametrize("status", [1, 2, 7])
def test_dispatch_refusal_replays_stderr_and_preserves_status(tmp_path: Path, status: int):
    result = execute_release_step(
        tmp_path,
        "Decide the live-check dispatch after the upload",
        "live_check=run\n",
        stderr="::error::index unavailable — café\n",
        status=status,
    )
    assert result.returncode == status
    assert "index unavailable — café" in result.stderr.decode("utf-8")
    assert release_outputs(tmp_path) == ("", "")


@pytest.mark.parametrize("mode", ["breaking", "build", "pre-upload", "dispatch"])
@pytest.mark.parametrize("status", [0, 7])
@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_non_utf8_checker_diagnostics_preserve_release_decisions(
    tmp_path: Path,
    mode: str,
    status: int,
    stream: str,
):
    answer = b"breaking=true\n" if mode == "breaking" else b"live_check=run\n"
    diagnostic = b"native diagnostic: \xff\n"
    stdout = diagnostic + answer if stream == "stdout" else answer
    stderr = diagnostic if stream == "stderr" else b""
    program = (
        f"import sys; sys.stdout.buffer.write({stdout!r}); "
        f"sys.stderr.buffer.write({stderr!r}); sys.exit({status})"
    )
    result = run_release(
        tmp_path, [mode, "maf-sandbox", "1.2.3", "--", sys.executable, "-c", program]
    )
    assert result.returncode == (0 if mode == "breaking" else status), result.stderr
    assert b"Traceback" not in result.stderr
    output, summary = release_outputs(tmp_path)
    if mode == "breaking":
        assert output == f"breaking={str(status == 0 and stream == 'stderr').lower()}\n"
        if status:
            assert b"::warning::" in result.stdout
    elif mode == "dispatch" and status == 0:
        assert output == "live_check=run\n"
        if stderr:
            assert "::error::native diagnostic: �" in result.stdout.decode("utf-8")
            assert "native diagnostic: �" in summary
    else:
        assert output == ""
    if mode != "breaking" or stream == "stderr":
        assert "native diagnostic: �" in (result.stdout + result.stderr).decode("utf-8")


@pytest.fixture(scope="module")
def native_python(tmp_path_factory: pytest.TempPathFactory) -> Path:
    directory = tmp_path_factory.mktemp("native interpreter with spaces") / "python café"
    venv.EnvBuilder(with_pip=False).create(directory)
    return directory / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


@pytest.mark.parametrize("status", [0, 7])
def test_powershell_preserves_paths_arguments_utf8_and_native_failures(
    tmp_path: Path,
    native_python: Path,
    status: int,
):
    pwsh = shutil.which("pwsh")
    if pwsh is None:
        if os.name == "nt":
            pytest.fail("Windows workflow checks require PowerShell 7 (pwsh)")
        pytest.skip("PowerShell is not installed on this Linux host")
    checkout = tmp_path / "checkout with spaces café"
    scripts = checkout / "scripts"
    scripts.mkdir(parents=True)
    wrapper = scripts / "check_workflows.ps1"
    shutil.copyfile(ROOT / "scripts/check_workflows.ps1", wrapper)
    module = tmp_path / "pytest.py"
    module.write_text(
        "import json, os, sys\n"
        "sys.stdout.buffer.write(json.dumps({'args': sys.argv[1:], 'cwd': os.getcwd()}, ensure_ascii=False).encode('utf-8'))\n"
        f"raise SystemExit({status})\n",
        encoding="utf-8",
    )
    arguments = ['a "quoted" value', "café 🌻", "$(not-a-command)", "x&y|z", "path with spaces/"]
    # A literal argument array exercises PowerShell's native-command binding, not shell evaluation.
    quoted = ", ".join("'" + arg.replace("'", "''") + "'" for arg in arguments)
    command = (
        "$PSNativeCommandUseErrorActionPreference = $true\n"
        "& $env:TEST_WRAPPER -Python $env:TEST_PYTHON -TestArgs @(" + quoted + ")\n"
        "exit $LASTEXITCODE\n"
    )
    result = subprocess.run(
        [pwsh, "-NoProfile", "-NonInteractive", "-Command", command],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=os.environ
        | {
            "TEST_WRAPPER": str(wrapper),
            "TEST_PYTHON": str(native_python),
            "PYTHONPATH": str(tmp_path),
            "PYTHONUTF8": "0",
        },
    )
    assert result.returncode == status, result.stderr
    output = json.loads(result.stdout)
    assert output["args"] == [
        "tests",
        "-m",
        "workflow",
        *arguments,
    ]
    assert Path(output["cwd"]) == checkout


@linux_bash
@pytest.mark.parametrize(
    ("step", "stdout", "status", "expected"),
    [
        ("Check whether this release is breaking", "breaking=true\n", 0, 0),
        ("Check whether this release is breaking", "", 7, 0),
        ("Verify the published dependents import against this core", "live_check=skip\n", 0, 0),
        ("Verify the published dependents import against this core", "", 7, 7),
        ("Re-verify only newly admitting published versions import against this core", "", 7, 7),
        ("Decide the live-check dispatch after the upload", "live_check=run\n", 0, 0),
        ("Decide the live-check dispatch after the upload", "", 7, 7),
    ],
)
def test_linux_executes_the_production_release_bash_command(
    tmp_path: Path,
    step: str,
    stdout: str,
    status: int,
    expected: int,
):
    assert shutil.which("bash"), "Linux CI must provide Bash for production integration checks"
    for folder in ("scripts", "release-guard/scripts"):
        directory = tmp_path / folder
        directory.mkdir(parents=True)
        shutil.copyfile(ROOT / "scripts/release_workflow.py", directory / "release_workflow.py")
        for name in ("check_release_is_breaking.py", "check_published_dependents_work.py"):
            (directory / name).write_text(
                f"import sys\nsys.stdout.write({stdout!r})\nsys.exit({status})\n",
                encoding="utf-8",
            )
    result = subprocess.run(
        [
            "bash",
            "--noprofile",
            "--norc",
            "-eo",
            "pipefail",
            "-c",
            run_block(PUBLISH_WORKFLOW, step),
        ],
        cwd=tmp_path,
        capture_output=True,
        env=os.environ
        | {
            "PACKAGE": "maf-sandbox",
            "VERSION": "0.13.0",
            "GITHUB_OUTPUT": str(tmp_path / "output with spaces.txt"),
            "GITHUB_STEP_SUMMARY": str(tmp_path / "summary with spaces.md"),
        },
    )
    assert result.returncode == expected, result.stderr


@linux_bash
@pytest.mark.parametrize("retrying", RETRYING, ids=lambda r: r.label)
@pytest.mark.parametrize("sample_status", [0, 3, 7])
def test_linux_executes_the_production_retry_bash_command(
    tmp_path: Path, retrying, sample_status: int
):
    assert shutil.which("bash"), "Linux CI must provide Bash for production integration checks"
    harness = tmp_path / "harness with spaces"
    scripts = harness / "scripts"
    scripts.mkdir(parents=True)
    shutil.copyfile(ROOT / "scripts/retry_live_sample.py", scripts / "retry_live_sample.py")
    tally = tmp_path / "attempts"
    tally.write_text("")
    for name in ("check_live_fix_loop_sample.py", "check_live_host_tools_call_sample.py"):
        (scripts / name).write_text(
            "MODEL_DID_NOT_CONVERGE = 3\n"
            "if __name__ == '__main__':\n"
            "    import os\n    from pathlib import Path\n"
            "    raise SystemExit(3 if len(Path(os.environ['TALLY']).read_text()) == 1 else 0)\n",
            encoding="utf-8",
        )
    binaries = tmp_path / "bin"
    binaries.mkdir()
    stub = binaries / "uv"
    stub.write_text(
        f'#!/bin/sh\nprintf x >> "$TALLY"\nprintf "sample output\\n"\nexit {sample_status}\n'
    )
    stub.chmod(0o755)
    summary = tmp_path / "summary.md"
    output = tmp_path / "sample output.bin"
    result = subprocess.run(
        [
            "bash",
            "--noprofile",
            "--norc",
            "-eo",
            "pipefail",
            "-c",
            isolated_retry_block(retrying, output),
        ],
        cwd=tmp_path,
        capture_output=True,
        env=os.environ
        | {
            "PATH": str(binaries) + os.pathsep + os.environ["PATH"],
            "HARNESS": str(harness),
            "TALLY": str(tally),
            "GITHUB_STEP_SUMMARY": str(summary),
        },
    )
    assert result.returncode == sample_status, result.stderr
    attempts = 2 if sample_status == 0 else 1
    assert len(tally.read_text()) == attempts
    assert output.read_bytes() == b"sample output\n"
    assert f"exit {sample_status} after {attempts} attempt(s)" in summary.read_text("utf-8")
