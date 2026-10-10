# /// script
# requires-python = ">=3.11"
# dependencies = ["openshell==0.1.2", "grpcio==1.84.0"]
# ///
"""Measure workspace lifecycle authority with disposable loopback OIDC identities."""

from __future__ import annotations

import argparse
import base64
import json
import re
import subprocess
import threading
import time
import uuid
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import import_module
from pathlib import Path
from tempfile import TemporaryDirectory

from openshell_owned_probe import docker_get, fixture_spec, inventory, process_identity, snapshot


def encoded(data: bytes) -> str:
    """Encode JWT/JWK bytes without padding."""
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


class Issuer:
    """Serve only public discovery/JWKS; all signing material stays in the disposable fixture."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.key = root / "oidc-key.pem"
        self.kid = str(uuid.uuid4())
        subprocess.run(
            ["openssl", "genpkey", "-algorithm", "ED25519", "-out", str(self.key)],
            check=True,
            capture_output=True,
        )
        public = subprocess.check_output(
            ["openssl", "pkey", "-in", str(self.key), "-pubout", "-outform", "DER"]
        )
        if len(public) != 44 or public[:12].hex() != "302a300506032b6570032100":
            raise RuntimeError("unexpected Ed25519 public key encoding")
        jwks = {
            "keys": [
                {
                    "kty": "OKP",
                    "crv": "Ed25519",
                    "use": "sig",
                    "alg": "EdDSA",
                    "kid": self.kid,
                    "x": encoded(public[12:]),
                }
            ]
        }
        self.url = "http://127.0.0.1:19800"
        documents = {
            "/.well-known/openid-configuration": {
                "issuer": self.url,
                "jwks_uri": self.url + "/jwks",
            },
            "/jwks": jwks,
        }

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                document = documents.get(self.path)
                data = json.dumps(document).encode()
                self.send_response(200 if document else 404)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, format: str, *args) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 19800), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def token(
        self,
        subject: str,
        *,
        role: str = "spike-user",
        scopes: str = "sandbox:read sandbox:write workspace:read",
    ) -> str:
        """Mint one short-lived fixture identity without printing its credential."""
        header = encoded(json.dumps({"alg": "EdDSA", "kid": self.kid}).encode())
        claims = {
            "iss": self.url,
            "aud": "spike-1797",
            "sub": subject,
            "exp": int(time.time()) + 900,
            "roles": [role],
            "scope": scopes,
        }
        signing_input = (header + "." + encoded(json.dumps(claims).encode())).encode()
        message = self.root / "signing-input"
        message.write_bytes(signing_input)
        signature = subprocess.check_output(
            ["openssl", "pkeyutl", "-sign", "-rawin", "-inkey", str(self.key), "-in", str(message)]
        )
        message.unlink()
        return signing_input.decode() + "." + encoded(signature)

    def close(self) -> None:
        """Stop the fixture issuer before deleting its temporary key."""
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


def verify_present(record: dict) -> bool:
    """Require the same observed containers and processes to remain running."""
    containers = [
        docker_get(f"/containers/{c['Id']}/json") for c in record["observed"]["containers"]
    ]
    processes = record["observed"]["processes"]
    return (
        bool(containers)
        and all(c is not None and c["State"]["Running"] for c in containers)
        and bool(processes)
        and all(
            start is not None and process_identity(pid) == start for pid, start in processes.items()
        )
    )


def verify_absent(record: dict) -> bool:
    """Require independent absence of the captured physical workload and supervisor."""
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        now = inventory(record["id"])
        originals = [
            docker_get(f"/containers/{c['Id']}/json") for c in record["observed"]["containers"]
        ]
        alive = [
            pid
            for pid, start in record["observed"]["processes"].items()
            if process_identity(pid) == start
        ]
        if not now["containers"] and not now["volumes"] and not any(originals) and not alive:
            return True
        time.sleep(0.05)
    return False


def measure(image: str, issuer: Issuer) -> dict:
    """Compare authenticated principals while creating and deleting only fixture-owned resources."""
    grpc = import_module("grpc")
    pb = import_module("openshell._proto.openshell_pb2")
    dm = import_module("openshell._proto.datamodel_pb2")
    bindings = import_module("openshell._proto.openshell_pb2_grpc")
    channel = grpc.insecure_channel("127.0.0.1:19797")
    stub = bindings.OpenShellStub(channel)
    tokens = {who: issuer.token(who) for who in ("adapter", "outsider", "peer")}
    tokens["reader"] = issuer.token("reader", scopes="sandbox:read")
    tokens["adapter_broad"] = issuer.token("adapter", scopes="openshell:all")
    tokens["admin"] = issuer.token("admin", role="spike-admin", scopes="openshell:all")
    observations = []
    owned = []
    workspace = "s1797-" + uuid.uuid4().hex[:12]
    scope = dm.WorkspaceSelector(workspace=workspace)

    def rpc(method, request, who="admin"):
        metadata = [("authorization", "Bearer " + tokens[who])] if who else []
        return getattr(stub, method)(request, metadata=metadata, timeout=10)

    def check(label, method, request, who, expected):
        response = None
        try:
            response = rpc(method, request, who)
            status = "OK"
        except grpc.RpcError as exc:
            status = exc.code().name
        observations.append(
            {
                "case": label,
                "method": method,
                "status": status,
                "expected": expected,
                "passed": status == expected,
            }
        )
        if method == "DeleteSandbox" and expected != "OK" and owned:
            original_survived = verify_present(owned[0])
            observations[-1]["original_survived"] = original_survived
            observations[-1]["passed"] &= original_survived
        if method == "CreateSandbox" and response is not None:
            record = {
                "name": request.name,
                "id": response.sandbox.metadata.id,
                "observed": {"containers": [], "processes": {}},
            }
            owned.append(record)
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                state = rpc(
                    "GetSandbox", pb.GetSandboxRequest(name=request.name, workspace_scope=scope)
                )
                if state.sandbox.status.phase == pb.SANDBOX_PHASE_READY:
                    record["observed"] = snapshot(record["id"])
                    break
                time.sleep(0.1)
            else:
                raise RuntimeError("workload did not become ready")
        return response

    def create(name):
        return pb.CreateSandboxRequest(
            name=name, workspace_scope=scope, spec=fixture_spec(image), request_id=str(uuid.uuid4())
        )

    def delete(name):
        return pb.DeleteSandboxRequest(
            name=name, workspace_scope=scope, request_id=str(uuid.uuid4())
        )

    def member(subject):
        return pb.AddWorkspaceMemberRequest(
            workspace_scope=scope, principal_subject=subject, role=pb.WORKSPACE_ROLE_USER
        )

    report = {"observations": observations, "image": image, "workspace_cleanup_verified": False}
    try:
        deadline = time.monotonic() + 15
        while True:
            try:
                health = stub.Health(pb.HealthRequest(), timeout=1)
                if health.version != "0.1.2":
                    raise RuntimeError("requires gateway 0.1.2")
                break
            except grpc.RpcError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.1)
        rpc("CreateWorkspace", pb.CreateWorkspaceRequest(name=workspace))
        rpc("AddWorkspaceMember", member("adapter"))
        rpc("AddWorkspaceMember", member("reader"))
        primary = "s1797-" + uuid.uuid4().hex[:12]
        denied = "s1797-" + uuid.uuid4().hex[:12]
        check("adapter_create", "CreateSandbox", create(primary), "adapter", "OK")
        for who, create_status, delete_status in (
            (None, "UNAUTHENTICATED", "UNAUTHENTICATED"),
            ("outsider", "PERMISSION_DENIED", "NOT_FOUND"),
            ("reader", "PERMISSION_DENIED", "PERMISSION_DENIED"),
        ):
            check(
                f"{who or 'anonymous'}_create", "CreateSandbox", create(denied), who, create_status
            )
            check(
                f"{who or 'anonymous'}_delete", "DeleteSandbox", delete(primary), who, delete_status
            )
        check(
            "reader_read",
            "GetSandbox",
            pb.GetSandboxRequest(name=primary, workspace_scope=scope),
            "reader",
            "OK",
        )
        check(
            "adapter_role_cannot_add_writer_even_with_all_scopes",
            "AddWorkspaceMember",
            member("peer"),
            "adapter_broad",
            "PERMISSION_DENIED",
        )
        check("peer_before_membership", "DeleteSandbox", delete(primary), "peer", "NOT_FOUND")
        check("admin_adds_writer", "AddWorkspaceMember", member("peer"), "admin", "OK")
        check("peer_deletes_adapter_instance", "DeleteSandbox", delete(primary), "peer", "OK")
        if not verify_absent(owned[0]):
            raise RuntimeError("peer deletion did not remove original")
        check("peer_reuses_physical_name", "CreateSandbox", create(primary), "peer", "OK")
        check("admin_without_membership_deletes", "DeleteSandbox", delete(primary), "admin", "OK")
        if not verify_absent(owned[-1]):
            raise RuntimeError("admin deletion did not remove replacement")
        check(
            "admin_removes_peer",
            "RemoveWorkspaceMember",
            pb.RemoveWorkspaceMemberRequest(workspace_scope=scope, principal_subject="peer"),
            "admin",
            "OK",
        )
        check(
            "revoked_peer_recreate", "CreateSandbox", create(primary), "peer", "PERMISSION_DENIED"
        )
        check("adapter_reuses_deleted_name", "CreateSandbox", create(primary), "adapter", "OK")
        report["distinct_physical_ids"] = len({r["id"] for r in owned}) == len(owned)
    finally:
        for record in reversed(owned):
            try:
                current = rpc(
                    "GetSandbox", pb.GetSandboxRequest(name=record["name"], workspace_scope=scope)
                )
            except grpc.RpcError as exc:
                if exc.code() != grpc.StatusCode.NOT_FOUND:
                    raise
            else:
                if current.sandbox.metadata.id == record["id"]:
                    rpc("DeleteSandbox", delete(record["name"]))
            if not verify_absent(record):
                raise RuntimeError("independent resource cleanup incomplete")
        rpc("DeleteWorkspace", pb.DeleteWorkspaceRequest(name=workspace, allow_missing=True))
        try:
            rpc("GetWorkspace", pb.GetWorkspaceRequest(name=workspace))
        except grpc.RpcError as exc:
            report["workspace_cleanup_verified"] = exc.code() == grpc.StatusCode.NOT_FOUND
        channel.close()
    report["physical_cleanup_verified"] = True
    report["passed"] = (
        bool(observations)
        and all(x["passed"] for x in observations)
        and report["workspace_cleanup_verified"]
        and report.get("distinct_physical_ids", False)
    )
    return report


class GatewayProcess:
    """Restart only this disposable gateway while preserving its database and signing state."""

    def __init__(self, command, log):
        self.command, self.log = command, log
        self.process = None
        self.start()

    def start(self):
        if self.process is not None and self.process.poll() is None:
            raise RuntimeError("fixture gateway already running")
        self.process = subprocess.Popen(self.command, stdout=self.log, stderr=self.log)

    def stop(self):
        if self.process is None or self.process.poll() is not None:
            return
        self.process.terminate()
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=5)


@contextmanager
def gateway_fixture(config_path: Path, *, control: dict | None = None):
    """Own an isolated loopback OIDC gateway and discard its signing/database state on exit."""
    with TemporaryDirectory(prefix="openshell-authority-") as temporary:
        root = Path(temporary)
        issuer = Issuer(root)
        try:
            config = config_path.read_text(encoding="utf-8").replace(
                "allow_unauthenticated_users = true", "allow_unauthenticated_users = false"
            )
            config += '\n[openshell.gateway.oidc]\nissuer = "http://127.0.0.1:19800"\naudience = "spike-1797"\ndangerously_allow_insecure_http = true\nroles_claim = "roles"\nadmin_role = "spike-admin"\nuser_role = "spike-user"\nscopes_claim = "scope"\n'
            configured = root / "gateway.toml"
            configured.write_text(config)
            with (root / "gateway.log").open("wb") as log:
                gateway = GatewayProcess(
                    [
                        "/opt/openshell-gateway",
                        "--config",
                        str(configured),
                        "--disable-tls",
                        "--bind-address",
                        "127.0.0.1",
                        "--port",
                        "19797",
                        "--health-port",
                        "19798",
                        "--db-url",
                        f"sqlite://{root}/gateway.db?mode=rwc",
                    ],
                    log,
                )
                if control is not None:
                    control.update(gateway=gateway, database=root / "gateway.db")
                try:
                    yield issuer
                finally:
                    gateway.stop()
        finally:
            issuer.close()


def main() -> int:
    """Start an isolated issuer and gateway, measure permissions, then remove temporary secrets."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    if not re.fullmatch(r"(?:[^\s]+@)?sha256:[0-9a-f]{64}", args.image):
        parser.error("requires immutable image")
    with gateway_fixture(args.config) as issuer:
        report = measure(args.image, issuer)
        print(json.dumps(report), flush=True)
    return int(not report["passed"])


if __name__ == "__main__":
    raise SystemExit(main())
