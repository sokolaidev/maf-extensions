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
SETUP = yaml.safe_load((ROOT / ".github/actions/setup-wslc/action.yml").read_text("utf-8"))


@pytest.mark.skipif(shutil.which("pwsh") is None, reason="PowerShell is unavailable")
@pytest.mark.parametrize(
    ("initial", "probe_exit", "install_exit", "installed", "wsl_exit", "bad_hash", "expected"),
    [
        ("wslc 3.0.3.0", 0, 0, "wslc 3.0.3.0", 0, False, "reuse"),
        ("wslc 3.0.3.0", 0, 0, "wslc 3.0.3.0", 19, False, "wsl_failure"),
        ("missing", 0, 0, "wslc 3.0.3.0", 0, False, "install"),
        ("wslc 3.0.1.0", 0, 0, "wslc 3.0.3.0", 0, False, "install"),
        ("wslc 3.0.4.0", 0, 0, "wslc 3.0.3.0", 0, False, "install"),
        ("wslc 3.0.3.0", 17, 0, "wslc 3.0.3.0", 0, False, "install"),
        ("unrecognized", 0, 0, "wslc 3.0.3.0", 0, False, "install"),
        ("missing", 0, 1603, "wslc 3.0.3.0", 0, False, "installer_failure"),
        ("missing", 0, 3010, "wslc 3.0.3.0", 0, False, "installer_failure"),
        ("missing", 0, 0, "wslc 3.0.1.0", 0, False, "wrong_version"),
        ("missing", 0, 0, "wslc 3.0.3.0", 0, True, "hash_failure"),
    ],
)
def test_setup_reuses_qualified_engine_and_preserves_failures(
    tmp_path, initial, probe_exit, install_exit, installed, wsl_exit, bad_hash, expected
):
    runner_temp = tmp_path / "runner temp"
    runner_temp.mkdir()
    stub = """
$env:ProgramFiles = $env:TEST_PROGRAM_FILES
$script:installed = $false
function Get-Command {
    param($Name, $ErrorAction)
    if ($Name -ne 'wslc') { throw "Unexpected command: $Name" }
    if ($script:installed -or $env:TEST_INITIAL -ne 'missing') { return 'wslc' }
}
function wslc {
    if ($args[0] -ne '--version') { throw 'Unexpected WSLC operation' }
    if ($script:installed) {
        $global:LASTEXITCODE = 0
        return $env:TEST_INSTALLED
    }
    $global:LASTEXITCODE = [int]$env:TEST_PROBE_EXIT
    return $env:TEST_INITIAL
}
function wsl {
    'wsl' | Add-Content events.txt
    $global:LASTEXITCODE = [int]$env:TEST_WSL_EXIT
}
function Invoke-WebRequest {
    param($Uri, $OutFile)
    'download' | Add-Content events.txt
    if ($Uri -ne 'https://github.com/microsoft/WSL/releases/download/3.0.3/wsl.3.0.3.0.x64.msi') {
        throw 'Unexpected installer URL'
    }
    'installer' | Set-Content -LiteralPath $OutFile
}
function Get-FileHash {
    param($LiteralPath, $Algorithm)
    if ($Algorithm -ne 'SHA256' -or !(Test-Path -LiteralPath $LiteralPath)) {
        throw 'Installer was not downloaded for verification'
    }
    if ($env:TEST_BAD_HASH -eq 'True') { return @{Hash = 'bad'} }
    return @{Hash = 'ae977286974255bd26bf925f00f40f5692c6b2084155cbc0563c09aea091f211'}
}
function Start-Process {
    param($FilePath, $ArgumentList, [switch]$Wait, [switch]$PassThru, $WindowStyle)
    'install' | Add-Content events.txt
    if ($FilePath -ne 'msiexec.exe' -or !$Wait -or !$PassThru -or $WindowStyle -ne 'Hidden') {
        throw 'Installer must finish before verification'
    }
    ConvertTo-Json -InputObject $ArgumentList | Set-Content installer-args.json
    $logIndex = [array]::IndexOf($ArgumentList, '/L*v')
    if ($logIndex -lt 0) { throw 'Missing MSI logging' }
    $logPath = $ArgumentList[$logIndex + 1].Trim('"')
    'MSI diagnostic evidence' | Set-Content -LiteralPath $logPath
    $script:installed = $true
    return @{ExitCode = [int]$env:TEST_INSTALL_EXIT}
}
"""
    result = subprocess.run(
        ["pwsh", "-NoProfile", "-Command", stub + SETUP["runs"]["steps"][0]["run"]],
        env={
            **os.environ,
            "TEST_PROGRAM_FILES": str(tmp_path / "Program Files"),
            "RUNNER_TEMP": str(runner_temp),
            "GITHUB_PATH": str(tmp_path / "github-path.txt"),
            "TEST_INITIAL": initial,
            "TEST_PROBE_EXIT": str(probe_exit),
            "TEST_INSTALL_EXIT": str(install_exit),
            "TEST_INSTALLED": installed,
            "TEST_WSL_EXIT": str(wsl_exit),
            "TEST_BAD_HASH": str(bad_hash),
        },
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
    )
    output = result.stdout + result.stderr
    events = (tmp_path / "events.txt").read_text("utf-8").splitlines()
    if expected in {"reuse", "install"}:
        assert result.returncode == 0, output
        assert events == (["wsl"] if expected == "reuse" else ["download", "install", "wsl"])
        assert "wslc 3.0.3.0" in output
    else:
        assert result.returncode != 0, output
        if expected == "wsl_failure":
            assert result.returncode == 19
            assert events == ["wsl"]
        elif expected == "installer_failure":
            assert events == ["download", "install"]
            assert f"exit {install_exit}" in output
        elif expected == "wrong_version":
            assert "Expected wslc 3.0.3.0" in output
        elif expected == "hash_failure":
            assert events == ["download"]
            assert "SHA-256 mismatch" in output
    if "install" in events:
        args = json.loads((tmp_path / "installer-args.json").read_text("utf-8"))
        assert args == [
            "/i",
            f'"{runner_temp / "wsl.3.0.3.0.x64.msi"}"',
            "/qn",
            "/norestart",
            "/L*v",
            f'"{runner_temp / "wsl-install.log"}"',
        ]
        assert "MSI diagnostic evidence" in (runner_temp / "wsl-install.log").read_text("utf-8")
    assert str(tmp_path / "Program Files" / "WSL") in (tmp_path / "github-path.txt").read_text(
        "utf-8"
    )


def test_setup_retains_installer_log_on_failure():
    upload = SETUP["runs"]["steps"][-1]
    assert upload["uses"].startswith("actions/upload-artifact@")
    assert upload["if"] == "always()"
    assert upload["with"]["path"] == "${{ runner.temp }}/wsl-install.log"
    assert upload["with"]["name"] == "wsl-install-${{ github.job }}"
    assert upload["with"]["if-no-files-found"] == "ignore"


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
