"""Hosted WSLC qualification must execute work and preserve failures."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.workflow
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import check_wslc_e2e  # noqa: E402
import qualify_wslc  # noqa: E402

WORKFLOW = yaml.safe_load((ROOT / ".github/workflows/verify-live.yml").read_text("utf-8"))


@pytest.mark.parametrize("failure", [None, "exec", "retained", "unreadable"])
def test_probe_checks_transferred_bytes_and_removal(monkeypatch, failure):
    calls = []
    marker = ""
    name = ""

    def command(*args):
        nonlocal marker, name
        calls.append(args)
        if args[:2] == ("container", "run"):
            name = args[4]
        if args[:2] == ("container", "cp"):
            assert args[3] == f"{name}:/tmp/"
            assert Path(args[2]).name == "probe.txt"
            marker = Path(args[2]).read_text("utf-8")
        if args[:2] == ("container", "exec"):
            assert args[-1] == "/tmp/probe.txt"
            return "wrong contents" if failure == "exec" else marker
        if args[:2] == ("container", "list"):
            if failure == "retained":
                return json.dumps({"Names": name})
            if failure == "unreadable":
                return "null"
        return ""

    monkeypatch.setattr(qualify_wslc, "command", command)
    if failure:
        with pytest.raises(RuntimeError):
            qualify_wslc.qualify("test-image")
    else:
        qualify_wslc.qualify("test-image")
    assert ("container", "remove", "-f", name) in calls
    assert "--network" in calls[0] and "none" in calls[0]


@pytest.mark.parametrize("state", ["passed", "skipped", "failure", "missing"])
def test_junit_requires_actual_passes(tmp_path, state):
    suite = ET.Element("testsuite")
    if state != "missing":
        for name in check_wslc_e2e.REQUIRED:
            case = ET.SubElement(suite, "testcase", name=name)
            if state != "passed":
                ET.SubElement(case, state)
    path = tmp_path / "junit.xml"
    ET.ElementTree(suite).write(path)
    assert bool(check_wslc_e2e.assess(path)) is (state != "passed")


def test_sample_jobs_use_windows_and_keep_published_and_branch_modes():
    for number, kind in (("02", "bicep"), ("04", "codeact")):
        job = WORKFLOW["jobs"][f"sample-{number}"]
        assert job["runs-on"] == "windows-2025"
        assert "maf-sandbox-wslc" in job["if"] and f"maf-sandbox-{kind}" in job["if"]
        assert job["environment"] == "live-verify"
        assert job["permissions"]["id-token"] == "write"
        steps = job["steps"]
        runs = "\n".join(step.get("run", "") for step in steps)
        assert 'python "$HARNESS"/scripts/qualify_wslc.py' in runs
        assert "uv run --no-project $source_args samples/" in runs
        assert "uv sync" not in runs
        for step in steps:
            if "sample_source_args.py" in step.get("run", ""):
                assert "set -euo pipefail" in step["run"]
                assert "2>&1 | tee" in step["run"]
        upload = next(step for step in steps if "actions/upload-artifact@" in step.get("uses", ""))
        assert upload["if"] == "always()"


@pytest.mark.skipif(shutil.which("pwsh") is None, reason="PowerShell is unavailable")
def test_native_pytest_failure_survives_powershell_tee(tmp_path):
    steps = WORKFLOW["jobs"]["wslc-e2e"]["steps"]
    run = next(step["run"] for step in steps if step.get("name") == "Run the live backend suite")
    stub = """
function uv {
    if ($args[1] -eq 'pytest') {
        & $env:TEST_PYTHON -c "import sys; print('pytest failed'); sys.exit(17)"
    } else {
        & $env:TEST_PYTHON -c "pass"
    }
}
function python { throw 'The report checker ran after pytest failed' }
"""
    result = subprocess.run(
        ["pwsh", "-NoProfile", "-Command", stub + run],
        env={**os.environ, "TEST_PYTHON": sys.executable},
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 17, result.stdout + result.stderr
    assert "pytest failed" in (tmp_path / "wslc-e2e.txt").read_text("utf-8")
