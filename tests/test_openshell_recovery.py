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


@pytest.fixture
def authority(tmp_path):
    with recovery.AllocationAuthority(
        tmp_path / "authority", "gateway/workspace", initialize=True
    ) as value:
        yield value


def request(name, request_id):
    return json.dumps({"name": name, "request_id": request_id}).encode()


def test_reservation_survives_reopen_without_reusing_name(authority):
    first = authority.reserve(request)
    authority.close()
    with recovery.AllocationAuthority(authority.root, authority.binding) as restarted:
        second = restarted.reserve(request)
        assert first != second
        assert len(first) == len(second) == 19
        assert list(restarted.allocations()) == [first, second]


def test_dispatch_persists_uncertainty_before_transport(authority):
    name = authority.reserve(request)

    def lost(payload):
        assert authority.allocations()[name]["phase"] == "uncertain"
        assert json.loads(payload)["name"] == name
        raise ConnectionError("lost response")

    with pytest.raises(ConnectionError):
        authority.create(name, lost)
    with pytest.raises(ValueError, match="undispatched"):
        authority.create(name, lambda payload: pytest.fail("must not replay"))
    result = authority.recover(name, *[lambda *args: pytest.fail("unconfirmed mutation")] * 3)
    assert result["ownership_retained"] and result["phase"] == "uncertain"
    assert authority.reserve(request) != name


def test_reservation_never_queries_or_deletes_by_name(authority):
    name = authority.reserve(request)
    result = authority.recover(name, *[lambda *args: pytest.fail("undispatched mutation")] * 3)
    assert result["phase"] == "reserved" and not result["cleanup_verified"]


def test_confirmed_allocation_recovery_retains_tombstone(authority, ledger):
    _, original = ledger
    name = authority.reserve(request)
    owner = authority.create(name, lambda payload: {**original, "name": name})
    first = authority.recover(
        name, lambda _: owner["sandbox_id"], lambda *args: owner["sandbox_id"], lambda _: False
    )
    assert first["ownership_retained"] and authority.allocations()[name]["phase"] == "pending"
    second = authority.recover(
        name, lambda _: None, lambda *args: pytest.fail("already absent"), lambda _: True
    )
    assert second["cleanup_verified"] and authority.allocations()[name]["phase"] == "cleaned"
    assert authority.reserve(request) != name
    with pytest.raises(ValueError, match="undispatched"):
        authority.create(name, lambda payload: pytest.fail("must not reuse"))


def test_separate_handle_cannot_allocate_or_recover(authority):
    with pytest.raises(recovery.AuthorityBusyError):
        recovery.AllocationAuthority(authority.root, authority.binding)


def test_reentrant_mutation_refuses_instead_of_overwriting_history(authority):
    name = authority.reserve(request)
    with pytest.raises(RuntimeError, match="already active"):
        authority.create(name, lambda payload: authority.reserve(request))
    assert list(authority.allocations()) == [name]
    assert authority.allocations()[name]["phase"] == "uncertain"


def test_binding_mismatch_refuses_and_releases_lock(authority):
    authority.close()
    with pytest.raises(ValueError, match="binding mismatch"):
        recovery.AllocationAuthority(authority.root, "another/workspace")
    with recovery.AllocationAuthority(authority.root, authority.binding) as reopened:
        assert reopened.allocations() == {}


def test_missing_history_is_not_reinitialized(authority):
    authority.close()
    authority.path.unlink()
    with pytest.raises(FileNotFoundError):
        recovery.AllocationAuthority(authority.root, authority.binding)
    with pytest.raises(FileExistsError):
        recovery.AllocationAuthority(authority.root, authority.binding, initialize=True)


def test_failed_reservation_persistence_prevents_dispatch(authority, monkeypatch):
    def interrupted(*args):
        raise OSError("replace interrupted")

    monkeypatch.setattr(recovery.os, "replace", interrupted)
    with pytest.raises(OSError, match="replace interrupted"):
        authority.reserve(request)
    assert authority.allocations() == {}


def test_failed_uncertainty_persistence_never_sends(authority, monkeypatch):
    name = authority.reserve(request)

    def interrupted(*args):
        raise OSError("replace interrupted")

    monkeypatch.setattr(recovery.os, "replace", interrupted)
    with pytest.raises(OSError):
        authority.create(name, lambda payload: pytest.fail("must persist before send"))
    assert authority.allocations()[name]["phase"] == "reserved"


def test_wrong_response_identity_stays_uncertain(authority, ledger):
    _, original = ledger
    name = authority.reserve(request)
    with pytest.raises(ValueError, match="another name"):
        authority.create(name, lambda payload: original)
    assert authority.allocations()[name]["phase"] == "uncertain"


@pytest.mark.parametrize("change", ["counter", "gap", "phase", "binding"])
def test_corrupt_allocation_history_refuses_before_dispatch(authority, change):
    name = authority.reserve(request)
    state = json.loads(authority.path.read_text(encoding="utf-8"))
    if change == "counter":
        state["next"] = 1
    elif change == "gap":
        state["allocations"][name[:-1] + "2"] = state["allocations"].pop(name)
    elif change == "phase":
        state["allocations"][name]["phase"] = "maybe"
    else:
        state["binding"] = "other"
    authority.path.write_text(json.dumps(state), encoding="utf-8")
    with pytest.raises(ValueError):
        authority.create(name, lambda payload: pytest.fail("must not dispatch"))


def test_failed_confirmation_retains_uncertainty(authority, ledger, monkeypatch):
    _, original = ledger
    name = authority.reserve(request)
    replace = recovery.os.replace

    def interrupt_confirmation(source, target):
        state = json.loads(Path(source).read_text(encoding="utf-8"))
        if state["allocations"][name]["phase"] == "owned":
            raise OSError("confirmation interrupted")
        replace(source, target)

    monkeypatch.setattr(recovery.os, "replace", interrupt_confirmation)
    with pytest.raises(OSError, match="confirmation interrupted"):
        authority.create(name, lambda payload: {**original, "name": name})
    assert authority.allocations()[name]["phase"] == "uncertain"


def test_closed_authority_cannot_mutate(authority):
    authority.close()
    with pytest.raises(RuntimeError, match="closed"):
        authority.reserve(request)


def test_forked_authority_cannot_mutate(authority, monkeypatch):
    monkeypatch.setattr(recovery.os, "getpid", lambda: authority.pid + 1)
    with pytest.raises(RuntimeError, match="inherited"):
        authority.reserve(request)
