"""Sample 18 joins published release verification and retains each draw.io call duration."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.workflow
ROOT = Path(__file__).resolve().parent.parent


def test_drawio_live_runs_for_releases_and_retains_failed_or_successful_output():
    workflow = yaml.safe_load((ROOT / ".github/workflows/verify-live.yml").read_text("utf-8"))
    job = workflow["jobs"]["sample-18"]
    assert "inputs.source" not in job["if"]
    assert job["needs"] == "acas-images"
    assert "inputs.package == ''" in job["if"]
    selected = re.search(r"fromJSON\('([^']+)'\)", job["if"])
    assert selected is not None
    assert set(json.loads(selected[1])) == {"maf-sandbox", "maf-sandbox-acas", "maf-sandbox-drawio"}
    assert workflow["concurrency"]["cancel-in-progress"] is False
    assert job["environment"] == "live-verify"
    assert job["env"]["DRAWIO_SANDBOX_IMAGE"] == "${{ vars.DRAWIO_SANDBOX_IMAGE }}"
    steps = job["steps"]
    run = next(step["run"] for step in steps if "uv run --no-project" in step.get("run", ""))
    assert "uv run --no-project $source_args samples/18_acas_drawio_repair/agent.py" in run
    assert "set -euo pipefail" in run
    assert '2>&1 | tee "$RUNNER_TEMP/drawio-live.log"' in run
    artifact = next(step for step in steps if "actions/upload-artifact@" in step.get("uses", ""))
    assert artifact["if"] == "always()"
    assert artifact["with"]["path"] == "${{ runner.temp }}/drawio-live.log"
    assert not job.get("continue-on-error", False)
    publish = yaml.safe_load((ROOT / ".github/workflows/publish-packages.yml").read_text("utf-8"))
    conditions = [
        publish["jobs"][name]["if"] for name in ("wait-for-propagation", "train-status", "verify")
    ]
    assert len(set(conditions)) == 1
    for condition in conditions:
        packages = re.search(r"fromJSON\('([^']+)'\)", condition)
        assert packages and "maf-sandbox-drawio" in json.loads(packages[1])
