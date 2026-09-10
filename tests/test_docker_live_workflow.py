"""Keep live engine coverage independent of the PR gate and failures actionable."""

from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "report_docker_live_failure", ROOT / "scripts" / "report_docker_live_failure.py"
)
assert _spec and _spec.loader
report = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(report)


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
    assert job["timeout-minutes"] == 15
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


def test_failure_reporting_survives_live_job_setup_failure_or_timeout():
    live = workflow("docker-live.yml")
    assert live["permissions"] == {"contents": "read"}
    reporter = live["jobs"]["report-failure"]
    assert reporter["needs"] == "docker-e2e"
    assert reporter["if"] == "always() && needs.docker-e2e.result == 'failure'"
    assert reporter["permissions"] == {"contents": "read", "issues": "write"}
    step = reporter["steps"][-1]
    assert step["env"]["GH_TOKEN"] == "${{ github.token }}"
    assert step["run"] == "python3 scripts/report_docker_live_failure.py"


@pytest.fixture
def actions_env(monkeypatch):
    for key, value in {
        "GITHUB_REPOSITORY": "example/project",
        "GITHUB_SERVER_URL": "https://github.com",
        "GITHUB_RUN_ID": "123",
        "GITHUB_RUN_ATTEMPT": "2",
        "GITHUB_SHA": "abc123",
    }.items():
        monkeypatch.setenv(key, value)


@pytest.mark.usefixtures("actions_env")
@pytest.mark.parametrize("existing", [False, True])
def test_failure_opens_or_comments_on_tracker_across_all_pages(monkeypatch, existing):
    pages = [[{"number": 1, "body": None}, {"number": 2, "body": "unrelated"}]]
    pages[0].append({"number": 3, "body": report.MARKER, "pull_request": {}})
    pages.append([{"number": 4, "body": report.MARKER, "title": "Retitled"}] if existing else [])
    calls = []

    def run(args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(
            args, 0, stdout=json.dumps(pages) if len(calls) == 1 else "{}"
        )

    monkeypatch.setattr(report.subprocess, "run", run)
    report.main()
    assert len(calls) == 2
    assert calls[0][0] == [
        "gh",
        "api",
        "repos/example/project/issues?state=open&per_page=100",
        "--paginate",
        "--slurp",
    ]
    args, kwargs = calls[1]
    assert args == [
        "gh",
        "api",
        "repos/example/project/issues" + ("/4/comments" if existing else ""),
        "--input",
        "-",
    ]
    body = json.loads(kwargs["input"])
    assert "https://github.com/example/project/actions/runs/123/attempts/2" in body["body"]
    assert kwargs["check"] is True
    if not existing:
        assert body["title"] == report.TITLE
        assert report.MARKER in body["body"]
        assert "workspace commit abc123" in body["body"]
        assert "maf-sandbox-docker" in body["body"]


@pytest.mark.usefixtures("actions_env")
def test_api_failure_is_not_mistaken_for_no_open_tracker(monkeypatch):
    def run(args, **kwargs):
        raise subprocess.CalledProcessError(1, args)

    monkeypatch.setattr(report.subprocess, "run", run)
    with pytest.raises(subprocess.CalledProcessError):
        report.main()
