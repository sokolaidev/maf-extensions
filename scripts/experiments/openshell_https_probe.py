# /// script
# requires-python = ">=3.11"
# dependencies = ["openshell==0.1.2", "grpcio==1.84.0"]
# ///
"""Characterize HTTPS policy and origin verification with unchanged official images."""

from __future__ import annotations

import argparse
import http.client
import ipaddress
import json
import re
import ssl
import sys
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import import_module
from pathlib import Path

from openshell_egress_probe import (
    guest_request,
    matches_observation,
    origin_get,
    serve_origin,
)
from openshell_egress_probe import (
    measure as run_matrix,
)
from openshell_files_probe import execute, signed_gateway

CASES = 9
SCOPE = "public HTTPS echo and private untrusted origin; not complete egress conformance"
PUBLIC = "httpbingo.org"


def serve_tls(cert, key):
    """Run a TLS origin beside the independent plaintext journal/control server."""

    events = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if len(events) >= 1024:
                raise RuntimeError("TLS origin journal capacity exceeded")
            events.append({"path": self.path, "host": self.headers.get("Host")})
            body = b'{"private_tls_origin":true}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):
            pass

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)

    class TlsServer(ThreadingHTTPServer):
        def get_request(self):
            connection, address = self.socket.accept()
            connection.settimeout(3)
            try:
                return context.wrap_socket(connection, server_side=True), address
            except ssl.SSLError as exc:
                if len(events) >= 1024:
                    raise RuntimeError("TLS origin journal capacity exceeded") from exc
                events.append({"handshake_error": exc.reason})
                connection.close()
                raise

    server = TlsServer(("0.0.0.0", 8443), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    serve_origin(tls_events=events)


def control_https(host, path, *, method="GET", port=443, context=None):
    """Perform a certificate-verifying control outside the sandbox proxy."""
    connection = http.client.HTTPSConnection(host, port, timeout=8, context=context)
    try:
        connection.request(method, path, headers={"User-Agent": "OpenShell-spike-1797/1.0"})
        response = connection.getresponse()
        raw = response.read(16384)
        if response.status != 200:
            raise RuntimeError("HTTPS control did not succeed")
        return json.loads(raw)
    finally:
        connection.close()


def preflight(ip):
    """Require usable public TLS and prove the private certificate is the only control failure."""
    for method, path in (("GET", "/anything"), ("POST", "/anything"), ("GET", "/anything/blocked")):
        body = control_https(PUBLIC, path, method=method)
        if body.get("method") != method or body.get("url") != "https://" + PUBLIC + path:
            raise RuntimeError("unexpected public HTTPS control")
    host = "allowed." + ip + ".nip.io"
    try:
        control_https(host, "/", port=8443)
    except ssl.SSLCertVerificationError as exc:
        if exc.verify_code != 18:
            raise RuntimeError(
                "private control failed for an unexpected certificate reason"
            ) from exc
    else:
        raise RuntimeError("private origin unexpectedly trusted")
    context = ssl.create_default_context(cafile="/tmp/https-origin-cert.pem")
    if control_https(host, "/", port=8443, context=context) != {"private_tls_origin": True}:
        raise RuntimeError("private HTTPS positive control failed")
    print(
        json.dumps(
            {
                "https_controls": "public GET/POST/path verified; private self-signed refused and explicitly trusted control succeeded"
            }
        ),
        flush=True,
    )


def configure(request, ip, mode):
    """Require TLS inspection and explicit enforcement except in the audit control."""
    if mode == "closed":
        return
    pb = import_module("openshell._proto.sandbox_pb2")
    rule = request.spec.policy.network_policies["https_fixture"]
    rule.name = "https_fixture"
    rule.binaries.add(path="/usr/local/bin/python*")
    for host, port, ips in ((PUBLIC, 443, []), ("allowed." + ip + ".nip.io", 8443, [ip])):
        endpoint = rule.endpoints.add(host=host, port=port, protocol="rest", allowed_ips=ips)
        if mode == "enforce":
            endpoint.enforcement = pb.NETWORK_ENFORCEMENT_MODE_ENFORCE
        endpoint.rules.add().allow.CopyFrom(pb.L7Allow(method="GET", path="/anything"))


def matches_https(observed, expected, url, method, tls_events=()):
    """Reject incidental network errors and require origin echo or certificate-specific TLS alert evidence."""
    if expected in (403, "EACCES"):
        return matches_observation(observed, [], expected, 0, {})
    if expected == "certificate_refused":
        return observed.get("error") == "RemoteDisconnected" and list(tls_events) == [
            {"handshake_error": "SSLV3_ALERT_CERTIFICATE_UNKNOWN"}
        ]
    if observed.get("status") != 200:
        return False
    try:
        body = json.loads(observed.get("body", ""))
    except (TypeError, ValueError):
        return False
    return isinstance(body, dict) and body.get("url") == url and body.get("method") == method


def run_cases(client, name, owner, ip, mode):
    """Pair successful inspected HTTPS with denials and an independently diagnosed TLS failure."""
    results = []

    def check(
        label, host=PUBLIC, path="/anything", method="GET", expected: int | str = 200, port=443
    ):
        url = (
            f"https://{host}"
            + (f":{port}" if port != 443 else "")
            + path
            + "?case="
            + uuid.uuid4().hex
        )
        before = origin_get(ip, "/__tls_events")
        status, output, error = execute(client, name, guest_request(url, method))
        if status != 0 or error:
            raise RuntimeError("guest HTTPS observation failed")
        observed = json.loads(output)
        after = origin_get(ip, "/__tls_events")
        if after[: len(before)] != before:
            raise RuntimeError("TLS origin journal continuity lost")
        tls_events = after[len(before) :]
        passed = matches_https(observed, expected, url, method, tls_events)
        row = {
            "case": label,
            "passed": passed,
            "guest": observed,
            "private_origin_events": tls_events,
        }
        print(json.dumps(row), flush=True)
        results.append(row)
        if not passed:
            raise RuntimeError("unexpected HTTPS observation: " + label)

    if mode == "enforce":
        check("https_allowed_get")
        check("https_denied_post", method="POST", expected=403)
        check("https_denied_path", path="/anything/blocked", expected=403)
        check("https_denied_host", host="example.com", expected="EACCES")
        check(
            "https_untrusted_origin",
            host="allowed." + ip + ".nip.io",
            port=8443,
            expected="certificate_refused",
        )
        check("https_allowed_after_certificate_failure")
    elif mode == "audit":
        check("https_audit_post_reaches_origin", method="POST")
        check("https_audit_path_reaches_origin", path="/anything/blocked")
    else:
        check("closed_https_refused", expected="EACCES")
    return results


def main():
    """Run an owned origin or the HTTPS profile of the existing cleanup-verified harness."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serve-origin", action="store_true")
    parser.add_argument("--cert", type=Path)
    parser.add_argument("--key", type=Path)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--image")
    parser.add_argument("--origin-ip")
    parser.add_argument("--config", type=Path)
    args = parser.parse_args()
    if args.serve_origin:
        if not args.cert or not args.key:
            parser.error("origin requires --cert and --key")
        serve_tls(args.cert, args.key)
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
    profile = sys.modules[__name__]
    with signed_gateway(args.config) as issuer:
        report = run_matrix(args.image, issuer, str(ip), profile=profile)
        print(json.dumps(report), flush=True)
    return int(not report["passed"])


if __name__ == "__main__":
    raise SystemExit(main())
