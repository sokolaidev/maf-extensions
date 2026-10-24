# /// script
# requires-python = ">=3.11"
# dependencies = ["openshell==0.1.2", "grpcio==1.84.0"]
# ///
"""Characterize HTTP egress with a recording private origin and official OpenShell runtime."""

from __future__ import annotations

import argparse
import asyncio
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


def serve_origin(*, tls_events=None, record_authorization=False, revocation_controls=False):
    """Expose only fixture responses and a bounded request journal on the private Docker network."""
    events = []
    lock = threading.Lock()
    releases = {}

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1" if revocation_controls else "HTTP/1.0"

        def do_GET(self):
            self.handle_request()

        def do_POST(self):
            self.handle_request()

        def handle_request(self):
            parsed = urlsplit(self.path)
            marker = parse_qs(parsed.query).get("case", [""])[0]
            if revocation_controls and parsed.path == "/__release":
                with lock:
                    release = releases.get(marker)
                if release is None:
                    self.send_error(404)
                    return
                release.set()
                body, status = b'{"released":true}', 200
            elif self.path == "/__tls_events" and tls_events is not None:
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
                    if revocation_controls and parsed.path == "/allowed/hold":
                        releases[marker] = threading.Event()
                if revocation_controls and parsed.path == "/allowed/hold":
                    if not releases[marker].wait(60):
                        self.send_error(504, "fixture barrier not released")
                        return
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


def native_origin_ip(value):
    """Accept only an owned private IPv4 Docker origin for the native fixture."""
    address = ipaddress.IPv4Address(value)
    if not address.is_private or address.is_loopback or address.is_link_local:
        raise ValueError("requires owned private Docker origin")
    return str(address)


def rule_plan(rules, *, providers=()):
    """Translate a bounded suite-rule subset for the fixture's fixed HTTP port only."""
    from maf_sandbox import EgressRule

    if providers:
        raise ValueError("provider authority is not representable by base rules alone")
    result, hosts = [], set()
    for rule in rules:
        if not isinstance(rule, EgressRule) or rule.authority is not None:
            raise ValueError("requires explicit uncredentialed EgressRule values")
        host = rule.host.lower()
        if host in hosts:
            raise ValueError("duplicate host rules require explicit conflict resolution")
        hosts.add(host)
        if not rule.methods or set(rule.methods) - {"GET", "POST"} or not rule.paths:
            raise ValueError("fixture requires explicit GET/POST methods and paths")
        paths = []
        for path in rule.paths:
            if not re.fullmatch(r"/[A-Za-z0-9/_-]*(?:\*)?", path) or "//" in path:
                raise ValueError("unqualified path encoding or pattern")
            if path.endswith("/*"):
                paths.extend([path[:-2] or "/", path[:-1] + "**"])
            else:
                paths.append(path)
        result.append(
            {
                "host": "**." + host[2:] if host.startswith("*.") else host,
                "methods": list(rule.methods),
                "paths": list(dict.fromkeys(paths)),
            }
        )
    return result


def fixture_policy(image, rules, ip, *, providers=()):
    """Compile the entire validated plan with explicit blocking L7 enforcement."""
    from openshell_owned_probe import fixture_spec

    plan = rule_plan(rules, providers=providers)
    pb = import_module("openshell._proto.sandbox_pb2")
    policy = fixture_spec(image).policy
    for index, item in enumerate(plan):
        rule = policy.network_policies["native_" + str(index)]
        rule.name = "native_" + str(index)
        rule.binaries.add(path="/usr/local/bin/python*")
        endpoint = rule.endpoints.add(
            host=item["host"], port=8000, protocol="rest", allowed_ips=[native_origin_ip(ip)]
        )
        endpoint.enforcement = pb.NETWORK_ENFORCEMENT_MODE_ENFORCE
        for method in item["methods"]:
            for path in item["paths"]:
                endpoint.rules.add().allow.CopyFrom(pb.L7Allow(method=method, path=path))
    return policy


def inflight_observation(observations, hits, first):
    """Attribute both responses to the held request and its same-client follow-up."""
    second = dict(first, path=first["path"].replace("/hold?", "/after?"))
    if len(observations) != 2 or hits not in ([first], [first, second]):
        raise RuntimeError("in-flight origin attribution incomplete")
    completed = matches_observation(observations[0], [first], 200, 1, first)
    if not completed and observations[0].get("error") not in (
        "RemoteDisconnected",
        "ConnectionResetError",
        "BrokenPipeError",
    ):
        raise RuntimeError("held response has no attributable completion or connection closure")
    later = len(hits) == 2
    if later:
        if not matches_observation(observations[1], [second], 200, 1, second):
            raise RuntimeError("follow-up reached origin without matching response")
        outcome = "reached_origin"
    elif (
        matches_observation(observations[1], [], 403, 0, second)
        or observations[1].get("errno") == 13
    ):
        outcome = "explicit_policy_refusal"
    elif observations[1].get("error") in (
        "RemoteDisconnected",
        "ConnectionResetError",
        "BrokenPipeError",
    ):
        outcome = "connection_closed; policy refusal not established"
    else:
        raise RuntimeError("follow-up outcome unattributed")
    return {
        "guest": observations,
        "origin_hits": hits,
        "response_completed_after_loaded": completed,
        "later_request_reached_origin": later,
        "later_outcome": outcome,
        "same_connection": bool(observations[0].get("connection_generation"))
        and observations[0]["connection_generation"]
        == observations[1].get("connection_generation"),
        "instant_revocation_qualified": False,
    }


def persistent_request_code(url):
    """Use native hostname connections and record any automatic reconnect."""
    return (
        "import http.client,json,urllib.parse\n"
        "class TrackedConnection(http.client.HTTPConnection):\n"
        " generation=0\n def connect(self):\n  self.generation+=1; super().connect()\n"
        "p=urllib.parse.urlsplit("
        + repr(url)
        + "); c=TrackedConnection(p.hostname,p.port,timeout=40); results=[]\n"
        "for url in " + repr([url, url.replace("/hold?", "/after?")]) + ":\n"
        " try:\n  p=urllib.parse.urlsplit(url); c.request('GET',p.path+'?'+p.query,headers={'Host':p.netloc}); r=c.getresponse(); results.append({'status':r.status,'body':r.read(4096).decode()})\n"
        " except Exception as e:\n  results.append({'error':type(e).__name__,'errno':getattr(e,'errno',None)})\n"
        " results[-1]['connection_generation']=c.generation\n"
        "c.close(); print(json.dumps(results))"
    )


async def measure_native(fixture, ip):
    """Measure translated HTTP rules and a held request across an installed policy change."""
    from maf_sandbox import EgressRule
    from openshell_composition_probe import revision_installed

    ip = native_origin_ip(ip)
    client, report = fixture.client, fixture.report
    allowed, denied, wildcard = (
        label + "." + ip + ".nip.io" for label in ("allowed", "denied", "wild")
    )
    rules = (
        EgressRule(allowed, methods=("GET",), paths=("/allowed/*",)),
        EgressRule("*." + wildcard, methods=("GET",), paths=("/allowed/*",)),
    )
    report["egress"] = {
        "scope": "provider-free fixed IPv4 HTTP port 8000; not a supported suite egress mode",
        "rule_plan": rule_plan(rules),
        "origin_ip": ip,
        "cases": [],
        "unqualified": [
            "HTTPS owned trust",
            "IPv6",
            "DNS rebinding",
            "UDP",
            "provider composition",
            "concurrent controllers",
            "restart",
        ],
    }
    evidence = report["egress"]
    hosts = (allowed, denied, wildcard, "one." + wildcard, "two.one." + wildcard)
    for host in hosts:
        addresses = await asyncio.to_thread(socket.getaddrinfo, host, 8000, socket.AF_INET)
        if {address[4][0] for address in addresses} != {ip}:
            raise RuntimeError("fixture DNS did not resolve to the owned origin")
        for method in ("GET", "POST"):
            await asyncio.to_thread(origin_get, ip, "/blocked", method=method, host=host)
    await asyncio.to_thread(origin_get, ip, "/allowed", port=8001)
    sandbox = await fixture.acquire(policy=fixture_policy(client.image, rules, ip))

    async def journal(marker):
        rows = await asyncio.to_thread(origin_get, ip, "/__events")
        return [
            row for row in rows if parse_qs(urlsplit(row["path"]).query).get("case") == [marker]
        ]

    async def observe(code):
        response = await sandbox.exec(
            ["python", "-c", code], working_directory="/sandbox", timeout=45
        )
        if response.exit_code or response.stderr_bytes:
            raise RuntimeError("guest egress observation failed: " + response.stderr)
        return json.loads(response.stdout)

    async def check(label, host, path, expected, *, method="GET", port=8000, direct=False):
        marker = uuid.uuid4().hex
        url = f"http://{host}:{port}{path}?case={marker}"
        code = guest_request(url, method)
        if direct:
            code = (
                "import socket,json; s=socket.socket(); s.settimeout(3); print(json.dumps({'errno':s.connect_ex(("
                + repr(ip)
                + ",8000))})); s.close()"
            )
        observed = await observe(code)
        hits = await journal(marker)
        request = {
            "method": method,
            "path": path + "?case=" + marker,
            "host": host + ":" + str(port),
            "port": port,
        }
        expected_hits = int(expected == 200 or label == "redirect_denied")
        row = {
            "case": label,
            "guest": observed,
            "origin_hits": hits,
            "passed": matches_observation(observed, hits, expected, expected_hits, request),
        }
        evidence["cases"].append(row)
        fixture.save()
        if not row["passed"]:
            raise RuntimeError("unexpected native egress observation: " + label)

    async def update(selected):
        current = await asyncio.to_thread(
            client.rpc,
            "GetSandbox",
            client.pb.GetSandboxRequest(name=sandbox.owner["name"], workspace_scope=client.scope),
        )
        if current.sandbox.metadata.id != sandbox.instance_id:
            raise RuntimeError("refuse replaced fixture")
        response = await asyncio.to_thread(
            client.rpc,
            "UpdateConfig",
            client.pb.UpdateConfigRequest(
                sandbox=sandbox.owner["name"],
                workspace_scope=client.scope,
                policy=fixture_policy(client.image, selected, ip),
                expected_resource_version=current.sandbox.metadata.resource_version,
                request_id=str(uuid.uuid4()),
            ),
        )
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            status = await asyncio.to_thread(
                client.rpc,
                "GetSandboxPolicyStatus",
                client.pb.GetSandboxPolicyStatusRequest(
                    sandbox=sandbox.owner["name"],
                    workspace_scope=client.scope,
                    version=response.version,
                ),
            )
            if revision_installed(status, response, client.pb.POLICY_STATUS_LOADED):
                evidence.setdefault("loaded_revisions", []).append(
                    {"version": response.version, "policy_hash": response.policy_hash}
                )
                fixture.save()
                return
            await asyncio.sleep(0.1)
        raise RuntimeError("policy installation unconfirmed")

    try:
        for label, host, path, expected in (
            ("allowed_parent", allowed, "/allowed", 200),
            ("allowed_child", allowed, "/allowed/child", 200),
            ("allowed_deep_child", allowed, "/allowed/a/b", 200),
            ("denied_prefix", allowed, "/allowedness", 403),
            ("denied_path", allowed, "/blocked", 403),
            ("denied_host", denied, "/allowed", "EACCES"),
            ("wildcard_apex_refused", wildcard, "/allowed", "EACCES"),
            ("wildcard_one_label", "one." + wildcard, "/allowed", 200),
            ("wildcard_deep_label", "two.one." + wildcard, "/allowed", 200),
            ("redirect_denied", allowed, "/allowed/redirect", "EACCES"),
            ("uppercase_host", allowed.upper(), "/allowed", 200),
        ):
            await check(label, host, path, expected)
        await check("denied_post", allowed, "/allowed", 403, method="POST")
        await check("denied_port", allowed, "/allowed", "EACCES", port=8001)
        await check("direct_socket_refused", allowed, "/allowed", "EACCES", direct=True)
        marker = uuid.uuid4().hex
        url = f"http://{allowed}:8000/allowed/hold?case={marker}"
        first_request = {
            "method": "GET",
            "path": "/allowed/hold?case=" + marker,
            "host": allowed + ":8000",
            "port": 8000,
        }
        pending = asyncio.create_task(observe(persistent_request_code(url)))
        try:
            deadline = time.monotonic() + 15
            while not await journal(marker):
                if pending.done():
                    raise RuntimeError("held request never reached origin: " + str(await pending))
                if time.monotonic() >= deadline:
                    raise RuntimeError("origin admission barrier unavailable")
                await asyncio.sleep(0.1)
            if await journal(marker) != [first_request]:
                raise RuntimeError("held request admission has incorrect origin attribution")
            await update(())
            await check("closed_fresh_http", allowed, "/allowed", "EACCES")
            await check("closed_direct_socket", allowed, "/allowed", "EACCES", direct=True)
            await asyncio.to_thread(origin_get, ip, "/__release?case=" + marker)
            observations = await pending
            hits = await journal(marker)
            evidence["in_flight"] = inflight_observation(observations, hits, first_request)
            fixture.save()
        finally:
            if not pending.done():
                pending.cancel()
                try:
                    await pending
                except asyncio.CancelledError:
                    # Observe the execution task's cancellation cleanup before final disposal.
                    pass
        await update(rules)
        await check("restored_allowed_control", allowed, "/allowed", 200)
    finally:
        await sandbox.dispose()


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
    parser.add_argument("--revocation-controls", action="store_true")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--image")
    parser.add_argument("--origin-ip")
    parser.add_argument("--config", type=Path)
    args = parser.parse_args()
    if args.serve_origin:
        serve_origin(
            record_authorization=args.record_authorization,
            revocation_controls=args.revocation_controls,
        )
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
