"""Characterize raw HTTP normalization and alternate routes against an owned native origin."""

from __future__ import annotations

import asyncio
import hashlib
import http.client
import inspect
import json
import socket
import uuid

from openshell_egress_probe import fixture_policy, matches_observation, native_origin_ip, origin_get


def raw_request(destination, port, wire):
    """Send exact ASCII HTTP bytes without proxy environment or URL normalization."""
    connection = None
    phase = "connect"
    try:
        connection = socket.create_connection((destination, port), timeout=5)
        phase = "response"
        connection.sendall(wire.encode("ascii"))
        response = http.client.HTTPResponse(connection)
        response.begin()
        return {"status": response.status, "body": response.read(8192).decode("utf-8")}
    except Exception as exc:
        return {"error": type(exc).__name__, "errno": getattr(exc, "errno", None), "phase": phase}
    finally:
        if connection is not None:
            connection.close()


def guest_code(destination, port, wire):
    """Run the same raw client in the guest, independently of inherited proxy settings."""
    return (
        "import socket,http.client,json\n"
        + inspect.getsource(raw_request)
        + "\nprint(json.dumps(raw_request("
        + repr(destination)
        + ","
        + repr(port)
        + ","
        + repr(wire)
        + ")))"
    )


def route_cases(ip, *, closed=False):
    """Keep wire targets and expected origin paths separate, including explicit unqualified cases."""
    allowed, denied = (label + "." + ip + ".nip.io" for label in ("allowed", "denied"))
    rows = []

    def add(
        label,
        target,
        expected,
        *,
        forwarded=None,
        destination=allowed,
        host=None,
        method="GET",
        port=8000,
    ):
        rows.append(
            {
                "case": label,
                "target": target,
                "expected": expected,
                "forwarded": target if forwarded is None else forwarded,
                "destination": destination,
                "host": host or allowed + ":" + str(port),
                "method": method,
                "port": port,
            }
        )

    if not closed:
        add("allowed_control", "/allowed/child", 200)
        add("unreserved_decode", "/%61llowed/child", 200, forwarded="/allowed/child")
        add("dot_segment", "/allowed/./child", 200, forwarded="/allowed/child")
        add("duplicate_slash", "/allowed//child", 200, forwarded="/allowed/child")
        add("semicolon_parameter", "/allowed;param/child", 200, forwarded="/allowed/child")
        add("traversal_into_allowed", "/blocked/../allowed/child", 200, forwarded="/allowed/child")
        add("traversal_outside_allowed", "/allowed/../blocked", 403)
        add("encoded_traversal", "/allowed/%2e%2e/blocked", 403)
        add("semicolon_traversal", "/allowed/..;param/blocked", 403)
        add("path_case_sensitive", "/Allowed/child", 403)
        add("denied_method", "/allowed/child", 403, method="POST")
        add(
            "query_preserved",
            "/allowed/child?next=http%3A%2F%2Fdenied.invalid%2Fa&value=%252F",
            200,
        )
        add("encoded_slash", "/allowed%2fchild", "unqualified_rejection")
        add("invalid_percent", "/allowed/%zz", "unqualified_rejection")
        add(
            "absolute_form",
            "http://" + allowed + ":8000/allowed/./child",
            200,
            forwarded="/allowed/child",
        )
        add(
            "absolute_host_mismatch",
            "http://" + allowed + ":8000/allowed/child",
            "unqualified_rejection",
            host=denied + ":8000",
        )
        add("host_mismatch", "/allowed/child", "refused", host=denied + ":8000")
        add("host_port_mismatch", "/allowed/child", "refused", host=allowed + ":8001")
    add("direct_ipv4_with_allowed_host", "/allowed/child", "EACCES", destination=ip)
    add("direct_ipv4_literal_host", "/allowed/child", "EACCES", destination=ip, host=ip + ":8000")
    add("denied_hostname", "/allowed/child", "EACCES", destination=denied, host=denied + ":8000")
    add("alternate_port", "/allowed/child", "EACCES", port=8001)
    add(
        "closed_hostname" if closed else "final_allowed_control",
        "/allowed/child",
        "EACCES" if closed else 200,
    )
    return rows


def request_material(case, marker):
    """Bind a unique marker to the exact request bytes and expected independent origin record."""
    suffix = ("&" if "?" in case["target"] else "?") + "case=" + marker
    target = case["target"] + suffix
    wire = f"{case['method']} {target} HTTP/1.1\r\nHost: {case['host']}\r\nConnection: close\r\nContent-Length: 0\r\n\r\n"
    request = {
        "method": case["method"],
        "path": case["forwarded"] + suffix,
        "host": case["host"],
        "port": case["port"],
    }
    return wire, request


def route_observation(observed, hits, expected, request):
    """Do not turn a parser disconnect or arbitrary HTTP error into policy qualification."""
    if expected == 200 and matches_observation(observed, hits, 200, 1, request):
        return "forwarded_exactly"
    if expected in (403, "refused") and matches_observation(observed, hits, 403, 0, request):
        return "explicit_policy_refusal"
    if (
        expected in ("EACCES", "refused")
        and matches_observation(observed, hits, "EACCES", 0, request)
        and observed.get("phase") == "connect"
    ):
        return "connect_permission_refusal"
    if expected == "unqualified_rejection" and not hits:
        if observed.get("phase") == "response" and observed.get("error") in (
            "RemoteDisconnected",
            "ConnectionResetError",
        ):
            return "connection_closed_unqualified"
        if observed.get("status") == 400:
            return "http_bad_request_unqualified"
    raise RuntimeError("unexpected raw HTTP observation")


async def measure_routes(fixture, ip):
    """Run serial allowlist and empty-policy cases with origin controls and retained cleanup."""
    from maf_sandbox import EgressRule

    ip = native_origin_ip(ip)
    allowed = "allowed." + ip + ".nip.io"
    evidence = {
        "scope": "raw HTTP/1.1, provider-free fixed private IPv4 origin; proxy environment ignored",
        "cases": [],
        "controls": [],
        "qualification_passed": False,
        "unqualified": [
            "parser rejection attribution",
            "HTTPS",
            "IPv6",
            "UDP and DNS transport",
            "DNS rebinding",
            "provider authority",
            "concurrent transitions",
            "restart",
            "complete CLOSED contract",
        ],
    }
    fixture.report["routes"] = evidence
    fixture.save()
    for host in (allowed, "denied." + ip + ".nip.io"):
        addresses = await asyncio.to_thread(socket.getaddrinfo, host, 8000, socket.AF_INET)
        if {a[4][0] for a in addresses} != {ip}:
            raise RuntimeError("route fixture DNS did not resolve to owned origin")
    for closed in (False, True):
        cases = route_cases(ip, closed=closed)
        # Direct origin controls prove the wire bytes survive the client unchanged.
        for case in cases:
            marker = uuid.uuid4().hex
            wire, request = request_material(case, marker)
            request["path"] = (
                case["target"] + ("&" if "?" in case["target"] else "?") + "case=" + marker
            )
            observed = await asyncio.to_thread(raw_request, ip, case["port"], wire)
            before = await asyncio.to_thread(origin_get, ip, "/__events")
            hits = [hit for hit in before if marker in hit["path"]]
            if not matches_observation(observed, hits, 200, 1, request):
                raise RuntimeError("raw origin control failed: " + case["case"])
            evidence["controls"].append(
                {
                    "policy": "closed" if closed else "allowlist",
                    "case": case["case"],
                    "origin": request,
                }
            )
        rules = () if closed else (EgressRule(allowed, methods=("GET",), paths=("/allowed/*",)),)
        sandbox = await fixture.acquire(policy=fixture_policy(fixture.client.image, rules, ip))
        try:
            for case in cases:
                marker = uuid.uuid4().hex
                wire, request = request_material(case, marker)
                before = await asyncio.to_thread(origin_get, ip, "/__events")
                response = await sandbox.exec(
                    ["python", "-c", guest_code(case["destination"], case["port"], wire)],
                    working_directory="/sandbox",
                    timeout=30,
                )
                if response.exit_code or response.stderr_bytes:
                    raise RuntimeError("raw guest observation failed")
                observed = json.loads(response.stdout)
                after = await asyncio.to_thread(origin_get, ip, "/__events")
                if after[: len(before)] != before:
                    raise RuntimeError("origin journal changed outside append-only observation")
                hits = after[len(before) :]
                row = {
                    "policy": "closed" if closed else "allowlist",
                    **case,
                    "wire": wire,
                    "wire_sha256": hashlib.sha256(wire.encode("ascii")).hexdigest(),
                    "guest": observed,
                    "origin_hits": hits,
                    "observation": None,
                }
                evidence["cases"].append(row)
                fixture.save()
                row["observation"] = route_observation(observed, hits, case["expected"], request)
                fixture.save()
        finally:
            await sandbox.dispose()
    evidence["characterization_completed"] = True
    fixture.save()
