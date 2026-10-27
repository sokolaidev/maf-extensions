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
    reporters = {"report-failure"}
    if name == "image-security.yml":
        reporters.add("report-recovery")
    assert set(workflow["jobs"]) == {*sources, *reporters}
    condition = " || ".join(f"needs.{source}.result == 'failure'" for source in sources)
    if len(sources) > 1:
        condition = f"({condition})"
    if name == "image-security.yml":
        condition += " && github.event_name != 'pull_request'"
    if name == "container-image-monitor.yml":
        assert (
            reporter["if"]
            == "always() && needs.begin.result != 'skipped' && needs.begin.result != 'cancelled'"
        )
        env = reporter["steps"][-1]["env"]
        assert env["MONITOR_NOTIFICATION"] == "${{ needs.finish.outputs.notification }}"
        for source in sources:
            assert f"needs.{source}.result" in env["MONITOR_JOB_RESULTS"]
        assert (
            workflow["jobs"]["finish"]["outputs"]["notification"]
            == "${{ steps.finish.outputs.notification }}"
        )
    else:
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
    assert checkout["uses"].startswith("actions/checkout@")
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
        if "/comments?" in args[2]:
            return subprocess.CompletedProcess(args, 0, stdout="[[]]")
        return subprocess.CompletedProcess(
            args, 0, stdout=json.dumps(pages) if len(calls) == 1 else "{}"
        )

    monkeypatch.setattr(report.subprocess, "run", run)
    report.main(argv)
    assert len(calls) == (3 if existing and name == "container-image-monitor.yml" else 2)
    assert calls[0][0] == [
        "gh",
        "api",
        "repos/example/project/issues?state=open&per_page=100",
        "--paginate",
        "--slurp",
    ]
    args, kwargs = calls[-1]
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
        if name == "image-security.yml":
            assert "automatically closes this issue" in body["body"]
            assert "does not automatically close" not in body["body"]
        else:
            assert "does not automatically close" in body["body"]
        expected_packages = {
            "bicep-catalog.yml": {"maf-sandbox-bicep"},
            "docker-live.yml": {
                "maf-sandbox-mcp",
                "maf-sandbox",
                "maf-sandbox-docker",
                "maf-sandbox-codeact",
                "maf-sandbox-bicep",
                "maf-sandbox-deepagents",
                "maf-sandbox-document",
                "maf-sandbox-drawio",
            },
            "conformance-live.yml": {"maf-sandbox", "maf-sandbox-acas"},
            "cleanup-live.yml": {"maf-sandbox-acas"},
            "sbx-live.yml": {"maf-sandbox", "maf-sandbox-docker-sbx"},
            "lock-drift.yml": {"maf-sandbox-bicep", "maf-sandbox-codeact"},
            "terraform-live.yml": {"maf-sandbox", "maf-sandbox-docker", "maf-sandbox-terraform"},
            "image-security.yml": {
                "maf-sandbox",
                "maf-sandbox-docker",
                "maf-sandbox-bicep",
                "maf-sandbox-terraform",
                "maf-sandbox-document",
                "maf-sandbox-drawio",
                "maf-sandbox-hyperlight",
            },
            "container-image-monitor.yml": {
                "maf-sandbox",
                "maf-sandbox-docker",
                "maf-sandbox-bicep",
                "maf-sandbox-terraform",
                "maf-sandbox-document",
                "maf-sandbox-drawio",
                "maf-sandbox-hyperlight",
            },
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


def monitor_state(monkeypatch, *, attention=True, problems="findings", **jobs):
    monkeypatch.setenv(
        "MONITOR_NOTIFICATION",
        json.dumps(
            {
                "attention": attention,
                "problems": problems,
                "summary": "Current images assessed: 11. High CVE-example in bicep/0.1.0."
                if attention
                else "Current images assessed: 11. Historical warnings: 9.",
            }
        ),
    )
    monkeypatch.setenv(
        "MONITOR_JOB_RESULTS",
        json.dumps(
            {
                "begin": "success",
                "scan": "success",
                "finish": "success",
                "result": "failure" if attention else "success",
                **jobs,
            }
        ),
    )


@pytest.mark.usefixtures("actions_env")
def test_monitor_reports_changes_recovery_and_recurrence_but_never_reopens(monkeypatch):
    argv = reporter_arguments("container-image-monitor.yml")
    marker = argv[argv.index("--marker") + 1]
    issues = [{"number": 10, "state": "closed", "body": marker}]
    comments = {10: []}
    writes = []

    def api(endpoint, *args, body=None):
        if body is None:
            if "/comments?" in endpoint:
                number = int(endpoint.split("/")[-2])
                return json.dumps([[], comments[number]])
            assert "?state=open&" in endpoint
            # Include a closed result to exercise the reporter's own state check.
            return json.dumps([issues])
        writes.append((endpoint, body))
        assert "--method" not in args
        if endpoint.endswith("/comments"):
            number = int(endpoint.split("/")[-2])
            assert next(issue for issue in issues if issue["number"] == number)["state"] == "open"
            comments[number].append(body | {"user": {"login": "github-actions[bot]"}})
        else:
            number = 10 + len(issues)
            issues.append(body | {"number": number, "state": "open"})
            comments[number] = []
        return "{}"

    monkeypatch.setattr(report, "gh", api)
    monitor_state(monkeypatch)
    report.main(argv)
    assert len(issues) == 2 and len(writes) == 1
    assert "High CVE-example" in issues[-1]["body"]
    assert "actions/runs/123/attempts/2" in issues[-1]["body"]
    monkeypatch.setenv("GITHUB_RUN_ID", "124")
    report.main(argv)
    assert len(writes) == 1

    monitor_state(monkeypatch, problems="different findings")
    report.main(argv)
    assert len(writes) == 2
    monitor_state(monkeypatch, attention=False, problems="healthy")
    report.main(argv)
    assert len(writes) == 3 and "Monitoring recovered" in writes[-1][1]["body"]
    report.main(argv)
    assert len(writes) == 3
    assert issues[-1]["state"] == "open"

    monitor_state(monkeypatch, problems="different findings")
    report.main(argv)
    assert len(writes) == 4 and "Monitoring recovered" not in writes[-1][1]["body"]
    issues[-1]["state"] = "closed"
    report.main(argv)
    assert len(issues) == 3 and len(writes) == 5
    assert issues[0]["state"] == issues[1]["state"] == "closed"
    assert comments[10] == []


@pytest.mark.usefixtures("actions_env")
def test_monitor_recovery_without_an_open_tracker_creates_nothing(monkeypatch):
    monitor_state(monkeypatch, attention=False, problems="healthy")
    calls = []

    def api(*args, **kwargs):
        calls.append((args, kwargs))
        return "[[]]"

    monkeypatch.setattr(report, "gh", api)
    report.main(reporter_arguments("container-image-monitor.yml"))
    assert len(calls) == 1 and not calls[0][1]


@pytest.mark.parametrize("stage", ["begin", "scan", "finish", "result"])
@pytest.mark.parametrize("conclusion", ["failure", "cancelled", "skipped", "unknown"])
def test_monitor_never_reports_recovery_from_incomplete_stages(monkeypatch, stage, conclusion):
    monitor_state(monkeypatch, attention=False, **{stage: conclusion})
    details, _, recovered = report.monitor_notification()
    assert recovered is (stage == "scan" and conclusion == "skipped")
    if not recovered:
        assert f"{stage}={conclusion}" in details


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "{",
        "null",
        "[]",
        "{}",
        '{"attention": false}',
        '{"attention":"false","problems":"x","summary":"x"}',
    ],
)
def test_monitor_missing_or_invalid_health_cannot_claim_recovery(monkeypatch, raw):
    monitor_state(monkeypatch, attention=False)
    monkeypatch.setenv("MONITOR_NOTIFICATION", raw)
    details, _, recovered = report.monitor_notification()
    assert not recovered and "recovery is unverified" in details


def test_monitor_dedup_ignores_human_markers_and_reads_all_comment_pages(monkeypatch):
    marker = report.notification_marker("current")
    previous = report.notification_marker("previous")
    pages = [
        [{"body": marker, "user": {"login": "github-actions[bot]"}}],
        [
            {"body": previous, "user": {"login": "github-actions[bot]"}},
            {"body": marker, "user": {"login": "human"}},
        ],
    ]
    monkeypatch.setattr(report, "gh", lambda *a: json.dumps(pages))
    assert not report.unchanged({"number": 1, "body": marker}, "issues", marker)
    assert report.unchanged({"number": 1, "body": marker}, "issues", previous)
