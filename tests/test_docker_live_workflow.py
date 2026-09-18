"""Keep live engine coverage independent of the PR gate and failures actionable."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
pytestmark = pytest.mark.workflow


def workflow(name: str) -> dict:
    return yaml.safe_load((ROOT / ".github" / "workflows" / name).read_text(encoding="utf-8"))


def test_live_checks_run_after_merge_daily_and_on_demand():
    live = workflow("docker-live.yml")
    triggers = live[True]  # PyYAML's YAML 1.1 loader reads `on` as True.
    assert set(triggers) == {"push", "schedule", "workflow_dispatch"}
    assert triggers["push"] == {"branches": ["main"]}
    assert triggers["schedule"] == [{"cron": "50 4 * * *"}]
    assert triggers["schedule"] != workflow("conformance-live.yml")[True]["schedule"]
    assert live["concurrency"] == {"group": "docker-live", "cancel-in-progress": False}
    assert "docker-e2e" not in workflow("tests.yml")["jobs"]
    job = live["jobs"]["docker-e2e"]
    assert job["timeout-minutes"] == 20
    assert "needs" not in job
    assert "if" not in job
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
    assert '"$MAF_SANDBOX_DOCKER_SESSION_IMAGE" "$MAF_SANDBOX_CODEACT_E2E_IMAGE"' in step["run"]
    # Both guests run the suite's Python program, so neither may be the backend suite's own
    # image; the session one is the digest the fingerprint steps already pull, so it costs
    # this job no extra registry round trip.
    observers = {
        s["env"]["MAF_SANDBOX_DOCKER_OBSERVER_IMAGE"]
        for s in job["steps"]
        if "MAF_SANDBOX_DOCKER_OBSERVER_IMAGE" in (s.get("env") or {})
    }
    assert observers
    assert step["env"]["MAF_SANDBOX_DOCKER_SESSION_IMAGE"] in observers
    assert "python" in job["env"]["MAF_SANDBOX_CODEACT_E2E_IMAGE"]
    assert job["env"]["MAF_SANDBOX_DOCKER_E2E_IMAGE"] not in step["run"]
