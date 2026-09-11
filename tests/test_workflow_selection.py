"""Workflow test discovery and the conditions that schedule its CI matrix."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from _workflow_commands import RETRYING, ROOT, command_arguments, isolated_retry_block, run_block

pytestmark = pytest.mark.workflow


def test_workflow_marker_discovers_a_new_module(tmp_path: Path):
    pwsh = shutil.which("pwsh")
    if pwsh is None:
        if sys.platform == "win32":
            pytest.fail("native Windows workflow checks require PowerShell")
        pytest.skip("PowerShell is unavailable")
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    wrapper = scripts / "check_workflows.ps1"
    shutil.copyfile(ROOT / "scripts/check_workflows.ps1", wrapper)
    tests = tmp_path / "tests"
    tests.mkdir()
    (tmp_path / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\nmarkers = ["workflow: workflow checks"]\n', encoding="utf-8"
    )
    (tests / "test_new_workflow.py").write_text(
        "import pytest\nfrom pathlib import Path\npytestmark = pytest.mark.workflow\n"
        'def test_new():\n    Path("discovered").touch()\n',
        encoding="utf-8",
    )
    (tests / "test_unrelated.py").write_text(
        "def test_unrelated():\n    assert False\n", encoding="utf-8"
    )
    result = subprocess.run(
        [pwsh, "-NoProfile", "-NonInteractive", "-File", str(wrapper), "-Python", sys.executable],
        capture_output=True,
        # The byte match below needs plain output, whatever colour the caller's environment
        # forces: `PY_COLORS` outranks `NO_COLOR` in pytest, and `FORCE_COLOR` loses to both.
        env=os.environ | {"PYTHONUTF8": "0", "NO_COLOR": "1", "PY_COLORS": "0"},
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert (tmp_path / "discovered").exists()
    assert b"1 passed, 1 deselected" in result.stdout


@pytest.mark.parametrize("retrying", RETRYING, ids=lambda r: r.label)
def test_retry_log_is_isolated_even_when_its_path_needs_quoting(tmp_path: Path, retrying):
    output = tmp_path / "log with spaces.bin"
    arguments = command_arguments(
        isolated_retry_block(retrying, output), "retry_live_sample.py", {}
    )
    assert arguments[arguments.index("--output") + 1] == str(output)


def test_portable_jobs_guard_dependency_setup_and_test_execution():
    workflow = yaml.safe_load((ROOT / ".github/workflows/workflow-tests.yml").read_text("utf-8"))
    portable = workflow["jobs"]["portable"]
    assert portable["needs"] == "changes"
    expensive = [step for step in portable["steps"] if "checkout@" not in step.get("uses", "")]
    assert expensive
    assert all(step.get("if") == "needs.changes.outputs.code == 'true'" for step in expensive)


@pytest.mark.skipif(sys.platform != "linux", reason="production classifier Bash runs on Linux")
@pytest.mark.parametrize(
    ("paths", "event", "expected"),
    [
        ("docs/guide.md", "pull_request", "false"),
        ("scripts/templates/range-body.md", "pull_request", "true"),
        ("", "pull_request", "true"),
        ("docs/guide.md", "push", "true"),
    ],
)
def test_production_classifier_schedules_only_relevant_changes(
    tmp_path: Path, paths: str, event: str, expected: str
):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    shutil.copyfile(ROOT / "scripts/changed_paths.py", scripts / "changed_paths.py")
    binaries = tmp_path / "bin"
    binaries.mkdir()
    git = binaries / "git"
    git.write_text('#!/bin/sh\nprintf "%s\\n" "$CHANGED_PATHS"\n', encoding="utf-8")
    git.chmod(0o755)
    output = tmp_path / "output"
    result = subprocess.run(
        [
            "bash",
            "-eo",
            "pipefail",
            "-c",
            run_block(ROOT / ".github/workflows/workflow-tests.yml", "Classify the changed paths"),
        ],
        cwd=tmp_path,
        capture_output=True,
        env=os.environ
        | {
            "PATH": str(binaries) + os.pathsep + os.environ["PATH"],
            "CHANGED_PATHS": paths,
            "EVENT_NAME": event,
            "BASE_SHA": "base",
            "GITHUB_OUTPUT": str(output),
        },
    )
    assert result.returncode == 0, result.stderr
    assert output.read_text() == f"code={expected}\n"
