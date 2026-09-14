"""Sample 18 joins published release verification and retains each draw.io call duration."""

from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.workflow
ROOT = Path(__file__).resolve().parent.parent


def _required_variables():
    tree = ast.parse((ROOT / "samples/18_acas_drawio_repair/agent.py").read_text("utf-8"))
    variables = {"AZURE_CLIENT_ID", "AZURE_TENANT_ID", "AZURE_SUBSCRIPTION_ID"}
    for statement in tree.body:
        if isinstance(statement, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id in {"SANDBOX_VARS", "MODEL_VARS"}
            for target in statement.targets
        ):
            variables.update(ast.literal_eval(statement.value))
    return variables


def _preflight():
    workflow = yaml.safe_load((ROOT / ".github/workflows/verify-live.yml").read_text("utf-8"))
    job = workflow["jobs"]["sample-18"]
    step = next(
        step
        for step in job["steps"]
        if step.get("name") == "Check the live configuration is present"
    )
    return job, step


def test_preflight_covers_runtime_and_oidc_configuration_before_login():
    job, step = _preflight()
    login = next(step for step in job["steps"] if "azure/login@" in step.get("uses", ""))
    assert job["steps"].index(step) < job["steps"].index(login)
    assert "if" not in step and "continue-on-error" not in step
    loop = re.search(r"for name in (.*?); do", step["run"], re.DOTALL)
    assert loop
    assert set(loop[1].replace("\\", "").split()) == _required_variables()
    assert _required_variables() <= (job["env"] | step["env"]).keys()


@pytest.mark.skipif(
    sys.platform != "linux", reason="production Bash integration runs only on Linux"
)
@pytest.mark.parametrize("missing", [None, *sorted(_required_variables())])
def test_preflight_reports_each_missing_name_without_values(missing):
    _, step = _preflight()
    env = {"PATH": os.environ["PATH"]} | dict.fromkeys(_required_variables(), "private-test-value")
    if missing is not None:
        env[missing] = ""
    result = subprocess.run(
        ["bash", "--noprofile", "--norc", "-c", step["run"]],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == (0 if missing is None else 1)
    assert "private-test-value" not in result.stdout + result.stderr
    if missing is not None:
        assert missing in result.stdout and "::error::" in result.stdout


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
