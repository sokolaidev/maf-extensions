"""Offline tests for the ACAS missing-lifecycle recovery script."""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from azure.core.exceptions import ResourceNotFoundError

_ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location(
    "recover_lifecycle_policy", _ROOT / "scripts" / "recover_lifecycle_policy.py"
)
assert _SPEC and _SPEC.loader
recovery = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = recovery
_SPEC.loader.exec_module(recovery)

_NOW = datetime(2026, 9, 8, 12, tzinfo=UTC)
_OLD = "2026-09-01T00:00:00Z"
_FRESH = "2026-09-08T11:55:00Z"
_STOPPED_OLD = "2026-09-07T11:59:59Z"
_LABELS = {"scope": "scope-a", "thread": "thread-1", "agent": "devops", "kind": "bicep"}
_CONFIGURED = {"autoDeletePolicy": {"enabled": True, "deleteIntervalSeconds": 600}}
_MISSING = {"autoSuspendPolicy": {"enabled": True, "interval": 300, "mode": "Memory"}}


class _Pager:
    def __init__(self, items):
        self._items = items

    def __aiter__(self):
        async def _gen():
            for item in self._items:
                yield item

        return _gen()


def _sandbox(
    sandbox_id: str,
    *,
    labels: dict[str, str] | None = None,
    created_at: str = _OLD,
    state: str = "Running",
    stopped_at: str | None = None,
    lifecycle_policy=None,
):
    return SimpleNamespace(
        id=sandbox_id,
        labels=dict(_LABELS if labels is None else labels),
        created_at=created_at,
        state=state,
        state_details=SimpleNamespace(stopped_at=stopped_at) if stopped_at is not None else None,
        lifecycle_policy=_MISSING if lifecycle_policy is None else lifecycle_policy,
    )


def _replace_namespace(source: SimpleNamespace, **changes):
    values = vars(source).copy()
    values.update(changes)
    return SimpleNamespace(**values)


class _SandboxClient:
    def __init__(self, owner: _Group, sandbox_id: str) -> None:
        self._owner = owner
        self._sandbox_id = sandbox_id

    async def set_lifecycle_policy(self, policy) -> None:
        self._owner.installed.append((self._sandbox_id, policy))
        if self._sandbox_id in self._owner.set_errors:
            raise self._owner.set_errors[self._sandbox_id]
        current = self._owner.current[self._sandbox_id]
        self._owner.current[self._sandbox_id] = _replace_namespace(
            current, lifecycle_policy=_CONFIGURED
        )

    async def begin_delete(self) -> None:
        self._owner.requested_delete.append(self._sandbox_id)
        if self._sandbox_id in self._owner.delete_errors:
            raise self._owner.delete_errors[self._sandbox_id]
        if self._sandbox_id not in self._owner.current:
            raise ResourceNotFoundError("gone")
        del self._owner.current[self._sandbox_id]


class _Group:
    def __init__(self, *sandboxes) -> None:
        self.inventory = list(sandboxes)
        self.current = {sandbox.id: sandbox for sandbox in sandboxes}
        self.installed = []
        self.requested_delete: list[str] = []
        self.set_errors: dict[str, Exception] = {}
        self.delete_errors: dict[str, Exception] = {}

    def list_sandboxes(self):
        return _Pager(self.inventory)

    async def get_sandbox(self, sandbox_id: str):
        if sandbox_id not in self.current:
            raise ResourceNotFoundError("gone")
        return self.current[sandbox_id]

    def get_sandbox_client(self, sandbox_id: str):
        return _SandboxClient(self, sandbox_id)


def _run(group: _Group, *, apply: bool = False, **policy_overrides):
    policy = recovery.RecoveryPolicy(**policy_overrides)
    return asyncio.run(
        recovery.recover_lifecycle_policies(group, policy=policy, apply=apply, now=_NOW)
    )


def test_empty_registry_discovery_installs_and_verifies_missing_policy():
    group = _Group(_sandbox("missing"))

    result = _run(group, apply=True)

    assert result.candidates == ["missing"]
    assert result.installed == result.verified == ["missing"]
    assert result.deleted == result.failures == []
    assert len(group.installed) == 1


def test_successfully_installed_policy_is_retained():
    group = _Group(_sandbox("configured", lifecycle_policy=_CONFIGURED))

    result = _run(group, apply=True)

    assert result.configured == ["configured"]
    assert result.candidates == []
    assert group.installed == group.requested_delete == []


def test_fresh_sandbox_is_protected_as_configuration_in_progress():
    group = _Group(_sandbox("fresh", created_at=_FRESH))

    result = _run(group, apply=True)

    assert result.retained == ["fresh"]
    assert result.candidates == []
    assert group.installed == group.requested_delete == []


def test_unrelated_labels_are_ignored():
    unrelated = _sandbox("unrelated", labels={"scope": "scope-a", "thread": "thread-1"})
    group = _Group(unrelated)

    result = _run(group, apply=True)

    assert result.ignored == ["unrelated"]
    assert result.candidates == []
    assert group.installed == group.requested_delete == []


def test_rejected_policy_update_deletes_an_expired_stopped_candidate():
    group = _Group(_sandbox("expired", state="Stopped", stopped_at=_STOPPED_OLD))
    group.set_errors["expired"] = RuntimeError("HTTP 400 invalid policy")

    result = _run(group, apply=True, max_age=None)

    assert result.candidates == ["expired"]
    assert result.deleted == ["expired"]
    assert result.failures
    assert "HTTP 400 invalid policy" in result.failures[0]
    assert group.requested_delete == ["expired"]


def test_policy_update_failure_retains_an_active_candidate_inside_max_age():
    group = _Group(_sandbox("active", created_at="2026-09-08T10:00:00Z"))
    group.set_errors["active"] = RuntimeError("HTTP 400 invalid policy")

    result = _run(group, apply=True, max_age=timedelta(days=7))

    assert result.retained == ["active"]
    assert result.deleted == []
    assert group.requested_delete == []
    assert "invalid policy" in result.failures[0]


def test_policy_update_failure_deletes_a_candidate_past_max_age():
    group = _Group(_sandbox("stale-active"))
    group.set_errors["stale-active"] = RuntimeError("HTTP 400 invalid policy")

    result = _run(group, apply=True, max_age=timedelta(days=1))

    assert result.deleted == ["stale-active"]
    assert group.requested_delete == ["stale-active"]


def test_delete_failure_is_reported_and_can_be_retried():
    group = _Group(_sandbox("expired", state="Stopped", stopped_at=_STOPPED_OLD))
    group.set_errors["expired"] = RuntimeError("HTTP 400 invalid policy")
    group.delete_errors["expired"] = TimeoutError("delete timed out")

    result = _run(group, apply=True, max_age=None)

    assert result.deleted == []
    assert "delete timed out" in result.failures[-1]
    assert "expired" in group.current

    group.delete_errors.clear()
    retried = _run(group, apply=True, max_age=None)
    assert retried.deleted == ["expired"]


def test_concurrent_disappearance_is_success():
    group = _Group(_sandbox("gone"))
    del group.current["gone"]

    result = _run(group, apply=True)

    assert result.already_absent == ["gone"]
    assert result.failures == []


def test_preview_reports_candidates_without_writes():
    group = _Group(_sandbox("missing"))

    result = _run(group)

    assert result.dry_run
    assert result.candidates == ["missing"]
    assert group.installed == group.requested_delete == []
