# /// script
# requires-python = ">=3.11"
# dependencies = ["openshell==0.1.2", "grpcio==1.84.0"]
# ///
"""Characterize create replay, restart and injected retention loss in an isolated gateway."""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import time
import uuid
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

from openshell_allocation_probe import Client, dispose_fixture, observed
from openshell_authority_probe import gateway_fixture, verify_absent
from openshell_owned_probe import inventory
from openshell_recovery import AllocationAuthority


def wait_gateway(client):
    """Wait for the restarted pinned gateway, without inferring workload readiness."""
    deadline = time.monotonic() + 20
    while True:
        try:
            if client.rpc("Health", client.pb.HealthRequest()).version != "0.1.2":
                raise RuntimeError("requires gateway 0.1.2")
            return
        except client.grpc.RpcError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.1)


def replay(client, payload):
    """Capture the explicit replay marker as well as status and physical identity."""
    request = client.pb.CreateSandboxRequest.FromString(payload)
    try:
        response, call = client.stub.CreateSandbox.with_call(
            request, metadata=[("authorization", "Bearer " + client.token)], timeout=10
        )
        return {
            "status": "OK",
            "sandbox_id": response.sandbox.metadata.id,
            "replayed": dict(call.initial_metadata()).get("openshell-replayed") == "true",
        }
    except client.grpc.RpcError as exc:
        return {"status": exc.code().name, "detail": exc.details(), "replayed": False}


def inject_admission(control, sandbox_id, mode):
    """Alter one matched create receipt only while this disposable gateway is stopped."""
    gateway = control["gateway"]
    gateway.stop()
    if gateway.process.poll() is None:
        raise RuntimeError("gateway did not stop before fixture injection")
    try:
        with closing(sqlite3.connect(control["database"])) as db, db:
            rows = db.execute(
                "SELECT id,payload FROM objects WHERE object_type=?", ("mutation_admission_v1",)
            ).fetchall()
            matched = []
            for row_id, data in rows:
                admission = json.loads(data)
                outcome = (admission.get("success") or {}).get("Ordinary", {}).get("Sandbox", {})
                if outcome.get("id") == sandbox_id:
                    matched.append((row_id, admission))
            if len(matched) != 1:
                raise RuntimeError(f"expected one original create receipt, found {len(matched)}")
            row_id, admission = matched[0]
            aged = int(time.time() * 1000) - 25 * 60 * 60 * 1000
            if mode == "expired_success":
                admission["completed_at_ms"] = aged
            elif mode == "unresolved":
                admission.update(success=None, completed_at_ms=None)
            else:
                raise ValueError("unknown fault injection")
            changed = db.execute(
                "UPDATE objects SET payload=?,created_at_ms=?,updated_at_ms=? WHERE id=? AND object_type=?",
                (json.dumps(admission).encode(), aged, aged, row_id, "mutation_admission_v1"),
            )
            if changed.rowcount != 1:
                raise RuntimeError("fixture injection did not update exactly one admission")
    finally:
        gateway.start()


def measure(image, issuer, control, root):
    """Compare original instance receipts with replacement and expired/unresolved admissions."""
    workspace = "s1797-" + uuid.uuid4().hex[:12]
    admin = Client(
        workspace, issuer.token("admin", role="spike-admin", scopes="openshell:all"), image
    )
    adapter = Client(workspace, issuer.token("adapter"), image)
    peer = Client(workspace, issuer.token("peer"), image)
    records, results = [], []
    binding = "spike-1797-live/" + workspace

    def emit(case, result, passed):
        receipt = {"case": case, **result, "passed": passed}
        results.append(receipt)
        print(json.dumps(receipt), flush=True)
        if not passed:
            raise RuntimeError(f"unexpected observation: {case}")

    def create(name=None):
        name = name or "s1797-" + uuid.uuid4().hex[:12]
        payload = adapter.request(name, str(uuid.uuid4()))
        owner = adapter.send(payload)
        records.append(owner)
        return payload, owner

    try:
        wait_gateway(admin)
        admin.rpc("CreateWorkspace", admin.pb.CreateWorkspaceRequest(name=workspace))
        for subject in ("adapter", "peer"):
            admin.rpc(
                "AddWorkspaceMember",
                admin.pb.AddWorkspaceMemberRequest(
                    workspace_scope=admin.scope,
                    principal_subject=subject,
                    role=admin.pb.WORKSPACE_ROLE_USER,
                ),
            )
        with AllocationAuthority(root / "ledger", binding, initialize=True) as authority:
            name = authority.reserve(adapter.request)

            def lose_result(payload):
                owner = adapter.send(payload)
                records.append(owner)
                raise ConnectionError("fixture withheld confirmed create result")

            try:
                authority.create(name, lose_result)
            except ConnectionError:
                # Withheld result delivery must leave the allocation uncertain.
                pass
            row = authority.allocations()[name]
            assert row["phase"] == "uncertain"
            payload = bytes.fromhex(row["request"])
            original = records[-1]
            first = replay(adapter, payload)
            emit(
                "uncertain_create_immediate_replay",
                first,
                first.get("sandbox_id") == original["sandbox_id"] and first["replayed"],
            )
            changed = adapter.pb.CreateSandboxRequest.FromString(payload)
            changed.spec.command[:] = ["sleep", "3599"]
            mismatch = replay(adapter, changed.SerializeToString())
            emit(
                "same_id_changed_payload",
                mismatch,
                mismatch["status"] == "FAILED_PRECONDITION"
                and mismatch.get("detail")
                == "request_id was already used with a different payload",
            )
            other = replay(peer, payload)
            emit("same_request_other_subject", other, other["status"] == "ALREADY_EXISTS")
            control["gateway"].stop()
            control["gateway"].start()
            wait_gateway(admin)
            restarted = replay(adapter, payload)
            emit(
                "replay_after_gateway_restart",
                restarted,
                restarted.get("sandbox_id") == original["sandbox_id"] and restarted["replayed"],
            )
            assert authority.allocations()[name]["phase"] == "uncertain"
        dispose_fixture(admin, original)
        _, replacement = create(original["name"])
        absent_original = replay(adapter, payload)
        emit(
            "original_deleted_replacement_present",
            absent_original,
            absent_original["status"] == "FAILED_PRECONDITION"
            and "this request was not executed again" in absent_original.get("detail", "")
            and adapter.current_id(replacement["name"]) == replacement["sandbox_id"]
            and bool(inventory(replacement["sandbox_id"])["containers"]),
        )
        dispose_fixture(admin, replacement)

        expiring_payload, expiring = create()
        dispose_fixture(admin, expiring)
        inject_admission(control, expiring["sandbox_id"], "expired_success")
        wait_gateway(admin)
        expired = replay(adapter, expiring_payload)
        # Capture any newly created fixture resource before evaluating the assertion.
        if expired["status"] == "OK":
            new_owner = adapter.send(expiring_payload)
            records.append(new_owner)
        emit(
            "expired_success_readmits_create",
            expired,
            expired["status"] == "OK"
            and not expired["replayed"]
            and expired.get("sandbox_id") != expiring["sandbox_id"],
        )
        again = replay(adapter, expiring_payload)
        emit(
            "reused_request_now_replays_new_instance",
            again,
            again["replayed"] and again.get("sandbox_id") == expired.get("sandbox_id"),
        )

        unresolved_payload, unresolved = create()
        inject_admission(control, unresolved["sandbox_id"], "unresolved")
        wait_gateway(admin)
        pending = replay(adapter, unresolved_payload)
        emit(
            "aged_unresolved_admission_refuses",
            pending,
            pending["status"] == "FAILED_PRECONDITION"
            and "no confirmed replayable success" in pending.get("detail", "")
            and adapter.current_id(unresolved["name"]) == unresolved["sandbox_id"],
        )
    finally:
        for owner in reversed(records):
            if admin.current_id(owner["name"]) == owner["sandbox_id"]:
                admin.delete(owner["name"], owner["delete_request_id"])
            if not verify_absent(observed(owner)):
                raise RuntimeError("fixture cleanup incomplete; retain identities")
        admin.rpc(
            "DeleteWorkspace", admin.pb.DeleteWorkspaceRequest(name=workspace, allow_missing=True)
        )
        workspace_absent = False
        try:
            admin.rpc("GetWorkspace", admin.pb.GetWorkspaceRequest(name=workspace))
        except admin.grpc.RpcError as exc:
            workspace_absent = exc.code() == admin.grpc.StatusCode.NOT_FOUND
        for client in (adapter, peer, admin):
            client.close()
    assert workspace_absent
    return {
        "cases": len(results),
        "passed": len(results) == 8,
        "physical_cleanup_verified": True,
        "workspace_cleanup_verified": workspace_absent,
        "allocator_still_refuses_automatic_replay": True,
        "expiry_clock": "fixture completion timestamp aged 25 hours; not elapsed wall time",
    }


def main():
    """Run only an isolated loopback gateway with disposable persistence."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    if not re.fullmatch(r"(?:[^\s]+@)?sha256:[0-9a-f]{64}", args.image):
        parser.error("requires immutable image")
    control = {}
    with TemporaryDirectory(prefix="replay-ledger-") as temporary:
        with gateway_fixture(args.config, control=control) as issuer:
            report = measure(args.image, issuer, control, Path(temporary))
            print(json.dumps(report), flush=True)
    return int(not report["passed"])


if __name__ == "__main__":
    raise SystemExit(main())
