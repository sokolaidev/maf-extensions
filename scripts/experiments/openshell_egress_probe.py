# /// script
# requires-python = ">=3.11"
# dependencies = ["openshell==0.1.2", "grpcio==1.84.0"]
# ///
"""Characterize HTTP egress with a recording private origin and official OpenShell runtime."""

from __future__ import annotations

import argparse
import hashlib
import http.client
import ipaddress
import json
import re
import socket
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import import_module
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from openshell_allocation_probe import Client
from openshell_authority_probe import verify_absent
from openshell_cleanup import Cleanup
from openshell_files_probe import execute, signed_gateway
from openshell_owned_probe import snapshot
from openshell_replay_probe import wait_gateway


def serve_origin(*, tls_events=None, record_authorization=False):
    """Expose only fixture responses and a bounded request journal on the private Docker network."""
    events = []
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.handle_request()

        def do_POST(self):
            self.handle_request()

        def handle_request(self):
            if self.path == "/__tls_events" and tls_events is not None:
                body = json.dumps(tls_events).encode()
                status = 200
            elif self.path == "/__events":
                with lock:
                    body = json.dumps(events).encode()
                status = 200
            else:
                length = min(int(self.headers.get("Content-Length", 0)), 1024)
                self.rfile.read(length)
                item = {
                    "method": self.command,
                    "path": self.path,
                    "host": self.headers.get("Host"),
                    "port": self.connection.getsockname()[1],
                }
                if record_authorization:
                    authorization = self.headers.get("Authorization")
                    item["authorization_sha256"] = (
                        hashlib.sha256(authorization.encode()).hexdigest()
                        if authorization is not None
                        else None
                    )
                with lock:
                    if len(events) >= 1024:
                        raise RuntimeError("fixture journal capacity exceeded")
                    events.append(item)
                status = 302 if urlsplit(self.path).path == "/allowed/redirect" else 200
                body = json.dumps(item).encode()
            self.send_response(status)
            if status == 302:
                location = (
                    "http://"
                    + self.headers["Host"].replace("allowed.", "denied.", 1)
                    + "/allowed"
                    + ("?" + urlsplit(self.path).query if urlsplit(self.path).query else "")
                )
                self.send_header("Location", location.replace("\r", "%0D").replace("\n", "%0A"))
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):
            pass

    second = ThreadingHTTPServer(("0.0.0.0", 8001), Handler)
    threading.Thread(target=second.serve_forever, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", 8000), Handler).serve_forever()


def origin_get(ip, path, *, method="GET", host=None, port=8000):
    """Query the independent recording origin, with no proxy environment involved."""
    connection = http.client.HTTPConnection(ip, port, timeout=3)
    try:
        connection.request(method, path, headers={"Host": host or ip})
        response = connection.getresponse()
        data = response.read(256 * 1024)
        if response.status != 200:
            raise RuntimeError("origin control failed")
        return json.loads(data)
    finally:
        connection.close()


def configure(request, ip, mode):
    """Select explicit L7 enforcement, its audit control, or an empty network policy."""
    if mode == "closed":
        return
    pb = import_module("openshell._proto.sandbox_pb2")
    rule = request.spec.policy.network_policies["fixture"]
    rule.name = "fixture"
    rule.binaries.add(path="/usr/local/bin/python*")
    hosts = [
        "allowed." + ip + ".nip.io",
        "*.wild." + ip + ".nip.io",
        "**.recursive." + ip + ".nip.io",
    ]
    for host in hosts:
        endpoint = rule.endpoints.add(host=host, port=8000, protocol="rest", allowed_ips=[ip])
        if mode == "enforce":
            endpoint.enforcement = pb.NETWORK_ENFORCEMENT_MODE_ENFORCE
        for path in ("/allowed", "/allowed/**"):
            endpoint.rules.add().allow.CopyFrom(pb.L7Allow(method="GET", path=path))


def guest_request(url, method):
    """Return status or explicit transport refusal; raw failures never count as policy success."""
    return (
        "import json,urllib.request,urllib.error\ntry:\n r=urllib.request.urlopen(urllib.request.Request("
        + repr(url)
        + ",method="
        + repr(method)
        + "),timeout=3); print(json.dumps({'status':r.status,'body':r.read(4096).decode()}))\nexcept urllib.error.HTTPError as e:\n print(json.dumps({'status':e.code,'body':e.read(4096).decode()}))\nexcept Exception as e:\n print(json.dumps({'error':type(e).__name__,'errno':getattr(getattr(e,'reason',e),'errno',None),'detail':str(e)}))"
    )


def matches_observation(observed, hits, expected_status, expected_hits, expected_request):
    """Require explicit refusal or echoed success plus the exact independent origin observations."""
    if len(hits) != expected_hits or any(hit != expected_request for hit in hits):
        return False
    if expected_status == "EACCES":
        return observed.get("errno") == 13
    if observed.get("status") != expected_status:
        return False
    try:
        body = json.loads(observed.get("body", ""))
    except (TypeError, ValueError):
        return False
    if expected_status == 403:
        return isinstance(body, dict) and body.get("error") == "policy_denied"
    return expected_status == 200 and body == expected_request


def measure(image, issuer, ip, *, profile=None):
    """Compare policy modes in owned sandboxes, optionally using another protocol profile."""
    workspace = "s1797-" + uuid.uuid4().hex[:12]
    client = Client(
        workspace, issuer.token("admin", role="spike-admin", scopes="openshell:all"), image
    )
    owners, results = [], []
    created_workspace = False
    allowed, denied = (label + "." + ip + ".nip.io" for label in ("allowed", "denied"))

    def check(
        name, label, host, path, method, expected_status, expected_hits, *, port=8000, direct=False
    ):
        case_id = uuid.uuid4().hex
        url = f"http://{host}:{port}{path}?case={case_id}"
        if direct:
            code = (
                "import socket,json; s=socket.socket(); s.settimeout(2); print(json.dumps({'errno':s.connect_ex(("
                + repr(ip)
                + ",8000))})); s.close()"
            )
        else:
            code = guest_request(url, method)
        status, output, error = execute(client, name, code)
        if status != 0 or error:
            raise RuntimeError("guest HTTP observation failed")
        observed = json.loads(output)
        hits = [
            x
            for x in origin_get(ip, "/__events")
            if parse_qs(urlsplit(x["path"]).query).get("case") == [case_id]
        ]
        expected_request = {
            "method": method,
            "path": path + "?case=" + case_id,
            "host": host + ":" + str(port),
            "port": port,
        }
        passed = matches_observation(
            observed, hits, "EACCES" if direct else expected_status, expected_hits, expected_request
        )
        row = {"case": label, "passed": passed, "guest": observed, "origin_hits": hits}
        results.append(row)
        print(json.dumps(row), flush=True)
        if not passed:
            raise RuntimeError("unexpected egress observation: " + label)

    try:
        wait_gateway(client)
        for host in (
            allowed,
            denied,
            "one.wild." + ip + ".nip.io",
            "two.one.wild." + ip + ".nip.io",
            "two.one.recursive." + ip + ".nip.io",
        ):
            if {x[4][0] for x in socket.getaddrinfo(host, 8000, family=socket.AF_INET)} != {ip}:
                raise RuntimeError("fixture DNS did not resolve to the owned origin")
        if profile is not None:
            profile.preflight(ip)
        for method in ("GET", "POST"):
            origin_get(ip, "/blocked", method=method, host=denied)
        origin_get(ip, "/allowed", port=8001)
        client.rpc("CreateWorkspace", client.pb.CreateWorkspaceRequest(name=workspace))
        created_workspace = True
        if profile is not None and hasattr(profile, "prepare_workspace"):
            profile.prepare_workspace(client)
        for mode in getattr(profile, "MODES", ("enforce", "audit", "closed")):
            name = "s1797-" + uuid.uuid4().hex[:12]
            request = client.pb.CreateSandboxRequest.FromString(
                client.request(name, str(uuid.uuid4()))
            )
            if profile is None:
                configure(request, ip, mode)
            else:
                profile.configure(request, ip, mode)
            request.spec.template.labels["spike.probe"] = "egress"
            response = client.rpc("CreateSandbox", request)
            owner = {
                "name": name,
                "id": response.sandbox.metadata.id,
                "observed": {"containers": [], "processes": {}},
            }
            owners.append(owner)
            print(
                json.dumps({"created": name, "sandbox_id": owner["id"], "mode": mode}), flush=True
            )
            deadline = time.monotonic() + 40
            while time.monotonic() < deadline:
                current = client.rpc(
                    "GetSandbox",
                    client.pb.GetSandboxRequest(name=name, workspace_scope=client.scope),
                ).sandbox
                if current.metadata.id != owner["id"]:
                    raise RuntimeError("fixture replaced")
                if current.status.phase == client.pb.SANDBOX_PHASE_READY:
                    break
                time.sleep(0.1)
            else:
                raise RuntimeError("fixture not ready")
            owner["observed"] = snapshot(owner["id"])
            if (
                len(owner["observed"]["containers"]) != 2
                or not owner["observed"]["processes"]
                or any(v is None for v in owner["observed"]["processes"].values())
            ):
                raise RuntimeError("independent process observer unavailable")
            print(json.dumps({"physical_identity": owner}), flush=True)
            if profile is not None:
                results.extend(profile.run_cases(client, name, owner, ip, mode))
            elif mode == "enforce":
                check(name, "allowed_get", allowed, "/allowed", "GET", 200, 1)
                check(name, "denied_post", allowed, "/allowed", "POST", 403, 0)
                check(name, "denied_path", allowed, "/blocked", "GET", 403, 0)
                check(name, "denied_host", denied, "/allowed", "GET", "EACCES", 0)
                check(name, "denied_port", allowed, "/allowed", "GET", "EACCES", 0, port=8001)
                check(name, "denied_path_prefix", allowed, "/allowedness", "GET", 403, 0)
                check(
                    name,
                    "wildcard_apex_refused",
                    "wild." + ip + ".nip.io",
                    "/allowed",
                    "GET",
                    "EACCES",
                    0,
                )
                check(
                    name,
                    "recursive_apex_refused",
                    "recursive." + ip + ".nip.io",
                    "/allowed",
                    "GET",
                    "EACCES",
                    0,
                )
                check(name, "subpath_allowed", allowed, "/allowed/child", "GET", 200, 1)
                check(name, "query_ignored", allowed, "/allowed", "GET", 200, 1)
                check(name, "redirect_denied", allowed, "/allowed/redirect", "GET", "EACCES", 1)
                check(
                    name,
                    "wildcard_one_label",
                    "one.wild." + ip + ".nip.io",
                    "/allowed",
                    "GET",
                    200,
                    1,
                )
                check(
                    name,
                    "wildcard_deep_refused",
                    "two.one.wild." + ip + ".nip.io",
                    "/allowed",
                    "GET",
                    "EACCES",
                    0,
                )
                check(
                    name,
                    "recursive_deep_allowed",
                    "two.one.recursive." + ip + ".nip.io",
                    "/allowed",
                    "GET",
                    200,
                    1,
                )
                check(
                    name, "direct_socket_refused", allowed, "/allowed", "GET", None, 0, direct=True
                )
            elif mode == "audit":
                check(name, "audit_post_reaches_origin", allowed, "/allowed", "POST", 200, 1)
                check(name, "audit_path_reaches_origin", allowed, "/blocked", "GET", 200, 1)
            else:
                check(name, "closed_http_refused", allowed, "/allowed", "GET", "EACCES", 0)
                check(
                    name, "closed_direct_refused", allowed, "/allowed", "GET", None, 0, direct=True
                )
    finally:
        with Cleanup(client.close) as cleanup:

            def dispose_record(owner):
                if client.current_id(owner["name"]) != owner["id"]:
                    raise RuntimeError("refuse replacement cleanup")
                if client.delete(owner["name"], str(uuid.uuid4())) != owner["id"]:
                    raise RuntimeError("fixture delete targeted another instance")
                if not verify_absent(owner):
                    raise RuntimeError("physical cleanup incomplete; retain identities")

            for owner in reversed(owners):
                cleanup.attempt(owner, dispose_record, owner)
            if not cleanup.errors and created_workspace:
                if profile is not None and hasattr(profile, "cleanup_workspace"):
                    profile.cleanup_workspace(client)
                client.rpc(
                    "DeleteWorkspace",
                    client.pb.DeleteWorkspaceRequest(name=workspace, allow_missing=True),
                )
                try:
                    client.rpc("GetWorkspace", client.pb.GetWorkspaceRequest(name=workspace))
                except client.grpc.RpcError as exc:
                    if exc.code() != client.grpc.StatusCode.NOT_FOUND:
                        raise
                else:
                    raise RuntimeError("workspace remains")
        print(
            json.dumps(
                {
                    "physical_cleanup_verified": True,
                    "workspace_cleanup_verified": created_workspace,
                }
            ),
            flush=True,
        )
    return {
        "passed": len(results) == (19 if profile is None else profile.CASES)
        and all(x["passed"] for x in results),
        "cases": len(results),
        "scope": "plaintext HTTP, provider-free fixed private origin; not complete egress conformance"
        if profile is None
        else profile.SCOPE,
    }


def main():
    """Run the owned-origin observer or an explicitly selected live gateway experiment."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serve-origin", action="store_true")
    parser.add_argument("--record-authorization", action="store_true")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--image")
    parser.add_argument("--origin-ip")
    parser.add_argument("--config", type=Path)
    args = parser.parse_args()
    if args.serve_origin:
        serve_origin(record_authorization=args.record_authorization)
        return 0
    if (
        not args.execute
        or not args.config
        or not args.image
        or not re.fullmatch(r"(?:[^\s]+@)?sha256:[0-9a-f]{64}", args.image)
    ):
        parser.error("requires --execute, --config and immutable --image")
    ip = ipaddress.IPv4Address(args.origin_ip)
    if not ip.is_private or ip.is_loopback or ip.is_link_local:
        parser.error("requires owned private Docker origin")
    with signed_gateway(args.config) as issuer:
        report = measure(args.image, issuer, str(ip))
        print(json.dumps(report), flush=True)
    return int(not report["passed"])


if __name__ == "__main__":
    raise SystemExit(main())
