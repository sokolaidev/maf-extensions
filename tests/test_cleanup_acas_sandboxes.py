"""Cleanup requires a continuous stopped interval, never just an old creation time."""

from __future__ import annotations

import importlib.util
import json
import sys
from contextlib import nullcontext
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml
from azure.containerapps.sandbox import Sandbox, SandboxStateDetails
from azure.core.exceptions import ResourceNotFoundError

_ROOT = Path(__file__).resolve().parent.parent
_SPEC = importlib.util.spec_from_file_location(
    "cleanup_acas_sandboxes", _ROOT / "scripts" / "cleanup_acas_sandboxes.py"
)
assert _SPEC and _SPEC.loader
cleanup = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = cleanup
_SPEC.loader.exec_module(cleanup)

_NOW = datetime(2026, 9, 8, 12, tzinfo=UTC)
_OLD = Sandbox(
    id="old",
    created_at="2026-09-01T00:00:00Z",
    state="Stopped",
    state_details=SandboxStateDetails(stopped_at="2026-09-07T11:59:59Z"),
)
_FRESH = replace(
    _OLD, id="fresh", state_details=SandboxStateDetails(stopped_at="2026-09-08T10:00:00Z")
)


class _Group:
    def __init__(self, *sandboxes):
        self.inventory = sandboxes
        self.current = {sandbox.id: sandbox for sandbox in sandboxes}
        self.list_error: Exception | None = None
        self.delete_errors = {}
        self.requested = []

    def list_sandboxes(self):
        yield from self.inventory
        if self.list_error:
            raise self.list_error

    def get_sandbox(self, sandbox_id):
        if sandbox_id not in self.current:
            raise ResourceNotFoundError("gone")
        return self.current[sandbox_id]

    def begin_delete_sandbox(self, sandbox_id, **kwargs):
        self.requested.append(sandbox_id)
        group = self

        class Poller:
            def result(self):
                if sandbox_id in group.delete_errors:
                    raise group.delete_errors[sandbox_id]
                del group.current[sandbox_id]

        return Poller()


def test_stopped_duration_is_strict_and_timezone_aware():
    boundary = replace(
        _OLD,
        id="boundary",
        state_details=SandboxStateDetails(stopped_at="2026-09-07T14:00:00+02:00"),
    )
    fractional = replace(
        _OLD,
        id="fractional",
        state_details=SandboxStateDetails(stopped_at="2026-09-07T13:59:59.9999999+02:00"),
    )
    future = replace(
        _OLD, id="future", state_details=SandboxStateDetails(stopped_at="2026-09-09T00:00:00Z")
    )
    group = _Group(_OLD, _FRESH, boundary, fractional, future)

    result = cleanup.cleanup(group, now=_NOW, apply=True)

    assert result.deleted == ["old", "fractional"]
    assert result.failures == []
    assert set(group.current) == {"fresh", "boundary", "future"}


@pytest.mark.parametrize(
    "state", ["Running", "Suspended", "Resuming", "Stopping", "Creating", "Deleting", None]
)
@pytest.mark.parametrize("details", [None, _OLD.state_details])
def test_only_stopped_state_qualifies_even_with_an_old_stop_timestamp(state, details):
    group = _Group(replace(_OLD, state=state, state_details=details))
    result = cleanup.cleanup(group, now=_NOW, apply=True)
    assert result.eligible == result.failures == group.requested == []


def test_creation_time_is_not_required_and_labels_do_not_limit_the_group():
    group = _Group(replace(_OLD, created_at=None, labels={"arbitrary": "label"}))
    result = cleanup.cleanup(group, now=_NOW, apply=True)
    assert result.deleted == ["old"]
    assert result.failures == []


def test_preview_has_no_deletion_requests():
    group = _Group(_OLD)
    result = cleanup.cleanup(group, now=_NOW)
    assert result.dry_run
    assert result.eligible == ["old"]
    assert result.deleted == group.requested == []


def test_invalid_policy_or_clock_is_refused():
    with pytest.raises(ValueError):
        cleanup.cleanup(_Group(), now=_NOW, stopped_for=timedelta(0))
    with pytest.raises(ValueError):
        cleanup.cleanup(_Group(), now=_NOW.replace(tzinfo=None))


def test_failure_after_an_inventory_page_prevents_every_delete():
    group = _Group(_OLD)
    group.list_error = RuntimeError("second page unavailable")
    result = cleanup.cleanup(group, now=_NOW, apply=True)
    assert "second page unavailable" in result.failures[0]
    assert group.requested == []


@pytest.mark.parametrize(
    "details",
    [
        None,
        SandboxStateDetails(),
        SandboxStateDetails(stopped_at="yesterday"),
        SandboxStateDetails(stopped_at="2026-09-07T10:00:00"),
    ],
)
def test_unknown_stop_time_is_retained_and_reported_without_blocking_valid_candidates(details):
    group = _Group(_OLD, replace(_OLD, id="unknown", state_details=details))
    result = cleanup.cleanup(group, now=_NOW, apply=True)
    assert result.failures
    assert result.deleted == ["old"]
    assert "unknown" in group.current


@pytest.mark.parametrize("invalid", [replace(_OLD, id=""), _OLD])
def test_missing_or_duplicate_ids_prevent_every_delete(invalid):
    group = _Group(_OLD, invalid)
    result = cleanup.cleanup(group, now=_NOW, apply=True)
    assert "no deletions" in result.failures[0]
    assert group.requested == []


@pytest.mark.parametrize(
    "replacement",
    [
        replace(_OLD, state="Running", state_details=None),
        replace(_OLD, state="Resuming"),
        replace(_OLD, state_details=_FRESH.state_details),
    ],
)
def test_a_resumed_or_re_stopped_candidate_is_retained(replacement):
    group = _Group(_OLD)
    group.current["old"] = replacement
    result = cleanup.cleanup(group, now=_NOW, apply=True)
    assert result.retained == ["old"]
    assert result.failures == group.requested == []


def test_a_changed_identity_is_not_deleted():
    group = _Group(_OLD)
    group.current["old"] = replace(_OLD, id="different")
    result = cleanup.cleanup(group, now=_NOW, apply=True)
    assert "changed since inventory" in result.failures[0]
    assert group.requested == []


def test_a_candidate_that_loses_its_stop_timestamp_is_not_deleted():
    group = _Group(_OLD)
    group.current["old"] = replace(_OLD, state_details=None)
    result = cleanup.cleanup(group, now=_NOW, apply=True)
    assert "missing stateDetails.stoppedAt" in result.failures[0]
    assert group.requested == []


@pytest.mark.parametrize("during_delete", [False, True])
def test_concurrent_removal_is_success(during_delete):
    group = _Group(_OLD)
    if during_delete:
        group.delete_errors["old"] = ResourceNotFoundError("gone")
    else:
        del group.current["old"]
    result = cleanup.cleanup(group, now=_NOW, apply=True)
    assert result.already_absent == ["old"]
    assert result.deleted == result.failures == []


def test_a_failed_deletion_does_not_block_others_and_can_be_retried():
    second = replace(_OLD, id="second")
    group = _Group(_OLD, second)
    group.delete_errors["old"] = TimeoutError("deletion not confirmed")
    result = cleanup.cleanup(group, now=_NOW, apply=True)
    assert result.deleted == ["second"]
    assert "deletion not confirmed" in result.failures[0]
    assert "old" in group.current

    group.delete_errors.clear()
    group.inventory = tuple(group.current.values())
    retried = cleanup.cleanup(group, now=_NOW, apply=True)
    assert retried.deleted == ["old"]
    assert retried.failures == []


@pytest.mark.parametrize("hours", ["0", "-1", "nan", "inf", "1e100", "bad"])
def test_invalid_duration_is_refused_before_authentication(hours, monkeypatch):
    def refuse_auth():
        pytest.fail("invalid policy attempted authentication")

    monkeypatch.setattr(cleanup, "AzureCliCredential", refuse_auth)
    with pytest.raises(SystemExit) as exc:
        cleanup.main(["--stopped-for-hours", hours, "--apply"])
    assert exc.value.code == 2


def test_missing_group_configuration_is_refused(monkeypatch):
    for variable in cleanup._CONFIG.values():
        monkeypatch.delenv(variable, raising=False)
    with pytest.raises(SystemExit) as exc:
        cleanup.main(["--apply"])
    assert exc.value.code == 2


@pytest.mark.parametrize("apply", [False, True])
def test_cli_forwards_the_group_and_policy_and_reports_failure(
    apply, monkeypatch, tmp_path, capsys
):
    for key, variable in cleanup._CONFIG.items():
        monkeypatch.setenv(variable, key)
    group = _Group()
    group.list_error = RuntimeError("unavailable")
    received = {}

    def client(**kwargs):
        received.update(kwargs)
        return nullcontext(group)

    monkeypatch.setattr(cleanup, "AzureCliCredential", lambda: nullcontext(object()))
    monkeypatch.setattr(cleanup, "SandboxGroupClient", client)
    sweep = cleanup.cleanup

    def record_policy(client, **kwargs):
        received.update(kwargs)
        return sweep(client, **kwargs)

    monkeypatch.setattr(cleanup, "cleanup", record_policy)
    summary = tmp_path / "summary.md"
    args = ["--summary", str(summary), "--stopped-for-hours", "48"]
    if apply:
        args.append("--apply")
    assert cleanup.main(args) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["dry_run"] is not apply
    assert "unavailable" in report["failures"][0]
    assert received["sandbox_group"] == "sandbox_group"
    assert received["resource_group"] == "resource_group"
    assert received["stopped_for"] == timedelta(hours=48)
    assert received["apply"] is apply
    assert "Failures" in summary.read_text("utf-8")


def test_scheduled_workflow_is_scoped_and_manual_runs_preview():
    workflow = yaml.safe_load((_ROOT / ".github/workflows/cleanup-live.yml").read_text("utf-8"))
    triggers = workflow.get("on", workflow.get(True))
    assert set(triggers) == {"schedule", "workflow_dispatch"}
    assert triggers["workflow_dispatch"]["inputs"]["dry_run"]["default"] is True
    assert workflow["concurrency"]["cancel-in-progress"] is False
    job = workflow["jobs"]["acas-cleanup"]
    assert job["environment"] == "live-verify"
    assert job["permissions"] == {"contents": "read", "id-token": "write"}
    assert set(job["env"]) == set(cleanup._CONFIG.values())
    sweep = job["steps"][-1]
    assert sweep["env"]["DRY_RUN"] == (
        "${{ github.event_name == 'workflow_dispatch' && inputs.dry_run || false }}"
    )
    assert "--stopped-for-hours 24" in sweep["run"]
    assert 'if [ "$DRY_RUN" != true ]' in sweep["run"]
    assert "args+=(--apply)" in sweep["run"]
