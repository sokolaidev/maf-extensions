"""Keep live engine coverage independent of the PR gate and failures actionable."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
pytestmark = pytest.mark.workflow


def workflow(name: str) -> dict:
    return yaml.safe_load((ROOT / ".github" / "workflows" / name).read_text(encoding="utf-8"))


def test_capability_qualification_cannot_pass_by_skipping_or_losing_pipeline_failures():
    job = workflow("docker-live.yml")["jobs"]["capability-policy"]
    assert job["if"] == "github.event_name != 'workflow_dispatch' || !inputs.openclaw_bicep"
    steps = {step.get("name"): step for step in job["steps"]}
    matrix = steps["Qualify every capability combination"]["run"]
    assert "set -euo pipefail" in matrix
    assert "test_docker_capability_e2e.py" in matrix
    assert "len(cases) == 96" in matrix
    assert 'case.find("skipped") is None' in matrix
    drawio = steps["Qualify editable creation and native export"]["run"]
    assert "set -euo pipefail" in drawio
    assert "check_drawio_docker.py" in drawio and "check_drawio_exports.py" in drawio
    assert steps["Retain capability qualification evidence"]["if"] == "always()"
    identity = steps["Record source and runtime identities"]["run"]
    assert '"GITHUB_SHA"' in identity and '"RepoDigests"' in identity
    assert '"version"' in identity


def test_live_checks_run_after_merge_daily_and_on_demand():
    live = workflow("docker-live.yml")
    triggers = live[True]  # PyYAML's YAML 1.1 loader reads `on` as True.
    assert set(triggers) == {"push", "schedule", "workflow_dispatch"}
    assert triggers["push"] == {"branches": ["main"]}
    assert triggers["schedule"] == [{"cron": "50 4 * * *"}]
    assert triggers["schedule"] != workflow("conformance-live.yml")[True]["schedule"]
    assert live["concurrency"] == {
        "group": "${{ github.event_name == 'workflow_dispatch' && inputs.openclaw_bicep && 'openclaw-bicep-live' || 'docker-live' }}",
        "cancel-in-progress": False,
    }
    assert "docker-e2e" not in workflow("tests.yml")["jobs"]
    job = live["jobs"]["docker-e2e"]
    assert job["timeout-minutes"] == 20
    assert "needs" not in job
    assert job["if"] == "github.event_name != 'workflow_dispatch' || !inputs.openclaw_bicep"
    assert all("if" not in step for step in job["steps"])
    suite = next(step["run"] for step in job["steps"] if step.get("name") == "Run the live suite")
    assert (
        "uv run pytest -q -ra \\\n  packages/maf-sandbox-docker/tests/test_docker_e2e.py \\\n  packages/maf-sandbox-codeact/tests/test_codeact_e2e.py"
        in suite
    )
    for image in ("NONROOT", "GUEST_OWNED", "LOOSE_PARENT", "NAMED_USER", "ABSENT_WORK"):
        variable = f"MAF_SANDBOX_DOCKER_E2E_{image}_IMAGE"
        assert variable in job["env"]
        assert f'docker build -t "${variable}" -' in suite


def test_process_cleanup_runs_on_an_image_of_each_launcher_path():
    """The launcher makes a session only where `setsid` is, and each path has an image here."""
    job = workflow("docker-live.yml")["jobs"]["docker-e2e"]
    step = next(
        s
        for s in job["steps"]
        if s.get("name") == "Measure process cleanup reach on both launcher paths"
    )
    assert "packages/maf-sandbox-docker/tests/test_docker_process_cleanup_e2e.py" in step["run"]
    # The pairing, not the image names, is what keeps both paths covered: a tag that gains or
    # loses `setsid` has to red the step rather than run one path twice.
    assert "command -v setsid" in step["run"]
    assert 'measure_cleanup_on "$MAF_SANDBOX_DOCKER_SESSION_IMAGE" session' in step["run"]
    assert 'measure_cleanup_on "$MAF_SANDBOX_CODEACT_E2E_IMAGE" none' in step["run"]
    # Both guests run the suite's Python program, so neither may be the backend suite's own
    # image. Neither `docker run` here reaches the registry: the session image is the digest an
    # earlier step pulls as its observer, and the CodeAct one is local from the first step.
    observers = [
        index
        for index, s in enumerate(job["steps"])
        if (s.get("env") or {}).get("MAF_SANDBOX_DOCKER_OBSERVER_IMAGE")
        == step["env"]["MAF_SANDBOX_DOCKER_SESSION_IMAGE"]
        and 'docker pull "$MAF_SANDBOX_DOCKER_OBSERVER_IMAGE"' in s.get("run", "")
    ]
    assert observers and min(observers) < job["steps"].index(step)
    assert "docker pull" not in step["run"]
    assert "python" in job["env"]["MAF_SANDBOX_CODEACT_E2E_IMAGE"]
    assert job["env"]["MAF_SANDBOX_DOCKER_E2E_IMAGE"] not in step["run"]


def test_openclaw_is_opt_in_and_only_uploads_bounded_evidence():
    live = workflow("docker-live.yml")
    assert live[True]["workflow_dispatch"]["inputs"]["openclaw_bicep"]["default"] is False
    for name in ("docker-e2e", "capability-policy", "document"):
        assert (
            live["jobs"][name]["if"]
            == "github.event_name != 'workflow_dispatch' || !inputs.openclaw_bicep"
        )
    job = live["jobs"]["openclaw-bicep"]
    assert job["if"] == (
        "github.event_name == 'workflow_dispatch' && inputs.openclaw_bicep "
        "&& inputs.openclaw_mode != 'model'"
    )
    assert "environment" not in job and "permissions" not in job
    assert job["runs-on"] == "ubuntu-24.04"
    assert "container" not in job
    steps = {step.get("name"): step for step in job["steps"]}
    assert "continue-on-error" not in steps["Run the installed sample"]
    upload = steps["Retain bounded Markdown evidence"]
    assert upload["if"] == "always()"
    assert upload["with"]["path"] == "${{ runner.temp }}/openclaw-bicep-evidence.md"
    assert steps["Record bounded evidence and independently check owner scope"]["if"] == "always()"


def test_failure_reporter_serializes_the_shared_tracker_without_locking_workloads():
    live = workflow("docker-live.yml")
    assert live["jobs"]["report-failure"]["concurrency"] == {
        "group": "docker-live-failure-tracker",
        "cancel-in-progress": False,
    }
    assert "docker-live-failure-tracker" not in live["concurrency"]["group"]
    for name in (
        "docker-e2e",
        "capability-policy",
        "document",
        "openclaw-bicep",
        "openclaw-bicep-model",
    ):
        assert "concurrency" not in live["jobs"][name]


def test_real_model_requires_explicit_dispatch_and_isolates_credentials():
    live = workflow("docker-live.yml")
    option = live[True]["workflow_dispatch"]["inputs"]["openclaw_mode"]
    assert option["default"] == "smoke" and option["options"] == ["smoke", "model"]
    job = live["jobs"]["openclaw-bicep-model"]
    assert job["if"] == (
        "github.event_name == 'workflow_dispatch' && inputs.openclaw_bicep "
        "&& inputs.openclaw_mode == 'model'"
    )
    assert job["environment"] == "live-verify"
    assert job["permissions"] == {"contents": "read", "id-token": "write"}
    assert job["runs-on"] == "ubuntu-24.04" and job["timeout-minutes"] == 25
    steps = {step.get("name"): step for step in job["steps"]}
    download = steps["Download verified published worker and prepared compiler"]["run"]
    assert "maf_sandbox_mcp-0.2.0-py3-none-any.whl" in download
    assert "ebb54bc8b51d4d0ec3e63f1f850c5aa53b55d80ec5567c679ead169a3ed0d545" in download
    assert "sha256sum --check --status" in download
    run = steps["Run the installed sample with Azure OpenAI"]
    assert run["timeout-minutes"] == 18
    assert "--mode model" in run["run"] and "::add-mask::" in run["run"]
    assert "--reasoning-effort none" in run["run"]
    assert "get-access-token --resource https://cognitiveservices.azure.com/" in run["run"]
    assert "$GITHUB_ENV" not in run["run"] and "set -x" not in run["run"]
    report = steps["Record bounded evidence and independently check owner scope"]
    assert report["if"] == "always()" and "--mode model" in report["run"]
    upload = steps["Retain bounded Markdown evidence"]
    assert upload["if"] == "always()"
    assert upload["with"]["path"] == "${{ runner.temp }}/openclaw-bicep-evidence.md"
    assert all("continue-on-error" not in step for step in job["steps"])
