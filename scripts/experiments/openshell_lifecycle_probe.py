# /// script
# requires-python = ">=3.11"
# dependencies = ["openshell==0.1.2", "grpcio==1.84.0"]
# ///
"""Reproduce name-replacement races and recover a killed client's confirmed Docker ownership."""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import re
import select
import subprocess
import sys
import time
import uuid
from importlib import import_module
from pathlib import Path

from openshell_owned_probe import docker_get, fixture_spec, inventory, process_identity, snapshot
from openshell_recovery import load, recover, save


class Fixture:
    """Own a fixture API connection; each method uses explicit finite RPC deadlines."""

    def __init__(self, endpoint: str, image: str) -> None:
        self.grpc = import_module("grpc")
        self.pb = import_module("openshell._proto.openshell_pb2")
        self.scope = import_module("openshell._proto.datamodel_pb2").WorkspaceSelector(
            workspace="default"
        )
        self.channel = self.grpc.insecure_channel(endpoint)
        self.stub = import_module("openshell._proto.openshell_pb2_grpc").OpenShellStub(self.channel)
        self.client = import_module("openshell.sandbox").SandboxClient(endpoint, timeout=5)
        self.image = image

    def close(self) -> None:
        """Close local connections without treating them as remote cleanup."""
        self.client.close()
        self.channel.close()

    def create(self, name: str) -> dict:
        """Capture a ready instance and its independently observed physical resources."""
        if self.client.health().version != "0.1.2":
            raise RuntimeError("requires gateway 0.1.2")
        instance = self.client.create(name=name, workspace="default", spec=fixture_spec(self.image))
        print(json.dumps({"created": name, "sandbox_id": instance.id}), flush=True)
        self.client.wait_ready(name, workspace="default", timeout_seconds=30)
        observed = snapshot(instance.id)
        return {
            "format": 1,
            "phase": "owned",
            "name": name,
            "sandbox_id": instance.id,
            "delete_request_id": str(uuid.uuid4()),
            "containers": [x["Id"] for x in observed["containers"]],
            "processes": observed["processes"],
        }

    def current_id(self, name: str) -> str | None:
        """Treat only an explicit NOT_FOUND as absent."""
        try:
            response = self.stub.GetSandbox(
                self.pb.GetSandboxRequest(name=name, workspace_scope=self.scope), timeout=3
            )
            return response.sandbox.metadata.id
        except self.grpc.RpcError as exc:
            if exc.code() == self.grpc.StatusCode.NOT_FOUND:
                return None
            raise

    def delete(self, name: str, request_id: str) -> str:
        """Send one name-addressed delete, preserving its admission identity across retries."""
        response = self.stub.DeleteSandbox(
            self.pb.DeleteSandboxRequest(
                name=name, workspace_scope=self.scope, allow_missing=True, request_id=request_id
            ),
            timeout=5,
        )
        return response.sandbox_id

    def verify_absent(self, record: dict) -> bool:
        """Observe captured containers, sandbox-labelled resources and known processes independently."""
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            now = inventory(record["sandbox_id"])
            originals = [
                docker_get(f"/containers/{identifier}/json") for identifier in record["containers"]
            ]
            alive = [
                pid for pid, start in record["processes"].items() if process_identity(pid) == start
            ]
            if not now["containers"] and not now["volumes"] and not any(originals) and not alive:
                return True
            time.sleep(0.05)
        return False


def replacement_case(args) -> dict:
    """Replace A with B at a deliberate barrier; only this test owns either sandbox."""
    fixture = Fixture(args.endpoint, args.image)
    name = "s1797-" + uuid.uuid4().hex[:12]
    records = []
    result = {"case": args.case}
    try:
        original = fixture.create(name)
        records.append(original)
        save(args.ledger, original)
        result["precheck_matched"] = fixture.current_id(name) == original["sandbox_id"]
        if fixture.delete(name, original["delete_request_id"]) != original["sandbox_id"]:
            raise RuntimeError("original delete targeted another instance")
        fixture.client.wait_deleted(
            name,
            workspace="default",
            expected_sandbox_id=original["sandbox_id"],
            timeout_seconds=20,
        )
        assert fixture.verify_absent(original)
        replacement = fixture.create(name)
        records.append(replacement)
        result["distinct_ids"] = original["sandbox_id"] != replacement["sandbox_id"]
        if args.case == "stale_precheck":
            result["recovery"] = recover(
                args.ledger, fixture.current_id, fixture.delete, fixture.verify_absent
            )
        else:
            key = (
                original["delete_request_id"] if args.case == "delete_replay" else str(uuid.uuid4())
            )
            target = fixture.delete(name, key)
            result["target_was_original"] = target == original["sandbox_id"]
            result["target_was_replacement"] = target == replacement["sandbox_id"]
            if args.case == "stale_race":
                fixture.client.wait_deleted(
                    name,
                    workspace="default",
                    expected_sandbox_id=replacement["sandbox_id"],
                    timeout_seconds=20,
                )
                result["replacement_disposed"] = fixture.verify_absent(replacement)
        result["replacement_survives"] = fixture.current_id(name) == replacement[
            "sandbox_id"
        ] and bool(inventory(replacement["sandbox_id"])["containers"])
    finally:
        for record in reversed(records):
            if fixture.current_id(name) == record["sandbox_id"]:
                fixture.delete(name, record["delete_request_id"])
            if not fixture.verify_absent(record):
                raise RuntimeError("fixture cleanup incomplete; retain recorded identities")
        fixture.close()
    result["fixture_cleanup_verified"] = True
    return result


def crash_worker(args) -> None:
    """Persist confirmed ownership before emitting a kill-ready event; parent owns recovery."""
    fixture = Fixture(args.endpoint, args.image)
    record = fixture.create("s1797-" + uuid.uuid4().hex[:12])
    save(args.ledger, record)
    code = "import subprocess,os,time; subprocess.Popen(['python','-c','import time; time.sleep(120)']); os.write(1,b'READY'); time.sleep(120)"
    for chunk in fixture.client.exec_stream(
        record["name"],
        ["python", "-c", code],
        workspace="default",
        workdir="/sandbox",
        no_login_shell=True,
    ):
        if getattr(chunk, "data", b"") == b"READY":
            observed = snapshot(record["sandbox_id"])
            record["processes"].update(observed["processes"])
            save(args.ledger, record)
            print(
                json.dumps({"kill_ready": True, "processes": len(record["processes"])}), flush=True
            )
    raise RuntimeError("worker returned before its coordinator killed it")


def client_crash(args) -> dict:
    """Kill only the experiment's client worker and optionally leave its journal for recovery."""
    command = [
        sys.executable,
        __file__,
        "--execute",
        "--image",
        args.image,
        "--endpoint",
        args.endpoint,
        "--case",
        "worker",
        "--ledger",
        str(args.ledger),
    ]
    worker = subprocess.Popen(
        command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1
    )
    assert worker.stdout is not None
    ready = False
    try:
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            readable, _, _ = select.select([worker.stdout], [], [], 1)
            if not readable:
                continue
            line = worker.stdout.readline()
            if not line:
                break
            event = json.loads(line)
            print(json.dumps(event), flush=True)
            if event.get("kill_ready"):
                ready = True
                break
    finally:
        worker.kill()
        _, error = worker.communicate(timeout=5)
    if not ready:
        raise RuntimeError(f"worker did not reach confirmed ownership: {error}")
    record = load(args.ledger)
    result = {
        "case": args.case,
        "worker_returncode": worker.returncode,
        "journal_phase": record["phase"],
        "resources_after_client_death": snapshot(record["sandbox_id"]),
    }
    if args.case == "client_crash":
        result["recovery"] = recovery_case(args)
    return result


def recovery_case(args) -> dict:
    """Recover from the persisted journal using a fresh API connection."""
    fixture = Fixture(args.endpoint, args.image)
    try:
        return recover(args.ledger, fixture.current_id, fixture.delete, fixture.verify_absent)
    finally:
        fixture.close()


def main() -> int:
    """Run explicit lifecycle experiments; intentional race failures are observations."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--endpoint", default="127.0.0.1:19797")
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument(
        "--case",
        choices=[
            "stale_precheck",
            "stale_race",
            "delete_replay",
            "client_crash",
            "crash_hold",
            "worker",
            "recover",
        ],
        required=True,
    )
    args = parser.parse_args()
    if not re.fullmatch(r"(?:[^\s]+@)?sha256:[0-9a-f]{64}", args.image) or not re.fullmatch(
        r"127\.0\.0\.1:[0-9]+", args.endpoint
    ):
        parser.error("requires an immutable image and loopback fixture endpoint")
    if importlib.metadata.version("openshell") != "0.1.2":
        raise RuntimeError("requires openshell 0.1.2")
    if args.case != "recover" and args.case != "worker" and args.ledger.exists():
        parser.error("use a fresh ledger path; recover an existing record before starting again")
    if args.case == "worker":
        crash_worker(args)
        return 1
    if args.case == "recover":
        result = recovery_case(args)
    elif args.case in ("client_crash", "crash_hold"):
        result = client_crash(args)
    else:
        result = replacement_case(args)
    print(json.dumps(result), flush=True)
    return int(args.case == "recover" and not result["cleanup_verified"])


if __name__ == "__main__":
    raise SystemExit(main())
