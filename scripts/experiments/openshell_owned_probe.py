# /// script
# requires-python = ">=3.11"
# dependencies = ["openshell==0.1.2", "grpcio==1.84.0"]
# ///
"""Exercise owned execution in a disposable Docker-host-network OpenShell fixture."""

from __future__ import annotations

import argparse
import asyncio
import http.client
import importlib.metadata
import json
import re
import socket
import time
import uuid
from dataclasses import asdict
from importlib import import_module
from pathlib import Path
from urllib.parse import urlencode

from openshell_owned_exec import Event, execute_owned


class DockerConnection(http.client.HTTPConnection):
    """Read the independently managed Docker state through the fixture's socket."""

    def connect(self) -> None:
        self.sock = socket.socket(getattr(socket, "AF_UNIX"), socket.SOCK_STREAM)
        self.sock.settimeout(2)
        self.sock.connect("/var/run/docker.sock")


def docker_get(path: str):
    """Return Docker JSON, treating only an explicit 404 as absent."""
    connection = DockerConnection("localhost", timeout=2)
    try:
        connection.request("GET", "/v1.45" + path)
        response = connection.getresponse()
        data = response.read()
        if response.status == 404:
            return None
        if response.status != 200:
            raise RuntimeError(f"Docker observer HTTP {response.status}")
        return json.loads(data)
    finally:
        connection.close()


def inventory(sandbox_id: str) -> dict:
    """Select only this physical sandbox's containers and volumes."""
    query = urlencode({"filters": json.dumps({"label": [f"openshell.ai/sandbox-id={sandbox_id}"]})})
    containers = docker_get("/containers/json?all=true&" + query)
    volumes = docker_get("/volumes?" + query)
    if containers is None or volumes is None:
        raise RuntimeError("Docker inventory endpoint missing")
    return {"containers": containers, "volumes": volumes["Volumes"] or []}


def process_identity(pid: str) -> str | None:
    """Read a known host PID's start time to distinguish PID reuse."""
    try:
        return Path(f"/proc/{int(pid)}/stat").read_text().rsplit(")", 1)[1].split()[19]
    except FileNotFoundError:
        return None


def snapshot(sandbox_id: str) -> dict:
    """Capture immutable container identities and independently observed workload processes."""
    result = inventory(sandbox_id)
    result["processes"] = {}
    result["top"] = []
    for container in result["containers"]:
        if container["Labels"].get("openshell.ai/isolation-role") == "sandbox":
            top = docker_get(f"/containers/{container['Id']}/top?ps_args=-eo%20pid,ppid,args")
            if top is None:
                raise RuntimeError("workload disappeared during process observation")
            result["top"].extend(top["Processes"])
            for row in top["Processes"]:
                result["processes"][row[0]] = process_identity(row[0])
    return result


class RawStream:
    """Consume protobuf events without the convenience SDK's retained chunk lists."""

    def __init__(self, call) -> None:
        self.call = call

    def cancel(self):
        return self.call.cancel()

    async def __aiter__(self):
        async for event in self.call:
            kind = event.WhichOneof("payload")
            if kind == "exit":
                yield Event("exit", exit_code=event.exit.exit_code)
            elif kind in ("stdout", "stderr"):
                yield Event(kind, data=bytes(getattr(event, kind).data))
            else:
                raise RuntimeError("unknown transport event")


async def run_case(args, case: str) -> dict:
    """Measure one single-use sandbox, retaining ownership on any unverified cleanup."""
    grpc = import_module("grpc")
    sdk = import_module("openshell.sandbox")
    pb = import_module("openshell._proto.openshell_pb2")
    bindings = import_module("openshell._proto.openshell_pb2_grpc")
    dm = import_module("openshell._proto.datamodel_pb2")
    policy_pb = import_module("openshell._proto.sandbox_pb2")
    scope = dm.WorkspaceSelector(workspace="default")
    name = "s1797-" + uuid.uuid4().hex[:12]
    client = sdk.SandboxClient(args.endpoint, timeout=20)
    policy = policy_pb.SandboxPolicy(version=1)
    policy.filesystem.read_only.extend(["/usr", "/bin", "/lib", "/lib64", "/etc", "/proc"])
    policy.filesystem.read_write.extend(["/sandbox", "/tmp", "/home/sandbox", "/dev/null"])
    policy.filesystem.include_workdir = True
    policy.landlock.compatibility = "hard_requirement"
    policy.process.run_as_user = policy.process.run_as_group = "sandbox"
    spec = pb.SandboxSpec(policy=policy, command=["sleep", "3600"])
    spec.template.image = args.image
    spec.template.labels["spike.issue"] = "1797"
    created = None
    owned = False
    try:
        health = await asyncio.to_thread(client.health)
        if health.version != "0.1.2":
            raise RuntimeError("requires gateway 0.1.2")
        created = await asyncio.to_thread(client.create, name=name, workspace="default", spec=spec)
        print(json.dumps({"created": name, "sandbox_id": created.id, "case": case}), flush=True)
        await asyncio.to_thread(client.wait_ready, name, workspace="default", timeout_seconds=60)
        initial = await asyncio.to_thread(snapshot, created.id)
        if (
            len(initial["containers"]) != 2
            or not initial["processes"]
            or any(x is None for x in initial["processes"].values())
        ):
            raise RuntimeError(
                "observer requires the Docker host PID namespace and two fixture containers"
            )
        child = "import time; time.sleep(60)"
        parent = (
            "import subprocess,time; subprocess.Popen(['python','-c',"
            + repr(child)
            + "]); time.sleep(60)"
        )
        codes = {
            "bytes": "import os; os.write(1,b'\\x00\\xff\\xfe\\x80'); os.write(2,b'\\xfe\\x00\\xff')",
            "natural_124": "raise SystemExit(124)",
            "deadline_child": parent,
            "cancel_child": parent,
            "disconnect_child": parent,
            "cleanup_timeout": parent,
            "cap_exact": "import os; os.write(1,b'a'*2048); os.write(2,b'b'*2048)",
            "cap_over": "import os,time; os.write(1,b'a'*2048); os.write(2,b'b'*2048); os.write(1,b'x'); time.sleep(60)",
            "message_limit": "import os,time; os.write(1,b'a'*8192); time.sleep(60)",
        }
        limit = 256 if case == "message_limit" else 65536
        async with grpc.aio.insecure_channel(
            args.endpoint, options=[("grpc.max_receive_message_length", limit)]
        ) as channel:
            stub = bindings.OpenShellStub(channel)
            request = pb.ExecSandboxRequest(
                workspace_scope=scope,
                sandbox=name,
                command=["python", "-c", codes[case]],
                workdir="/sandbox",
                no_login_shell=True,
                request_id=str(uuid.uuid4()),
            )
            # Cleanup responses must not inherit the deliberately tiny execution message limit.
            async with grpc.aio.insecure_channel(args.endpoint) as cleanup_channel:
                cleanup_stub = bindings.OpenShellStub(cleanup_channel)
                cleanup_entered = asyncio.Event()
                delay_cleanup = case == "cleanup_timeout"

                async def dispose():
                    cleanup_entered.set()
                    if delay_cleanup:
                        await asyncio.sleep(60)
                    before = await asyncio.to_thread(snapshot, created.id)
                    current = await cleanup_stub.GetSandbox(
                        pb.GetSandboxRequest(name=name, workspace_scope=scope), timeout=5
                    )
                    if current.sandbox.metadata.id != created.id:
                        raise RuntimeError("physical ownership changed before delete; refusing")
                    deleted = await cleanup_stub.DeleteSandbox(
                        pb.DeleteSandboxRequest(
                            name=name,
                            workspace_scope=scope,
                            allow_missing=True,
                            request_id=str(uuid.uuid4()),
                        ),
                        timeout=5,
                    )
                    if deleted.sandbox_id != created.id:
                        raise RuntimeError("delete targeted another physical identity")
                    while True:
                        after = await asyncio.to_thread(inventory, created.id)
                        identities = {**initial["processes"], **before["processes"]}
                        surviving = {
                            pid: started
                            for pid, started in identities.items()
                            if started is not None and process_identity(pid) == started
                        }
                        originals = [
                            await asyncio.to_thread(docker_get, f"/containers/{x['Id']}/json")
                            for x in initial["containers"]
                        ]
                        if (
                            not after["containers"]
                            and not after["volumes"]
                            and not surviving
                            and not any(originals)
                        ):
                            return {
                                "verified": True,
                                "before": before,
                                "after": after,
                                "observed_process_count": len(identities),
                                "delete_outcome": deleted.outcome,
                            }
                        await asyncio.sleep(0.05)

                task = asyncio.create_task(
                    execute_owned(
                        lambda: RawStream(stub.ExecSandbox(request)),
                        dispose,
                        owner={"name": name, "sandbox_id": created.id},
                        seconds=0.25 if case in ("deadline_child", "cleanup_timeout") else 10,
                        output_limit=65536 if case == "message_limit" else 4096,
                        cleanup_seconds=0.1 if case == "cleanup_timeout" else 20,
                    )
                )
                owned = True

                async def cancel_repeatedly():
                    await asyncio.sleep(0.25)
                    if case == "disconnect_child":
                        await channel.close()
                        return
                    task.cancel()
                    await cleanup_entered.wait()
                    for _ in range(3):
                        if task.done():
                            break
                        task.cancel()
                        await asyncio.sleep(0.01)

                canceller = (
                    asyncio.create_task(cancel_repeatedly())
                    if case in ("cancel_child", "disconnect_child")
                    else None
                )
                result = await task
                if canceller:
                    await canceller
                report = asdict(result)
                report["stdout_hex"] = report.pop("stdout").hex()
                report["stderr_hex"] = report.pop("stderr").hex()
                report.update(
                    case=case, image=args.image, request_id=request.request_id, message_limit=limit
                )
                if case == "cleanup_timeout":
                    report["retained_resources"] = await asyncio.to_thread(snapshot, created.id)
                    delay_cleanup = False
                    async with asyncio.timeout(20):
                        report["recovery"] = await dispose()
                return report
    finally:
        if not owned:
            deleted = await asyncio.to_thread(
                client.delete, name, workspace="default", allow_missing=True
            )
            await asyncio.to_thread(
                client.wait_deleted,
                name,
                workspace="default",
                expected_sandbox_id=deleted.sandbox_id,
                timeout_seconds=30,
            )
        client.close()


def main() -> int:
    """Run explicit live cases; JSON receipts report observations rather than suite conformance."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--endpoint", default="127.0.0.1:19797")
    parser.add_argument(
        "--case",
        choices=[
            "bytes",
            "natural_124",
            "deadline_child",
            "cancel_child",
            "disconnect_child",
            "cleanup_timeout",
            "cap_exact",
            "cap_over",
            "message_limit",
        ],
        required=True,
    )
    args = parser.parse_args()
    if not re.fullmatch(r"(?:[^\s]+@)?sha256:[0-9a-f]{64}", args.image):
        parser.error("image must be immutable")
    if not re.fullmatch(r"127\.0\.0\.1:[0-9]+", args.endpoint):
        parser.error("fixture requires a loopback endpoint")
    if importlib.metadata.version("openshell") != "0.1.2":
        raise RuntimeError("requires openshell 0.1.2")
    began = time.monotonic()
    result = asyncio.run(run_case(args, args.case))
    result["case_seconds"] = time.monotonic() - began
    print(json.dumps(result), flush=True)
    return int(not (result["cleanup_verified"] or result.get("recovery", {}).get("verified")))


if __name__ == "__main__":
    raise SystemExit(main())
