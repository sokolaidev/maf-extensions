# /// script
# requires-python = ">=3.11"
# dependencies = ["openshell==0.1.2", "grpcio==1.84.0"]
# ///
"""Characterize guest-authority file access in an isolated official OpenShell fixture."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import re
import subprocess
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

from openshell_allocation_probe import Client
from openshell_authority_probe import gateway_fixture, verify_absent
from openshell_owned_probe import snapshot
from openshell_replay_probe import wait_gateway

GUEST_CASES = {
    "binary_readback": "import os; os.write(1, open('/sandbox/nested/input.bin','rb').read())",
    "guest_can_overwrite": "from pathlib import Path; p=Path('/sandbox/nested/input.bin'); p.write_bytes(b'guest'); print(p.read_bytes()==b'guest')",
    "relative_workdir": "import os; print(os.getcwd())",
    "allowed_tmp_write": "from pathlib import Path; p=Path('/tmp/spike1797-value'); p.write_bytes(b'outside-base'); print(p.read_bytes()==b'outside-base')",
    "parent_traversal_to_tmp": "from pathlib import Path; print(Path('../tmp/spike1797-value').read_bytes()==b'outside-base')",
    "linked_parent_to_tmp": "from pathlib import Path; p=Path('/sandbox/linked-parent'); p.symlink_to('/tmp',target_is_directory=True); print((p/'spike1797-value').read_bytes()==b'outside-base')",
    "linked_file_to_tmp": "from pathlib import Path; p=Path('/sandbox/linked-file'); p.symlink_to('/tmp/spike1797-value'); p.write_bytes(b'via-link'); print(Path('/tmp/spike1797-value').read_bytes()==b'via-link')",
    "readonly_policy_read": "from pathlib import Path; print(Path('/etc/spike1797-readable').read_bytes()==b'readonly-sentinel')",
    "readonly_policy_write": "from pathlib import Path; Path('/etc/spike1797-readable').write_bytes(b'changed')",
    "outside_policy_read": "from pathlib import Path; Path('/opt/spike1797/sentinel').read_bytes()",
    "outside_policy_write": "from pathlib import Path; Path('/opt/spike1797/new-file').write_bytes(b'changed')",
    "linked_outside_policy_read": "from pathlib import Path; p=Path('/sandbox/denied-link'); p.symlink_to('/opt/spike1797/sentinel'); p.read_bytes()",
    "readonly_sentinel_unchanged": "from pathlib import Path; print(Path('/etc/spike1797-readable').read_bytes()==b'readonly-sentinel')",
}
DENIED = {
    "readonly_policy_write",
    "outside_policy_read",
    "outside_policy_write",
    "linked_outside_policy_read",
}
PAYLOAD = bytes(range(256)) * 16


def execute(client, name, code, *, workdir="/sandbox"):
    """Capture only small fixture results, refusing over-budget output without claiming termination."""
    request = client.pb.ExecSandboxRequest(
        workspace_scope=client.scope,
        sandbox=name,
        command=["python", "-c", code],
        workdir=workdir,
        no_login_shell=True,
        request_id=str(uuid.uuid4()),
    )
    call = client.stub.ExecSandbox(
        request, metadata=[("authorization", "Bearer " + client.token)], timeout=10
    )
    output, error, exit_code = bytearray(), bytearray(), None
    try:
        for event in call:
            kind = event.WhichOneof("payload")
            if kind == "exit":
                if exit_code is not None:
                    raise RuntimeError("duplicate exit")
                exit_code = event.exit.exit_code
            elif kind in ("stdout", "stderr"):
                data = bytes(getattr(event, kind).data)
                if len(output) + len(error) + len(data) > 16384:
                    raise RuntimeError("fixture output cap exceeded")
                (output if kind == "stdout" else error).extend(data)
            else:
                raise RuntimeError("unknown stream event")
    finally:
        call.cancel()
    if exit_code is None:
        raise RuntimeError("missing execution exit")
    return exit_code, bytes(output), bytes(error)


def denied_code(code):
    """Report errno explicitly so missing paths cannot pass as policy denials."""
    return (
        "import json\ntry:\n "
        + code
        + "\nexcept OSError as e:\n print(json.dumps({'errno':e.errno}))\nelse:\n print(json.dumps({'errno':None}))"
    )


def evaluate(case, result):
    """Compare observations with the characterization, including intentional storage-base gaps."""
    status, output, error = result
    if status != 0 or error:
        return False
    if case in DENIED:
        return json.loads(output) == {"errno": 13}
    if case == "binary_readback":
        return output == PAYLOAD
    if case == "relative_workdir":
        return output == b"/sandbox/nested\n"
    return output == b"True\n"


def measure(image, issuer, *, native_probe=None):
    """Own one unique workspace/sandbox and independently verify its physical disposal."""
    workspace, name = ("s1797-" + uuid.uuid4().hex[:12] for _ in range(2))
    client = Client(
        workspace, issuer.token("admin", role="spike-admin", scopes="openshell:all"), image
    )
    record = None
    workspace_created = False
    results = []
    cleanup = False
    native_result = None
    workspace_absent = False
    try:
        wait_gateway(client)
        client.rpc("CreateWorkspace", client.pb.CreateWorkspaceRequest(name=workspace))
        workspace_created = True
        request = client.pb.CreateSandboxRequest.FromString(client.request(name, str(uuid.uuid4())))
        request.spec.template.labels["spike.probe"] = "files"
        response = client.rpc("CreateSandbox", request)
        record = {
            "name": name,
            "id": response.sandbox.metadata.id,
            "observed": {"containers": [], "processes": {}},
        }
        print(
            json.dumps({"created": name, "workspace": workspace, "sandbox_id": record["id"]}),
            flush=True,
        )
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline:
            state = client.rpc(
                "GetSandbox", client.pb.GetSandboxRequest(name=name, workspace_scope=client.scope)
            ).sandbox
            if state.metadata.id != record["id"]:
                raise RuntimeError("fixture instance replaced")
            if state.status.phase == client.pb.SANDBOX_PHASE_READY:
                break
            time.sleep(0.1)
        else:
            raise RuntimeError("fixture did not become ready")
        record["observed"] = snapshot(record["id"])
        if (
            len(record["observed"]["containers"]) != 2
            or not record["observed"]["processes"]
            or any(v is None for v in record["observed"]["processes"].values())
        ):
            raise RuntimeError("requires independent Docker host process observation")
        print(json.dumps({"physical_identity": record}), flush=True)
        encoded = base64.b64encode(PAYLOAD).decode()
        setup = (
            "import base64; from pathlib import Path; p=Path('/sandbox/nested/input.bin'); p.parent.mkdir(); p.write_bytes(base64.b64decode("
            + repr(encoded)
            + ")); print(p.read_bytes()==base64.b64decode("
            + repr(encoded)
            + "))"
        )
        for case, code in {"binary_write": setup, **GUEST_CASES}.items():
            if case in DENIED:
                code = denied_code(code)
            result = execute(
                client,
                name,
                code,
                workdir="/sandbox/nested" if case == "relative_workdir" else "/sandbox",
            )
            passed = evaluate(case, result)
            item = {
                "case": case,
                "passed": passed,
                "exit_code": result[0],
                "stdout_size": len(result[1]),
                "stdout_sha256": hashlib.sha256(result[1]).hexdigest(),
                "stderr": result[2].decode(errors="replace"),
            }
            if case != "binary_readback":
                item["stdout"] = result[1].decode(errors="replace")
            results.append(item)
            print(json.dumps(item), flush=True)
            if not passed:
                raise RuntimeError("unexpected characterization: " + case)
        if native_probe is not None:
            native_result = native_probe(client, name, record["id"])
            if not native_result["passed"]:
                raise RuntimeError("native file characterization failed")
    finally:
        try:
            if record:
                if client.current_id(name) != record["id"]:
                    raise RuntimeError("refuse name deletion of unknown/replaced fixture")
                client.delete(name, str(uuid.uuid4()))
                cleanup = verify_absent(record)
                if not cleanup:
                    raise RuntimeError("physical cleanup incomplete; retain recorded identities")
            if workspace_created:
                client.rpc(
                    "DeleteWorkspace",
                    client.pb.DeleteWorkspaceRequest(name=workspace, allow_missing=True),
                )
                try:
                    client.rpc("GetWorkspace", client.pb.GetWorkspaceRequest(name=workspace))
                except client.grpc.RpcError as exc:
                    workspace_absent = exc.code() == client.grpc.StatusCode.NOT_FOUND
                if not workspace_absent:
                    raise RuntimeError("workspace cleanup incomplete")
        finally:
            client.close()
            print(
                json.dumps(
                    {
                        "physical_cleanup_verified": cleanup,
                        "workspace_cleanup_verified": workspace_absent,
                    }
                ),
                flush=True,
            )
    return {
        "passed": len(results) == 14 and all(x["passed"] for x in results),
        "cases": len(results),
        "native_sftp": native_result,
        "file_transport": "exec baseline uses small base64 input and raw output; native SFTP observations are separate",
        "storage_base_confined": False,
        "image": image,
    }


@contextmanager
def signed_gateway(config_path):
    """Own the disposable fixture signing material, issuer and gateway state."""
    with TemporaryDirectory(prefix="files-gateway-") as temporary:
        root = Path(temporary)
        private, public, kid = (root / x for x in ("private.pem", "public.pem", "kid"))
        subprocess.run(
            ["openssl", "genpkey", "-algorithm", "ED25519", "-out", str(private)],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["openssl", "pkey", "-in", str(private), "-pubout", "-out", str(public)],
            check=True,
            capture_output=True,
        )
        kid.write_text(str(uuid.uuid4()))
        config = config_path.read_text(encoding="utf-8")
        for old, new in (
            ("/tmp/spike-signing.pem", private),
            ("/tmp/spike-public.pem", public),
            ("/tmp/spike-kid", kid),
        ):
            if old not in config:
                raise ValueError("requires the pinned disposable gateway configuration")
            config = config.replace(old, str(new))
        configured = root / "gateway.toml"
        configured.write_text(config)
        with gateway_fixture(configured) as issuer:
            yield issuer


def main(*, native_probe=None):
    """Require an immutable sentinel fixture image and the isolated local gateway configuration."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    if not re.fullmatch(r"(?:[^\s]+@)?sha256:[0-9a-f]{64}", args.image):
        parser.error("requires immutable sentinel fixture image")
    with signed_gateway(args.config) as issuer:
        report = measure(args.image, issuer, native_probe=native_probe)
        print(json.dumps(report), flush=True)
    return int(not report["passed"])


if __name__ == "__main__":
    raise SystemExit(main())
