"""Contract proposals are exercised against independent state invariants, not a live gateway."""

from __future__ import annotations

import copy
import importlib
from dataclasses import replace
from pathlib import Path

import pytest


@pytest.fixture
def model(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts/experiments"))
    api = importlib.import_module("openshell_contract_model")
    scope = api.Scope("epoch-1", "workspace-id", "issuer", "adapter")
    server = api.ContractModel(scope.epoch, scope.workspace, {(scope.issuer, scope.subject)})
    return api, server, api.Create(scope, "create-1", "guest", "digest-1")


def create(server, intent):
    return server.complete_create(intent, server.admit_create(intent))


def read_without_effects(server, intent):
    """Reject admission and physical mutations, not just a changed returned outcome."""
    before = copy.deepcopy(vars(server))
    receipt = server.lookup(intent)
    assert vars(server) == before, "receipt lookup mutated state"
    return receipt


@pytest.mark.parametrize(
    "phase",
    ["absent", "unresolved", "confirmed", "expired", "replacement", "lost", "revoked", "mismatch"],
)
def test_receipt_lookup_has_no_state_or_physical_effects(model, phase):
    api, server, intent = model
    original = None
    admission_id = None
    if phase != "absent":
        admission_id = server.admit_create(intent)
        if phase != "unresolved":
            original = server.complete_create(intent, admission_id)
    expected = {
        "absent": "absent_at_observation",
        "unresolved": "unresolved",
        "confirmed": "confirmed",
        "expired": "history_unavailable",
        "replacement": "confirmed",
        "lost": "binding_mismatch",
        "revoked": "forbidden",
        "mismatch": "binding_mismatch",
    }[phase]
    if phase == "expired":
        server.expire_create_receipt(intent)
    elif phase == "replacement":
        server.names.pop(intent.name)
        server.resources.remove(original)
        create(server, replace(intent, request_id="create-2"))
    elif phase == "lost":
        server.lose_history("epoch-2")
    elif phase == "revoked":
        server.members.clear()
    elif phase == "mismatch":
        intent = replace(intent, payload_digest="other")
    receipt = read_without_effects(server, intent)
    assert receipt.state == expected
    if phase in ("confirmed", "replacement"):
        assert receipt.physical_id == original
        assert receipt.admission_id == admission_id
    else:
        assert receipt.physical_id is None


def test_absent_lookup_does_not_fence_delayed_creation(model):
    _, server, intent = model
    assert server.lookup(intent).state == "absent_at_observation"
    original = create(server, intent)
    assert original in server.resources
    assert server.lookup(intent).physical_id == original


def test_expired_original_never_readmits_or_resurrects(model):
    api, server, intent = model
    original = create(server, intent)
    server.names.clear()
    server.resources.remove(original)
    server.expire_create_receipt(intent)
    before = copy.deepcopy(vars(server))
    with pytest.raises(api.Refused, match="history_unavailable"):
        server.admit_create(intent)
    assert vars(server) == before
    assert server.lookup(intent).state == "history_unavailable"


def test_unresolved_admission_cannot_be_expired_or_replaced(model):
    api, server, intent = model
    admission_id = server.admit_create(intent)
    with pytest.raises(api.Refused, match="not_confirmed"):
        server.expire_create_receipt(intent)
    assert server.admit_create(intent) == admission_id
    with pytest.raises(api.Refused, match="binding_mismatch"):
        server.admit_create(replace(intent, payload_digest="different"))
    assert server.lookup(intent).state == "unresolved"


@pytest.mark.parametrize("field", ["epoch", "workspace", "issuer", "subject"])
def test_changed_binding_never_retrieves_original(model, field):
    api, server, intent = model
    original = create(server, intent)
    altered = replace(intent, scope=replace(intent.scope, **{field: "different"}))
    before = copy.deepcopy(vars(server))
    assert server.lookup(altered).physical_id is None
    with pytest.raises(api.Refused):
        server.admit_create(altered)
    assert vars(server) == before and original in server.resources


def test_another_authorized_subject_has_no_original_receipt(model):
    _, server, intent = model
    create(server, intent)
    server.members.add(("issuer", "peer"))
    other = replace(intent, scope=replace(intent.scope, subject="peer"))
    assert server.lookup(other).state == "absent_at_observation"


def test_preserved_state_restart_returns_original_without_resurrection(model):
    _, server, intent = model
    original = create(server, intent)
    server.resources.remove(original)
    server.names.clear()
    restarted = copy.deepcopy(server)
    assert restarted.lookup(intent) == server.lookup(intent)
    assert restarted.lookup(intent).physical_id == original
    assert not restarted.resources


def test_notified_history_loss_refuses_old_pending_work_and_epoch_reuse(model):
    api, server, intent = model
    admission_id = server.admit_create(intent)
    server.lose_history("epoch-2")
    with pytest.raises(api.Refused, match="binding_mismatch"):
        server.complete_create(intent, admission_id)
    with pytest.raises(api.Refused, match="epoch_reuse"):
        server.lose_history("epoch-1")
    assert not server.resources


def replacement_schedule(api, server, intent, barrier):
    """Keep B in a separate oracle and fail if any A-targeted effect reaches it."""
    original = create(server, intent)
    deletion = api.Delete(intent.scope, "delete-1", intent.name, original)
    replacement = None
    job = None
    events = ["admit", "ack", "cleanup"]
    events.insert(barrier, "replace")
    for event in events:
        if event == "replace":
            server.names.pop(intent.name, None)
            replacement = create(server, replace(intent, request_id="create-2"))
        elif event == "admit":
            try:
                job = server.admit_delete(deletion)
            except api.Refused:
                assert replacement is not None
        elif job is not None and event == "ack":
            server.acknowledge_delete(job)
        elif job is not None and event == "cleanup":
            server.cleanup_delete(job)
        if replacement is not None:
            assert replacement in server.resources, "replacement was deleted"
            assert server.names.get(intent.name) == replacement, "replacement registration changed"
            assert replacement not in server.effects, "replacement reached a driver effect"
    if barrier > 0:
        assert job is not None
        assert original not in server.resources, "original resource remains"
        assert original in server.effects, "original never reached driver cleanup"
    return deletion, job, replacement


@pytest.mark.parametrize(
    "barrier",
    range(4),
    ids=[
        "before-admission-or-while-queued",
        "after-atomic-admission",
        "after-ack-before-driver",
        "after-driver",
    ],
)
def test_stale_delete_preserves_replacement_at_each_barrier(model, barrier):
    api, server, intent = model
    replacement_schedule(api, server, intent, barrier)


def test_barrier_oracle_detects_unsafe_name_based_driver(model):
    api, _, intent = model

    class UnsafeNameDriver(api.ContractModel):
        def cleanup_delete(self, job_id):
            job = self._job(job_id)
            target = self.names.get(job.name)
            self.resources.discard(target)
            self.effects.append(target)

    unsafe = UnsafeNameDriver(intent.scope.epoch, intent.scope.workspace, {("issuer", "adapter")})
    with pytest.raises(AssertionError, match="replacement was deleted"):
        replacement_schedule(api, unsafe, intent, 2)


def test_acknowledgement_does_not_establish_resource_absence(model):
    api, server, intent = model
    original = create(server, intent)
    job = server.admit_delete(api.Delete(intent.scope, "delete-1", intent.name, original))
    server.acknowledge_delete(job)
    assert intent.name not in server.names
    assert original in server.resources and not server.effects
    server.cleanup_delete(job)
    assert original not in server.resources


def test_lost_delete_reply_and_restart_keep_original_target(model):
    api, server, intent = model
    original = create(server, intent)
    deletion = api.Delete(intent.scope, "delete-1", intent.name, original)
    job = server.admit_delete(deletion)
    restarted = copy.deepcopy(server)
    assert restarted.admit_delete(deletion) == job
    restarted.names.clear()
    replacement = create(restarted, replace(intent, request_id="create-2"))
    restarted.acknowledge_delete(job)
    restarted.cleanup_delete(job)
    assert original not in restarted.resources and replacement in restarted.resources
    assert restarted.effects == [original]


def test_expired_delete_receipt_never_retargets_and_pending_job_survives(model):
    api, server, intent = model
    original = create(server, intent)
    deletion = api.Delete(intent.scope, "delete-1", intent.name, original)
    job = server.admit_delete(deletion)
    server.expire_delete_receipt(deletion)
    server.names.clear()
    replacement = create(server, replace(intent, request_id="create-2"))
    with pytest.raises(api.Refused, match="history_unavailable"):
        server.admit_delete(deletion)
    server.acknowledge_delete(job)
    server.cleanup_delete(job)
    assert replacement in server.resources and server.effects == [original]


@pytest.mark.parametrize("field,value", [("expected_id", "another"), ("name", "another")])
def test_same_delete_request_id_cannot_change_target(model, field, value):
    api, server, intent = model
    original = create(server, intent)
    deletion = api.Delete(intent.scope, "delete-1", intent.name, original)
    server.admit_delete(deletion)
    before = copy.deepcopy(vars(server))
    with pytest.raises(api.Refused, match="binding_mismatch"):
        server.admit_delete(replace(deletion, **{field: value}))
    assert vars(server) == before


def test_state_loss_fences_old_delete_job_without_claiming_cleanup(model):
    api, server, intent = model
    original = create(server, intent)
    deletion = api.Delete(intent.scope, "delete-1", intent.name, original)
    job = server.admit_delete(deletion)
    server.lose_history("epoch-2")
    with pytest.raises(api.Refused, match="job_history_unavailable"):
        server.cleanup_delete(job)
    with pytest.raises(api.Refused, match="binding_mismatch"):
        server.admit_delete(deletion)
    assert original in server.resources and not server.effects


def test_already_gone_original_ack_does_not_remove_future_replacement(model):
    api, server, intent = model
    original = create(server, intent)
    server.names.clear()
    server.resources.remove(original)
    job = server.admit_delete(api.Delete(intent.scope, "delete-1", intent.name, original))
    replacement = create(server, replace(intent, request_id="create-2"))
    server.acknowledge_delete(job)
    server.cleanup_delete(job)
    assert replacement in server.resources and server.names[intent.name] == replacement


def test_barrier_oracle_rejects_noop_driver(model):
    api, _, intent = model

    class NoopDriver(api.ContractModel):
        def cleanup_delete(self, job_id):
            pass

    noop = NoopDriver(intent.scope.epoch, intent.scope.workspace, {("issuer", "adapter")})
    with pytest.raises(AssertionError, match="original resource remains"):
        replacement_schedule(api, noop, intent, 2)


def test_readonly_oracle_rejects_admitting_lookup(model):
    api, _, intent = model

    class MutatingLookup(api.ContractModel):
        def lookup(self, request):
            self.admit_create(request)
            return super().lookup(request)

    unsafe = MutatingLookup(intent.scope.epoch, intent.scope.workspace, {("issuer", "adapter")})
    with pytest.raises(AssertionError, match="receipt lookup mutated state"):
        read_without_effects(unsafe, intent)


@pytest.mark.parametrize("field", ["epoch", "workspace"])
def test_delete_binding_mismatch_never_arms_cleanup(model, field):
    api, server, intent = model
    original = create(server, intent)
    deletion = api.Delete(
        replace(intent.scope, **{field: "changed"}), "delete-1", intent.name, original
    )
    before = copy.deepcopy(vars(server))
    with pytest.raises(api.Refused, match="binding_mismatch"):
        server.admit_delete(deletion)
    assert vars(server) == before


def test_pending_create_never_adopts_an_intervening_same_name_instance(model):
    api, server, intent = model
    admission_id = server.admit_create(intent)
    replacement = create(server, replace(intent, request_id="create-2"))
    before = copy.deepcopy(vars(server))
    with pytest.raises(api.Refused, match="name_conflict"):
        server.complete_create(intent, admission_id)
    assert vars(server) == before
    receipt = read_without_effects(server, intent)
    assert receipt.state == "unresolved" and receipt.physical_id is None
    assert replacement in server.resources
