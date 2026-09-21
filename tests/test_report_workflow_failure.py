"""Require scheduled failure reporting and exercise each workflow's actual reporter arguments."""

from __future__ import annotations

import importlib.util
import json
import shlex
import subprocess
import tomllib
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "report_workflow_failure", ROOT / "scripts" / "report_workflow_failure.py"
)
assert _spec and _spec.loader
report = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(report)
pytestmark = pytest.mark.workflow


def scheduled_workflows() -> dict[str, dict]:
    workflows = {}
    for path in sorted((ROOT / ".github" / "workflows").iterdir()):
        if path.suffix not in {".yml", ".yaml"}:
            continue
        workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
        triggers = workflow.get("on", workflow.get(True, {}))
        if "schedule" in triggers:
            workflows[path.name] = workflow
    return workflows


SCHEDULED = scheduled_workflows()


def reporter_arguments(name: str) -> list[str]:
    command = SCHEDULED[name]["jobs"]["report-failure"]["steps"][-1]["run"]
    argv = shlex.split(command)
    assert argv[:2] == ["python3", "scripts/report_workflow_failure.py"]
    return argv[2:]


@pytest.mark.parametrize("name", SCHEDULED)
def test_every_scheduled_workflow_reports_even_setup_failure_or_timeout(name):
    workflow = SCHEDULED[name]
    assert workflow["permissions"] == {"contents": "read"}
    reporter = workflow["jobs"]["report-failure"]
    sources = reporter["needs"]
    if isinstance(sources, str):
        sources = [sources]
    assert set(workflow["jobs"]) == {*sources, "report-failure"}
    condition = " || ".join(f"needs.{source}.result == 'failure'" for source in sources)
    if len(sources) > 1:
        condition = f"({condition})"
    assert reporter["if"] == f"always() && {condition}"
    assert reporter["permissions"] == {"contents": "read", "issues": "write"}
    assert all(
        workflow["jobs"][source].get("permissions", {}).get("issues") is None for source in sources
    )
    assert "environment" not in reporter
    assert reporter["runs-on"] == "ubuntu-latest"
    assert reporter["timeout-minutes"] == 5
    assert all("if" not in step for step in reporter["steps"])
    checkout = reporter["steps"][0]
    assert checkout["uses"] == "actions/checkout@11d5960a326750d5838078e36cf38b85af677262"
    assert checkout["with"]["persist-credentials"] is False
    assert reporter["steps"][-1]["env"]["GH_TOKEN"] == "${{ github.token }}"
    reporter_arguments(name)


def test_opentofu_platform_build_is_opt_in_and_reports_failures():
    workflow = SCHEDULED["terraform-live.yml"]
    triggers = workflow.get("on", workflow.get(True, {}))
    option = triggers["workflow_dispatch"]["inputs"]["platform"]
    assert option["type"] == "boolean" and option["default"] is False
    job = workflow["jobs"]["opentofu-platform"]
    assert job["if"] == "github.event_name == 'workflow_dispatch' && inputs.platform"
    assert "opentofu-platform" in workflow["jobs"]["report-failure"]["needs"]
    commands = "\n".join(step.get("run", "") for step in job["steps"])
    assert "dependencies.opentofu-platform.json --check" in commands
    assert "MANIFEST=dependencies.opentofu-platform.json" in commands
    assert "pytest -q tests/test_opentofu_platform_offline.py" in commands
    assert job["env"]["MAF_OPENTOFU_PLATFORM_IMAGE"]


def test_workflow_trackers_are_distinct_and_keep_the_existing_docker_marker():
    markers = [
        args[args.index("--marker") + 1] for name in SCHEDULED if (args := reporter_arguments(name))
    ]
    assert len(markers) == len(set(markers)) == len(SCHEDULED)
    docker = reporter_arguments("docker-live.yml")
    assert docker[docker.index("--marker") + 1] == "<!-- docker-live-failure-tracker -->"


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
@pytest.mark.parametrize("name", SCHEDULED)
@pytest.mark.parametrize("existing", [False, True])
def test_failure_opens_or_comments_on_its_own_tracker_across_all_pages(monkeypatch, name, existing):
    argv = reporter_arguments(name)
    marker = argv[argv.index("--marker") + 1]
    pages = [[{"number": 1, "body": None}, {"number": 2, "body": "unrelated"}]]
    pages[0].append({"number": 3, "body": marker, "pull_request": {}})
    for other in SCHEDULED:
        if other != name:
            other_args = reporter_arguments(other)
            pages[0].append({"number": 5, "body": other_args[other_args.index("--marker") + 1]})
    pages.append([{"number": 4, "body": marker, "title": "Retitled"}] if existing else [])
    calls = []

    def run(args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(
            args, 0, stdout=json.dumps(pages) if len(calls) == 1 else "{}"
        )

    monkeypatch.setattr(report.subprocess, "run", run)
    report.main(argv)
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
    assert argv[argv.index("--title") + 1] in body["body"]
    if name == "cleanup-live.yml":
        for guidance in (
            "over 24 hours",
            "unreclaimed",
            "sweep log",
            "failures",
            "inventory is unknown",
            "dry run",
        ):
            assert guidance in body["body"]
    if not existing:
        if name == "terraform-live.yml":
            assert "optional OpenTofu platform image (when platform=true)" in body["body"]
        assert body["title"] == argv[argv.index("--title") + 1]
        assert marker in body["body"]
        assert argv[argv.index("--reproduce") + 1] in body["body"]
        assert argv[argv.index("--expected") + 1] in body["body"]
        assert "workspace commit abc123" in body["body"]
        expected_packages = {
            "bicep-catalog.yml": {"maf-sandbox-bicep"},
            "docker-live.yml": {
                "maf-sandbox",
                "maf-sandbox-docker",
                "maf-sandbox-codeact",
                "maf-sandbox-bicep",
                "maf-sandbox-deepagents",
            },
            "conformance-live.yml": {"maf-sandbox", "maf-sandbox-acas"},
            "cleanup-live.yml": {"maf-sandbox-acas"},
            "lock-drift.yml": {"maf-sandbox-bicep", "maf-sandbox-codeact"},
            "terraform-live.yml": {"maf-sandbox", "maf-sandbox-docker", "maf-sandbox-terraform"},
        }[name]
        for metadata in (ROOT / "packages").glob("*/pyproject.toml"):
            project = tomllib.loads(metadata.read_text(encoding="utf-8"))["project"]
            assert (f"{project['name']} {project['version']}" in body["body"]) == (
                project["name"] in expected_packages
            )


@pytest.mark.usefixtures("actions_env")
@pytest.mark.parametrize("fail_on", [1, 2])
def test_api_failure_is_propagated_without_opening_a_second_tracker(monkeypatch, fail_on):
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        if len(calls) == fail_on:
            raise subprocess.CalledProcessError(1, args)
        return subprocess.CompletedProcess(args, 0, stdout="[[]]")

    monkeypatch.setattr(report.subprocess, "run", run)
    with pytest.raises(subprocess.CalledProcessError):
        report.main(reporter_arguments("docker-live.yml"))
    assert len(calls) == fail_on
