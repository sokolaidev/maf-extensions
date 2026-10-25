# /// script
# requires-python = ">=3.11"
# dependencies = ["openshell==0.1.2", "grpcio==1.84.0", "cryptography==50.0.2"]
# ///
"""Characterize HTTPS policy and origin verification with unchanged official images."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import http.client
import ipaddress
import json
import re
import socket
import ssl
import sys
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import import_module
from pathlib import Path
from typing import cast

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


def prepare_native_tls(directory, ip):
    """Create short-lived test certificates; only the public CA enters the workload image."""
    from datetime import UTC, datetime, timedelta

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
    from openshell_egress_probe import native_origin_ip

    ip = native_origin_ip(ip)
    directory.mkdir(mode=0o700, parents=True, exist_ok=False)
    now = datetime.now(UTC)

    def authority(name):
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
        cert = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(days=1))
            .not_valid_after(now + timedelta(days=2))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(
                x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False
            )
            .add_extension(
                x509.KeyUsage(False, False, False, False, False, True, True, False, False),
                critical=True,
            )
            .sign(key, hashes.SHA256())
        )
        return key, cert

    trusted = authority("OpenShell ephemeral fixture CA")
    unknown = authority("OpenShell untrusted fixture CA")
    (directory / "ca.pem").write_bytes(trusted[1].public_bytes(serialization.Encoding.PEM))
    host = "allowed." + ip + ".nip.io"
    for label, dns, issuer, expired in (
        ("valid", host, trusted, False),
        ("hostname", "other." + ip + ".nip.io", trusted, False),
        ("expired", host, trusted, True),
        ("untrusted", host, unknown, False),
    ):
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cert = (
            x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, dns)]))
            .issuer_name(issuer[1].subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(days=2))
            .not_valid_after(now - timedelta(days=1) if expired else now + timedelta(days=1))
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(
                x509.AuthorityKeyIdentifier.from_issuer_public_key(issuer[0].public_key()),
                critical=False,
            )
            .add_extension(
                x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False
            )
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(dns)]), critical=False)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .sign(issuer[0], hashes.SHA256())
        )
        (directory / (label + ".pem")).write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        (directory / (label + ".key")).write_bytes(
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )
        (directory / (label + ".key")).chmod(0o600)


def native_tls_servers(directory, *, bind="127.0.0.1", ports=(8443, 8444, 8445, 8446)):
    """Serve four independently identified TLS endpoints with one bounded journal."""
    events = []
    lock = threading.Lock()

    def record(event):
        with lock:
            if len(events) >= 1024:
                raise RuntimeError("TLS origin journal capacity exceeded")
            events.append(event)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            row = {
                "method": self.command,
                "path": self.path,
                "host": self.headers.get("Host"),
                "port": cast(ThreadingHTTPServer, self.server).server_port,
            }
            record(row)
            body = json.dumps(row).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        do_POST = do_GET

        def log_message(self, format, *args):
            pass

    class Server(ThreadingHTTPServer):
        context: ssl.SSLContext

        def get_request(self):
            connection, address = self.socket.accept()
            connection.settimeout(3)
            try:
                return self.context.wrap_socket(connection, server_side=True), address
            except ssl.SSLError as exc:
                record({"handshake_error": exc.reason, "port": self.server_port})
                connection.close()
                raise

        def handle_error(self, request, client_address):
            pass

    servers = []
    try:
        for label, port in zip(("valid", "hostname", "expired", "untrusted"), ports, strict=True):
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(directory / (label + ".pem"), directory / (label + ".key"))
            server = Server((bind, port), Handler)
            server.context = context
            servers.append(server)
            threading.Thread(target=server.serve_forever, daemon=True).start()
    except BaseException:
        for server in servers:
            server.shutdown()
            server.server_close()
        raise
    return servers, events


def matches_native_https(observed, events, expected, request):
    """Require exact origin attribution, including certificate alerts without HTTP side effects."""
    if expected == "certificate_refused":
        return (
            observed.get("error") == "RemoteDisconnected"
            and len(events) == 1
            and events[0].get("port") == request["port"]
            and set(events[0]) == {"port", "handshake_error"}
            and events[0]["handshake_error"]
            in {
                "SSLV3_ALERT_CERTIFICATE_UNKNOWN",
                "SSLV3_ALERT_BAD_CERTIFICATE",
                "SSLV3_ALERT_CERTIFICATE_EXPIRED",
            }
        )
    return matches_observation(observed, events, expected, int(expected == 200), request)


def native_https_policy(image, host, ip, *, closed=False):
    """Select explicit inspection for the owned endpoints or an empty network policy."""
    from openshell_owned_probe import fixture_spec

    pb = import_module("openshell._proto.sandbox_pb2")
    policy = fixture_spec(image).policy
    if not closed:
        rule = policy.network_policies["native_https"]
        rule.name = "native_https"
        rule.binaries.add(path="/usr/local/bin/python*")
        for port in (8443, 8444, 8445, 8446):
            endpoint = rule.endpoints.add(host=host, port=port, protocol="rest", allowed_ips=[ip])
            endpoint.enforcement = pb.NETWORK_ENFORCEMENT_MODE_ENFORCE
            endpoint.rules.add().allow.CopyFrom(pb.L7Allow(method="GET", path="/anything"))
    return policy


def owned_trust_blocked(observed, events, request):
    """Identify the pinned supervisor's missing trust anchor without accepting incidental failures."""
    return observed.get("error") == "RemoteDisconnected" and events == [
        {"port": request["port"], "handshake_error": "TLSV1_ALERT_UNKNOWN_CA"}
    ]


async def measure_native_https(fixture, ip, ca_file):
    """Measure owned HTTPS with normal verification and independent origin evidence."""
    from openshell_egress_probe import native_origin_ip

    ip = native_origin_ip(ip)
    host = "allowed." + ip + ".nip.io"
    context = ssl.create_default_context(cafile=str(ca_file))
    evidence = {
        "scope": "provider-free IPv4 HTTPS with an ephemeral workload-image trust anchor",
        "ca_sha256": hashlib.sha256(ca_file.read_bytes()).hexdigest(),
        "leaf_sha256": {
            name: hashlib.sha256(ca_file.with_name(name + ".pem").read_bytes()).hexdigest()
            for name in ("valid", "hostname", "expired", "untrusted")
        },
        "controls": [],
        "cases": [],
        "qualification_passed": False,
        "unrun_cases": [],
        "unqualified": [
            "credential composition",
            "IPv6",
            "DNS rebinding",
            "revocation",
            "concurrent policy changes",
            "restart",
            "production trust management",
        ],
    }
    fixture.report["https"] = evidence
    fixture.save()
    for dns in (host, "other." + ip + ".nip.io"):
        addresses = await asyncio.to_thread(socket.getaddrinfo, dns, 8443, socket.AF_INET)
        if {a[4][0] for a in addresses} != {ip}:
            raise RuntimeError("TLS fixture DNS did not resolve to owned origin")
    for method, path in (("GET", "/anything"), ("POST", "/anything"), ("GET", "/blocked")):
        result = await asyncio.to_thread(
            control_https, host, path, method=method, port=8443, context=context
        )
        if result != {"method": method, "path": path, "host": host + ":8443", "port": 8443}:
            raise RuntimeError("owned HTTPS positive control failed")
        evidence["controls"].append(result)
    try:
        await asyncio.to_thread(control_https, host, "/anything", port=8443)
    except ssl.SSLCertVerificationError as exc:
        if exc.verify_code != 20:
            raise RuntimeError("unexpected default-trust failure") from exc
        evidence["controls"].append({"port": 8443, "default_trust_verify_code": 20})
    else:
        raise RuntimeError("fixture CA unexpectedly trusted without explicit installation")
    for port, code in ((8444, 62), (8445, 10), (8446, 20)):
        try:
            await asyncio.to_thread(control_https, host, "/anything", port=port, context=context)
        except ssl.SSLCertVerificationError as exc:
            if exc.verify_code != code:
                raise RuntimeError("unexpected independent certificate failure") from exc
            evidence["controls"].append({"port": port, "verify_code": code})
        else:
            raise RuntimeError("invalid certificate accepted by independent control")
    await asyncio.to_thread(
        control_https, "other." + ip + ".nip.io", "/anything", port=8444, context=context
    )
    fixture.save()
    for closed in (False, True):
        policy = native_https_policy(fixture.client.image, host, ip, closed=closed)
        sandbox = await fixture.acquire(policy=policy)
        try:
            if not closed:
                code = (
                    "import hashlib,json,os,pathlib; "
                    "ca=pathlib.Path('/usr/local/share/ca-certificates/openshell-fixture.crt').read_bytes(); "
                    "bundle=pathlib.Path('/etc/ssl/certs/ca-certificates.crt').read_bytes(); "
                    "effective=pathlib.Path(os.environ['SSL_CERT_FILE']).read_bytes(); "
                    "print(json.dumps({'installed_ca_sha256':hashlib.sha256(ca).hexdigest(),"
                    "'system_bundle_contains_ca':ca in bundle,'effective_bundle_contains_ca':ca in effective}))"
                )
                receipt = await sandbox.exec(
                    ["python", "-c", code], working_directory="/sandbox", timeout=30
                )
                if receipt.exit_code or receipt.stderr_bytes:
                    raise RuntimeError("cannot inspect workload trust material")
                evidence["workload_trust"] = json.loads(receipt.stdout)
                fixture.save()
                if (
                    evidence["workload_trust"]["installed_ca_sha256"] != evidence["ca_sha256"]
                    or not evidence["workload_trust"]["system_bundle_contains_ca"]
                ):
                    raise RuntimeError("fixture CA not installed in workload system bundle")
            cases = (
                [("closed_https", 8443, "GET", "/anything", "EACCES")]
                if closed
                else [
                    ("trusted_owned_get", 8443, "GET", "/anything", 200),
                    ("denied_method", 8443, "POST", "/anything", 403),
                    ("denied_path", 8443, "GET", "/blocked", 403),
                    ("wrong_hostname", 8444, "GET", "/anything", "certificate_refused"),
                    ("expired_certificate", 8445, "GET", "/anything", "certificate_refused"),
                    ("unknown_issuer", 8446, "GET", "/anything", "certificate_refused"),
                    ("trusted_after_failures", 8443, "GET", "/anything", 200),
                ]
            )
            for label, port, method, path, expected in cases:
                path += "?case=" + uuid.uuid4().hex
                url = f"https://{host}:{port}{path}"
                before = await asyncio.to_thread(origin_get, ip, "/__tls_events")
                response = await sandbox.exec(
                    ["python", "-c", guest_request(url, method)],
                    working_directory="/sandbox",
                    timeout=30,
                )
                if response.exit_code or response.stderr_bytes:
                    raise RuntimeError("guest TLS measurement failed: " + response.stderr)
                observed = json.loads(response.stdout)
                after = await asyncio.to_thread(origin_get, ip, "/__tls_events")
                if after[: len(before)] != before:
                    raise RuntimeError("TLS origin journal continuity lost")
                events = after[len(before) :]
                request = {"method": method, "path": path, "host": f"{host}:{port}", "port": port}
                row = {
                    "case": label,
                    "guest": observed,
                    "origin_events": events,
                    "passed": matches_native_https(observed, events, expected, request),
                }
                evidence["cases"].append(row)
                fixture.save()
                if not row["passed"]:
                    if label == "trusted_owned_get" and owned_trust_blocked(
                        observed, events, request
                    ):
                        row["finding"] = (
                            "workload CA is not trusted by the separate official supervisor"
                        )
                        evidence["blocked_by"] = (
                            "supervisor destination CA delivery; NVIDIA/OpenShell#3295"
                        )
                        evidence["unrun_cases"] = [case[0] for case in cases[1:]]
                        fixture.save()
                        break
                    raise RuntimeError("unexpected native HTTPS observation: " + label)
        finally:
            await fixture.dispose(sandbox.owner)
    evidence["qualification_passed"] = not evidence["unrun_cases"] and all(
        row["passed"] for row in evidence["cases"]
    )
    fixture.save()


def main():
    """Run an owned origin or the HTTPS profile of the existing cleanup-verified harness."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serve-origin", action="store_true")
    parser.add_argument("--prepare-native-tls", type=Path)
    parser.add_argument("--serve-native-tls", type=Path)
    parser.add_argument("--cert", type=Path)
    parser.add_argument("--key", type=Path)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--image")
    parser.add_argument("--origin-ip")
    parser.add_argument("--config", type=Path)
    args = parser.parse_args()
    if args.prepare_native_tls:
        prepare_native_tls(args.prepare_native_tls, args.origin_ip)
        return 0
    if args.serve_native_tls:
        servers, events = native_tls_servers(
            args.serve_native_tls,
            bind="0.0.0.0",  # nosec B104 - isolated Docker origin network
        )
        try:
            serve_origin(tls_events=events)
        finally:
            for server in servers:
                server.shutdown()
                server.server_close()
        return 0
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
