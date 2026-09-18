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
