"""New live coverage and release dispatch must select the same exercised packages."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.workflow
ROOT = Path(__file__).resolve().parents[1]


def selected(condition):
    match = re.search(r"fromJSON\('([^']+)'\)", condition)
    assert match
    return set(json.loads(match[1]))


def test_release_filters_include_otel_shell_and_compaction_pair():
    workflow = yaml.safe_load((ROOT / ".github/workflows/publish-packages.yml").read_text("utf-8"))
    filters = [
        selected(workflow["jobs"][job]["if"])
        for job in ("wait-for-propagation", "train-status", "verify")
    ]
    assert filters[0] == filters[1] == filters[2]
    assert {"maf-sandbox-otel", "maf-sandbox-shell", "maf-compaction", "maf-cachebench"} <= filters[
        0
    ]
    assert (
        not {
            "maf-sandbox-bubblewrap",
            "maf-sandbox-mcp",
            "maf-sandbox-tui",
            "maf-sandbox-hyperlight",
        }
        & filters[0]
    )


@pytest.mark.parametrize(
    "job,packages,checker",
    [
        (
            "sample-21",
            {"maf-sandbox", "maf-sandbox-docker", "maf-sandbox-shell"},
            "check_live_shell_sample.py",
        ),
        ("sample-22", {"maf-compaction", "maf-cachebench"}, "check_live_compaction_sample.py"),
    ],
)
def test_new_jobs_are_bounded_and_check_measured_output(job, packages, checker):
    workflow = yaml.safe_load((ROOT / ".github/workflows/verify-live.yml").read_text("utf-8"))
    definition = workflow["jobs"][job]
    assert selected(definition["if"]) == packages
    assert "inputs.package == ''" in definition["if"]
    assert definition["timeout-minutes"] <= 25
    assert definition["environment"] == "live-verify"
    steps = definition["steps"]
    run = next(step["run"] for step in steps if checker in step.get("run", ""))
    assert "set -euo pipefail" in run and "| tee" in run
    assert '"$HARNESS"/scripts/' + checker in run
    assert any(
        step.get("if") == "always()" and "upload-artifact@" in step.get("uses", "")
        for step in steps
    )
