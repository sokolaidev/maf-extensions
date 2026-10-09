"""Recovery must cover every scan and must not dismiss newer failure reports."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import report_image_security_recovery as recovery  # noqa: E402
from select_image_security import PROFILES  # noqa: E402

pytestmark = pytest.mark.workflow


@pytest.fixture
def actions(monkeypatch):
    for key, value in {
        "GITHUB_REPOSITORY": "example/project",
        "GITHUB_SERVER_URL": "https://github.com",
        "GITHUB_RUN_ID": "123",
        "GITHUB_RUN_ATTEMPT": "2",
        "GITHUB_EVENT_NAME": "schedule",
        "GITHUB_REF": "refs/heads/main",
        "DEFAULT_BRANCH": "main",
        "SELECTED_PROFILE": "all",
    }.items():
        monkeypatch.setenv(key, value)
    jobs = [
        {
            "name": f"Image security ({profile})",
            "conclusion": "success",
            "steps": [{"name": "Scan the retained inventory", "conclusion": "success"}],
        }
        for profile in PROFILES
    ]
    jobs.extend(
        {"name": name, "conclusion": "success"}
        for name in ("Select image security profiles", "Image security checks")
    )
    tracker = {
        "number": 4,
        "body": recovery.MARKER,
        "user": {"login": "github-actions[bot]"},
        "updated_at": "2026-10-07T06:00:00Z",
    }
    calls = []
    state = {"jobs": jobs, "trackers": [tracker], "calls": calls, "head": "abc123"}

    def gh(*args, body=None):
        calls.append((args, body))
        if args[0].endswith("/attempts/2"):
            return json.dumps({"run_started_at": "2026-10-07T07:00:00Z", "head_sha": "abc123"})
        if "/commits/" in args[0]:
            return json.dumps({"sha": state["head"]})
        if "/jobs?" in args[0]:
            assert args[1:] == ("--paginate", "--slurp")
            return json.dumps([{"jobs": jobs[:7]}, {"jobs": jobs[7:]}])
        if "issues?" in args[0]:
            assert args[1:] == ("--paginate", "--slurp")
            return json.dumps([[{"number": 1, "body": None}], state["trackers"]])
        return "{}"

    monkeypatch.setattr(recovery, "gh", gh)
    return state


@pytest.mark.parametrize("event", ["schedule", "workflow_dispatch"])
def test_full_success_comments_then_closes_only_its_tracker(actions, monkeypatch, event):
    monkeypatch.setenv("GITHUB_EVENT_NAME", event)
    recovery.main()
    calls = actions["calls"]
    assert len(calls) == 6
    assert calls[0][0] == ("repos/example/project/actions/runs/123/attempts/2",)
    assert calls[1][0] == (
        "repos/example/project/actions/runs/123/attempts/2/jobs?per_page=100",
        "--paginate",
        "--slurp",
    )
    assert calls[-2][0] == ("repos/example/project/issues/4/comments",)
    assert "https://github.com/example/project/actions/runs/123/attempts/2" in calls[-2][1]["body"]
    assert "All 12 profiles" in calls[-2][1]["body"]
    assert calls[-1] == (
        ("repos/example/project/issues/4", "--method", "PATCH"),
        {"state": "closed", "state_reason": "completed"},
    )


@pytest.mark.parametrize(
    "key,value",
    [
        ("GITHUB_EVENT_NAME", "pull_request"),
        ("GITHUB_EVENT_NAME", "push"),
        ("GITHUB_REF", "refs/heads/feature"),
        ("GITHUB_REF", "refs/tags/main"),
        ("SELECTED_PROFILE", "bicep"),
    ],
)
def test_uncovered_run_never_calls_github(actions, monkeypatch, key, value):
    monkeypatch.setenv(key, value)
    recovery.main()
    assert not actions["calls"]


@pytest.mark.parametrize("name", [f"Image security ({p})" for p in PROFILES])
@pytest.mark.parametrize("result", ["failure", "cancelled", "skipped", None, "missing"])
def test_no_profile_can_be_missing_or_incomplete(actions, name, result):
    job = next(job for job in actions["jobs"] if job["name"] == name)
    if result == "missing":
        actions["jobs"].remove(job)
    else:
        job["conclusion"] = result
    recovery.main()
    assert len(actions["calls"]) == 2


@pytest.mark.parametrize(
    "steps", [[], [{"name": "Scan the retained inventory", "conclusion": "skipped"}]]
)
def test_green_hyperlight_deferral_does_not_close_tracker(actions, steps):
    job = next(job for job in actions["jobs"] if job["name"] == "Image security (hyperlight)")
    job["steps"] = steps
    recovery.main()
    assert len(actions["calls"]) == 2


@pytest.mark.parametrize(
    "replacement",
    [
        {"body": "unrelated"},
        {"pull_request": {}},
        {"user": {"login": "someone-else"}},
        {"updated_at": "2026-10-07T07:00:00Z"},
        {"updated_at": "2026-10-07T07:01:00Z"},
    ],
)
def test_unrelated_or_newer_tracker_is_untouched(actions, replacement):
    actions["trackers"][0].update(replacement)
    recovery.main()
    assert len(actions["calls"]) == 4


def test_no_open_tracker_is_a_noop(actions):
    actions["trackers"] = []
    recovery.main()
    assert len(actions["calls"]) == 4


@pytest.mark.parametrize("fail_on", [1, 2, 3, 4, 5, 6])
def test_api_errors_propagate_and_failed_comment_prevents_close(actions, monkeypatch, fail_on):
    original = recovery.gh
    calls = []

    def gh(*args, **kwargs):
        calls.append(args)
        if len(calls) == fail_on:
            raise subprocess.CalledProcessError(1, ["gh", "api", *args])
        return original(*args, **kwargs)

    monkeypatch.setattr(recovery, "gh", gh)
    with pytest.raises(subprocess.CalledProcessError):
        recovery.main()
    assert len(calls) == fail_on


def test_recovery_job_requires_full_default_branch_success_and_read_only_actions():
    workflow = yaml.safe_load((ROOT / ".github/workflows/image-security.yml").read_text())
    job = workflow["jobs"]["report-recovery"]
    assert job["needs"] == ["select", "scan", "check"]
    assert job["if"] == (
        "success() && needs.select.outputs.scan == 'true' && "
        "github.ref == format('refs/heads/{0}', github.event.repository.default_branch) && "
        "(github.event_name == 'schedule' || (github.event_name == 'workflow_dispatch' && inputs.profile == 'all'))"
    )
    assert job["permissions"] == {"contents": "read", "actions": "read", "issues": "write"}
    assert job["steps"][0]["with"]["persist-credentials"] is False
    assert job["steps"][-1]["run"] == "python3 scripts/report_image_security_recovery.py"
    assert job["steps"][-1]["env"] == {
        "GH_TOKEN": "${{ github.token }}",
        "DEFAULT_BRANCH": "${{ github.event.repository.default_branch }}",
        "SELECTED_PROFILE": "${{ inputs.profile || 'all' }}",
    }


@pytest.mark.parametrize("name", ["report-failure", "report-recovery"])
def test_failure_and_recovery_updates_are_serialized_and_queued(name):
    workflow = yaml.safe_load((ROOT / ".github/workflows/image-security.yml").read_text())
    assert workflow["jobs"][name]["concurrency"] == {
        "group": "image-security-tracker",
        "queue": "max",
        "cancel-in-progress": False,
    }


def test_old_commit_cannot_close_current_tracker(actions):
    actions["head"] = "newer123"
    recovery.main()
    assert len(actions["calls"]) == 3


@pytest.mark.parametrize("name", ["Select image security profiles", "Image security checks"])
def test_failed_gate_cannot_close_tracker(actions, name):
    next(job for job in actions["jobs"] if job["name"] == name)["conclusion"] = "failure"
    recovery.main()
    assert len(actions["calls"]) == 2
