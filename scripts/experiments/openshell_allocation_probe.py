# /// script
# requires-python = ">=3.11"
# dependencies = ["openshell==0.1.2", "grpcio==1.84.0"]
# ///
"""Kill allocation clients at explicit barriers in a disposable OIDC Docker fixture."""

from __future__ import annotations

import argparse
import json
import os
import re
import select
import subprocess
import sys
import time
import uuid
from importlib import import_module
from pathlib import Path
from tempfile import TemporaryDirectory

from openshell_authority_probe import gateway_fixture, verify_absent, verify_present
from openshell_cleanup import Cleanup
from openshell_owned_probe import fixture_spec, snapshot
from openshell_recovery import AllocationAuthority, AuthorityBusyError, _write, validate


class Client:
    """Use one workspace-scoped token with bounded raw RPCs."""

    def __init__(self, workspace: str, token: str, image: str) -> None:
        self.grpc = import_module("grpc")
        self.pb = import_module("openshell._proto.openshell_pb2")
        self.scope = import_module("openshell._proto.datamodel_pb2").WorkspaceSelector(
            workspace=workspace
        )
        self.channel = self.grpc.insecure_channel("127.0.0.1:19797")
        self.stub = import_module("openshell._proto.openshell_pb2_grpc").OpenShellStub(self.channel)
        self.token, self.image = token, image

    def rpc(self, method, request):
        return getattr(self.stub, method)(
            request, metadata=[("authorization", "Bearer " + self.token)], timeout=10
        )

    def request(self, name, request_id):
        return self.pb.CreateSandboxRequest(
            name=name,
            request_id=request_id,
            workspace_scope=self.scope,
            spec=fixture_spec(self.image),
        ).SerializeToString()

    def send(self, payload, *, on_created):
        """Hand off admitted identity before readiness; enrich the same record on success."""
        request = self.pb.CreateSandboxRequest.FromString(payload)
        response = self.rpc("CreateSandbox", request)
        owner = created_owner(request.name, response.sandbox.metadata.id)
        on_created(owner)
        return self.wait_ready(owner)

    def wait_ready(self, owner):
        """Enrich an already retained owner without another create request."""
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            current = self.rpc(
                "GetSandbox",
                self.pb.GetSandboxRequest(name=owner["name"], workspace_scope=self.scope),
            )
            if current.sandbox.metadata.id != owner["sandbox_id"]:
                raise RuntimeError("created instance was replaced")
            if current.sandbox.status.phase == self.pb.SANDBOX_PHASE_READY:
                observed = snapshot(owner["sandbox_id"])
                owner.update(
                    containers=[c["Id"] for c in observed["containers"]],
                    processes=observed["processes"],
                )
                validate(owner)
                return owner
            time.sleep(0.05)
        raise RuntimeError("workload did not become ready")

    def current_id(self, name):
        try:
            return self.rpc(
                "GetSandbox", self.pb.GetSandboxRequest(name=name, workspace_scope=self.scope)
            ).sandbox.metadata.id
        except self.grpc.RpcError as exc:
            if exc.code() == self.grpc.StatusCode.NOT_FOUND:
                return None
            raise

    def delete(self, name, request_id):
        return self.rpc(
            "DeleteSandbox",
            self.pb.DeleteSandboxRequest(
                name=name, request_id=request_id, workspace_scope=self.scope, allow_missing=True
            ),
        ).sandbox_id

    def close(self):
        self.channel.close()


def created_owner(name, sandbox_id):
    """Retain physical identity before readiness and independent observations exist."""
    return {
        "format": 1,
        "phase": "owned",
        "name": name,
        "sandbox_id": sandbox_id,
        "delete_request_id": str(uuid.uuid4()),
        "containers": [],
        "processes": {},
    }


def observed(owner):
    """Convert confirmed ownership to the independent Docker observer shape."""
    return {
        "id": owner["sandbox_id"],
        "observed": {
            "containers": [{"Id": value} for value in owner["containers"]],
            "processes": owner["processes"],
        },
    }


def dispose_fixture(client, owner):
    """Delete one exclusively owned fixture and independently verify its physical absence."""
    if client.current_id(owner["name"]) != owner["sandbox_id"]:
        raise RuntimeError("fixture identity changed before deletion")
    deleted_id = client.delete(owner["name"], owner["delete_request_id"])
    if deleted_id != owner["sandbox_id"]:
        raise RuntimeError("fixture delete targeted another instance")
    if not verify_absent(observed(owner)):
        raise RuntimeError("fixture cleanup incomplete; retain identities")


def verify_workspace_absent(client, workspace):
    """Require an explicit NOT_FOUND before recording workspace cleanup success."""
    try:
        client.rpc("GetWorkspace", client.pb.GetWorkspaceRequest(name=workspace))
    except client.grpc.RpcError as exc:
        if exc.code() == client.grpc.StatusCode.NOT_FOUND:
            return True
        raise
    raise RuntimeError("workspace cleanup incomplete")


def checkpoint(name, phase):
    """Signal a persisted barrier and wait for the coordinator to kill this process."""
    print(json.dumps({"ready": True, "name": name, "phase": phase}), flush=True)
    while True:
        time.sleep(1)


def worker(args):
    """Hold allocation authority until killed; keep the test oracle outside its ledger."""
    context = json.loads(args.context.read_text(encoding="utf-8"))
    client = Client(context["workspace"], context["token"], args.image)
    try:
        try:
            authority = AllocationAuthority(args.ledger, context["binding"])
        except AuthorityBusyError:
            if args.worker == "contender":
                print(json.dumps({"lock_refused": True}), flush=True)
                return
            raise
        with authority:
            if args.worker == "contender":
                raise RuntimeError("contender acquired active authority")
            name = authority.reserve(client.request)
            if args.worker == "reserved":
                checkpoint(name, "reserved")

            def send(payload):
                # The coordinator's oracle is deliberately outside the recovery ledger.
                owner = client.send(payload, on_created=lambda value: _write(args.oracle, value))
                _write(args.oracle, owner)
                if args.worker == "uncertain":
                    checkpoint(name, "uncertain")
                return owner

            authority.create(name, send)
            checkpoint(name, "owned")
    finally:
        client.close()


def run_case(args, case, context_path, client, binding):
    """Kill one client and distinguish allocator recovery from fixture-only cleanup."""
    command = [
        sys.executable,
        __file__,
        "--execute",
        "--image",
        args.image,
        "--worker",
        case,
        "--context",
        str(context_path),
        "--ledger",
        str(args.ledger),
        "--oracle",
        str(args.evidence / f"{case}-oracle.json"),
    ]
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if not (process.stdout is not None):
        raise RuntimeError("probe expectation failed: process.stdout is not None")
    event = None
    contender = None
    try:
        ready, _, _ = select.select([process.stdout], [], [], 45)
        if not ready:
            raise RuntimeError("worker did not reach crash barrier")
        line = process.stdout.readline()
        if not line:
            raise RuntimeError("worker exited before crash barrier")
        event = json.loads(line)
        competing = command.copy()
        competing[competing.index("--worker") + 1] = "contender"
        contender = subprocess.run(
            competing, capture_output=True, text=True, timeout=10, check=True
        )
    finally:
        process.kill()
        _, error = process.communicate(timeout=5)
        if error:
            print(error, file=sys.stderr)
    if event is None or contender is None:
        raise RuntimeError("missing crash/contender evidence")
    if not (event["ready"] and process.returncode == -9):
        raise RuntimeError('probe expectation failed: event["ready"] and process.returncode == -9')
    if not (json.loads(contender.stdout)["lock_refused"]):
        raise RuntimeError('probe expectation failed: json.loads(contender.stdout)["lock_refused"]')
    oracle_path = args.evidence / f"{case}-oracle.json"
    owner = json.loads(oracle_path.read_text(encoding="utf-8")) if oracle_path.exists() else None
    calls = []
    with AllocationAuthority(args.ledger, binding) as authority:
        entry = authority.allocations()[event["name"]]
        if not (entry["phase"] == event["phase"]):
            raise RuntimeError('probe expectation failed: entry["phase"] == event["phase"]')
        if owner:
            if not (verify_present(observed(owner))):
                raise RuntimeError("probe expectation failed: verify_present(observed(owner))")

        def current(name):
            calls.append("lookup")
            return client.current_id(name)

        def delete(name, request_id):
            calls.append("delete")
            return client.delete(name, request_id)

        result = authority.recover(
            event["name"], current, delete, lambda value: verify_absent(observed(value))
        )
        if case == "confirmed":
            if not (result["cleanup_verified"] and calls == ["lookup", "delete"]):
                raise RuntimeError(
                    'probe expectation failed: result["cleanup_verified"] and calls == ["lookup", "delete"]'
                )
        else:
            if not (result["ownership_retained"] and not calls):
                raise RuntimeError(
                    'probe expectation failed: result["ownership_retained"] and not calls'
                )
        if case == "reserved":
            if client.current_id(event["name"]) is not None:
                raise RuntimeError(
                    'probe expectation failed: client.current_id(event["name"]) is None'
                )
        if case == "uncertain":
            if not (owner and client.current_id(event["name"]) == owner["sandbox_id"]):
                raise RuntimeError(
                    'probe expectation failed: owner and client.current_id(event["name"]) == owner["sandbox_id"]'
                )
            if not (verify_present(observed(owner))):
                raise RuntimeError("probe expectation failed: verify_present(observed(owner))")
        following = authority.reserve(client.request)
        if not (following != event["name"]):
            raise RuntimeError('probe expectation failed: following != event["name"]')
        saved_phase = authority.allocations()[event["name"]]["phase"]
    if owner and case == "uncertain":
        dispose_fixture(client, owner)
    return {
        "case": case,
        "worker_returncode": process.returncode,
        "contender_refused": True,
        "persisted_phase": saved_phase,
        "next_name_distinct": True,
        "workload_survived_client_death": owner is not None,
        "recovery": result,
        "fixture_cleanup_verified": True,
        "uncertain_cleanup_by_fixture_oracle": case == "uncertain",
        "passed": True,
    }


def measure(args, issuer):
    """Own one disposable workspace and record three independent crash barriers."""
    workspace = "s1797-" + uuid.uuid4().hex[:12]
    workspace_absent = False
    binding = "spike-1797-live/" + workspace
    admin = Client(
        workspace, issuer.token("admin", role="spike-admin", scopes="openshell:all"), args.image
    )
    adapter = Client(workspace, issuer.token("adapter"), args.image)
    results = []
    try:
        deadline = time.monotonic() + 15
        while True:
            try:
                if not (admin.rpc("Health", admin.pb.HealthRequest()).version == "0.1.2"):
                    raise RuntimeError(
                        'probe expectation failed: admin.rpc("Health", admin.pb.HealthRequest()).version == "0.1.2"'
                    )
                break
            except admin.grpc.RpcError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.1)
        admin.rpc("CreateWorkspace", admin.pb.CreateWorkspaceRequest(name=workspace))
        admin.rpc(
            "AddWorkspaceMember",
            admin.pb.AddWorkspaceMemberRequest(
                workspace_scope=admin.scope,
                principal_subject="adapter",
                role=admin.pb.WORKSPACE_ROLE_USER,
            ),
        )
        with AllocationAuthority(args.ledger, binding, initialize=True):
            pass
        with TemporaryDirectory(prefix="allocation-credential-") as temporary:
            context_path = Path(temporary) / "context.json"
            context_path.write_text(
                json.dumps({"workspace": workspace, "binding": binding, "token": adapter.token}),
                encoding="utf-8",
            )
            context_path.chmod(0o600)
            for case in ("reserved", "uncertain", "confirmed"):
                result = run_case(args, case, context_path, adapter, binding)
                results.append(result)
                print(json.dumps(result), flush=True)
    finally:
        with Cleanup(adapter.close, admin.close) as cleanup:

            def dispose_oracle(oracle):
                owner = json.loads(oracle.read_text(encoding="utf-8"))
                try:
                    current = admin.current_id(owner["name"])
                    if current == owner["sandbox_id"]:
                        dispose_fixture(admin, owner)
                    elif current is not None or not verify_absent(observed(owner)):
                        raise RuntimeError("fixture resources remain; preserve oracle")
                except Exception as exc:
                    exc.add_note(f"Retained owner: {owner!r}")
                    raise

            for oracle in args.evidence.glob("*-oracle.json"):
                cleanup.attempt(oracle, dispose_oracle, oracle)
            if not cleanup.errors:
                admin.rpc(
                    "DeleteWorkspace",
                    admin.pb.DeleteWorkspaceRequest(name=workspace, allow_missing=True),
                )
                workspace_absent = verify_workspace_absent(admin, workspace)
    return {
        "cases": len(results),
        "workspace_cleanup_verified": workspace_absent,
        "passed": len(results) == 3,
    }


def main():
    """Run the allocation experiment or one bounded coordinator-owned worker."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--evidence", type=Path)
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--worker", choices=("reserved", "uncertain", "confirmed", "contender"))
    parser.add_argument("--context", type=Path)
    parser.add_argument("--oracle", type=Path)
    args = parser.parse_args()
    if os.name != "posix" or not re.fullmatch(r"(?:[^\s]+@)?sha256:[0-9a-f]{64}", args.image):
        parser.error("requires POSIX fixture and immutable image")
    if args.worker:
        worker(args)
        return 0
    if not args.config or not args.evidence:
        parser.error("coordinator requires --config and --evidence")
    args.evidence.mkdir(parents=True, exist_ok=False)
    with gateway_fixture(args.config) as issuer:
        report = measure(args, issuer)
        print(json.dumps(report), flush=True)
    return int(not report["passed"])


if __name__ == "__main__":
    raise SystemExit(main())
