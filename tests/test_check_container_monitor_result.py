"""Exercise the final monitor gate's visible diagnostics without GitHub access."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.workflow


def run_gate(tmp_path, details, **changes):
    workflow = yaml.safe_load(
        (ROOT / ".github/workflows/container-image-monitor.yml").read_text(encoding="utf-8")
    )
    step = workflow["jobs"]["result"]["steps"][-1]
    assert step["env"]["MONITOR_NOTIFICATION"] == "${{ needs.finish.outputs.notification }}"
    for name in ("BEGIN", "SCAN", "FINISH"):
        assert step["env"][name] == "${{ needs." + name.lower() + ".result }}"
    assert step["env"]["ATTENTION"] == "${{ needs.finish.outputs.attention }}"
    summary = tmp_path / "summary.md"
    summary.write_text("Existing summary\n", encoding="utf-8")
    result = subprocess.run(
        [sys.executable, *step["run"].split()[1:]],
        cwd=ROOT,
        env=os.environ
        | {
            "PYTHONUTF8": "1",
            "BEGIN": "success",
            "SCAN": "success",
            "FINISH": "success",
            "ATTENTION": "false",
            "MONITOR_NOTIFICATION": json.dumps({"summary": details}),
            "GITHUB_STEP_SUMMARY": str(summary),
        }
        | changes,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    return result, summary.read_text(encoding="utf-8")


@pytest.mark.parametrize("scan", ["success", "skipped"])
def test_healthy_current_images_allow_historical_warnings(tmp_path, scan):
    details = "Current images tracked: 13. Historical warnings: 35."
    result, summary = run_gate(tmp_path, details, SCAN=scan)
    assert result.returncode == 0, result.stderr
    assert details in result.stdout and details in summary
    assert "passed the gate" in summary
    assert "::error::" not in result.stdout


def test_findings_and_recovery_actions_are_visible_in_the_failing_job(tmp_path):
    details = (
        "Affected images and reasons:\n\n"
        "- egress-proxy/0.1.2: High GHSA-example; sha256:" + "a" * 64 + ".\n"
        "- graphviz/0.2.0: assessment unavailable; delivery pending."
    )
    result, summary = run_gate(tmp_path, details, ATTENTION="true")
    assert result.returncode == 1
    assert details in result.stdout and details in summary
    assert summary.startswith("Existing summary\n")
    assert "::error::Current images or monitoring delivery need attention." in result.stdout
    assert "qualify a new image version" in summary
    assert "re-run all monitor jobs" in summary
    assert "a monitor retry cannot deliver it" in summary
    assert "#responding-to-monitor-findings" in summary


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"BEGIN": "failure"}, "begin job"),
        ({"SCAN": "cancelled"}, "scan job"),
        ({"FINISH": "failure"}, "finish job"),
        ({"ATTENTION": ""}, "valid health result"),
        ({"MONITOR_NOTIFICATION": ""}, "readable assessment summary"),
        ({"MONITOR_NOTIFICATION": "not JSON"}, "readable assessment summary"),
        ({"MONITOR_NOTIFICATION": "null"}, "readable assessment summary"),
        ({"MONITOR_NOTIFICATION": '{"summary": []}'}, "readable assessment summary"),
    ],
)
def test_missing_evidence_and_job_failures_have_explicit_errors(tmp_path, changes, message):
    result, summary = run_gate(tmp_path, "Assessment completed.", **changes)
    assert result.returncode == 1
    assert any(message in line for line in result.stdout.splitlines() if "::error::" in line)
    assert message in summary


def test_assessment_text_cannot_emit_workflow_commands(tmp_path):
    details = "::error::embedded finding\n::add-mask::value"
    result, summary = run_gate(tmp_path, details)
    assert result.returncode == 0
    stop = result.stdout.splitlines()[0]
    assert stop.startswith("::stop-commands::")
    resume = "::" + stop.removeprefix("::stop-commands::") + "::"
    assert result.stdout.index(stop) < result.stdout.index(details) < result.stdout.index(resume)
    assert details in summary
