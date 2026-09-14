"""The source-only ACAS sample retains call timings without changing release verification."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.workflow
ROOT = Path(__file__).resolve().parent.parent


def test_drawio_live_uses_the_checkout_and_retains_failed_or_successful_output():
    workflow = yaml.safe_load((ROOT / ".github/workflows/verify-live.yml").read_text("utf-8"))
    job = workflow["jobs"]["acas-drawio"]
    assert "inputs.source == 'branch'" in job["if"]
    assert "inputs.package == ''" in job["if"]
    selected = re.search(r"fromJSON\('([^']+)'\)", job["if"])
    assert selected is not None
    assert set(json.loads(selected[1])) == {"maf-sandbox", "maf-sandbox-acas", "maf-sandbox-drawio"}
    assert workflow["concurrency"]["cancel-in-progress"] is False
    assert job["environment"] == "live-verify"
    assert job["env"]["MAF_ACAS_DRAWIO_LIVE"] == "1"
    assert job["env"]["DRAWIO_SANDBOX_IMAGE"] == "${{ vars.DRAWIO_SANDBOX_IMAGE }}"
    steps = job["steps"]
    run = next(step["run"] for step in steps if "pytest" in step.get("run", ""))
    assert "--locked pytest -q tests/test_sample_acas_drawio_live.py" in run
    assert "set -euo pipefail" in run
    assert '2>&1 | tee "$RUNNER_TEMP/drawio-live.log"' in run
    artifact = next(step for step in steps if "actions/upload-artifact@" in step.get("uses", ""))
    assert artifact["if"] == "always()"
    assert artifact["with"]["path"] == "${{ runner.temp }}/drawio-live.log"
    assert not job.get("continue-on-error", False)
