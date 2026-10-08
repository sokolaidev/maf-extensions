"""Recovery refuses uncertain ownership and retains a stable deletion request across failures."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "openshell_recovery",
    Path(__file__).resolve().parents[1] / "scripts/experiments/openshell_recovery.py",
)
assert _spec and _spec.loader
recovery = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(recovery)


@pytest.fixture
def ledger(tmp_path):
    record = {
        "format": 1,
        "phase": "owned",
        "name": "s1797-123456789abc",
        "sandbox_id": "11111111-1111-4111-8111-111111111111",
        "delete_request_id": "22222222-2222-4222-8222-222222222222",
        "containers": ["a" * 64, "b" * 64],
        "processes": {"12": "345"},
    }
    path = tmp_path / "owner.json"
    recovery.save(path, record)
    return path, record


def test_replacement_is_not_deleted_and_original_requires_observation(ledger):
    path, record = ledger
    calls = []
    result = recovery.recover(
        path, lambda name: "replacement", lambda *args: calls.append(args), lambda owner: False
    )
    assert not result["cleanup_verified"] and result["ownership_retained"] and not calls
    result = recovery.recover(
        path,
        lambda name: "replacement",
        lambda *args: calls.append(args),
        lambda owner: owner == {**record, "phase": "pending"},
    )
    assert result["replacement_seen"] and result["cleanup_verified"] and not calls


def test_lost_delete_response_reuses_saved_request_id(ledger):
    path, record = ledger
    calls = []

    def lost(name, request_id):
        calls.append(request_id)
        raise ConnectionError("response lost")

    first = recovery.recover(path, lambda name: record["sandbox_id"], lost, lambda owner: True)
    assert first["ownership_retained"] and recovery.load(path)["phase"] == "pending"

    def replay(name, request_id):
        calls.append(request_id)
        return record["sandbox_id"]

    second = recovery.recover(path, lambda name: record["sandbox_id"], replay, lambda owner: True)
    assert second["cleanup_verified"] and calls == [record["delete_request_id"]] * 2


def test_unavailable_gateway_preserves_pending_record(ledger):
    path, record = ledger

    def unavailable(name):
        raise ConnectionError("gateway unavailable")

    result = recovery.recover(
        path,
        unavailable,
        lambda *args: pytest.fail("must not delete"),
        lambda owner: pytest.fail("must not infer absence"),
    )
    assert result["ownership_retained"] and recovery.load(path) == {**record, "phase": "pending"}


def test_changed_identity_in_delete_response_never_verifies_cleanup(ledger):
    path, record = ledger
    result = recovery.recover(
        path,
        lambda name: record["sandbox_id"],
        lambda *args: "replacement",
        lambda owner: pytest.fail("wrong target"),
    )
    assert not result["cleanup_verified"] and result["ownership_retained"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("format", 2),
        ("sandbox_id", ""),
        ("delete_request_id", "bad"),
        ("containers", ["a" * 64] * 2),
        ("processes", {}),
        ("phase", "guess"),
        ("name", "other"),
    ],
)
def test_malformed_evidence_refuses_before_mutation(ledger, field, value):
    path, record = ledger
    record[field] = value
    path.write_text(json.dumps(record))
    with pytest.raises((ValueError, KeyError)):
        recovery.recover(
            path,
            lambda *args: pytest.fail("must not query"),
            lambda *args: pytest.fail("must not delete"),
            lambda *args: pytest.fail("must not observe"),
        )


def test_missing_original_still_requires_resource_absence(ledger):
    path, _ = ledger
    result = recovery.recover(
        path, lambda name: None, lambda *args: pytest.fail("must not delete"), lambda owner: False
    )
    assert result["ownership_retained"]
    result = recovery.recover(
        path, lambda name: None, lambda *args: pytest.fail("must not delete"), lambda owner: True
    )
    assert result["cleanup_verified"]


def test_interrupted_atomic_replace_preserves_previous_record(ledger, monkeypatch):
    path, original = ledger

    def interrupted(*args):
        raise OSError("replace interrupted")

    monkeypatch.setattr(recovery.os, "replace", interrupted)
    with pytest.raises(OSError, match="replace interrupted"):
        recovery.save(path, {**original, "phase": "pending"})
    assert recovery.load(path) == original
    assert list(path.parent.iterdir()) == [path]


def test_completed_record_is_historical_and_does_not_delete_again(ledger):
    path, original = ledger
    recovery.save(path, {**original, "phase": "cleaned"})
    result = recovery.recover(
        path,
        lambda *args: pytest.fail("historical receipt"),
        lambda *args: pytest.fail("must not delete"),
        lambda *args: pytest.fail("not reobserved"),
    )
    assert result["already_recorded"] and not result["reobserved"]
