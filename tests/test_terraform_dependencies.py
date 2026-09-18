"""Request receiver controls and dependency preparation without running provider code."""

from __future__ import annotations

import ast
import base64
import copy
import datetime
import hashlib
import http.client
import io
import ipaddress
import json
import socket
import ssl
import stat
import subprocess
import sys
import threading
import time
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import cast

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import terraform_dependencies as prep  # noqa: E402


def bundle(files):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return output.getvalue()


def artifact(data=b"artifact", **extra):
    return {
        "url": "https://approved.example/repository/1.0/artifact.zip",
        "sha256": hashlib.sha256(data).hexdigest(),
        "provenance": "test-fixture-reviewed-digest",
        **extra,
    }


def manifest():
    return {
        "schema": 1,
        "engine": "terraform",
        "providers": [
            {
                "source": "registry.terraform.io/hashicorp/random",
                "version": "3.7.2",
                "platform": "linux_amd64",
                "artifact": artifact(),
            }
        ],
        "modules": [
            {
                "name": "example",
                "revision": "a" * 40,
                "subdir": "repo/module",
                "graph": {".": {"child": "child"}, "child": {}},
                "artifact": artifact(),
            }
        ],
    }


@pytest.fixture
def receiver(tmp_path, monkeypatch, request):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test receiver")])
    now = datetime.datetime.now(datetime.UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName(
                [x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
                + [
                    x509.DNSName(name)
                    for name in (
                        "localhost",
                        "approved.example",
                        "denied.example",
                        "github.com",
                        "release-assets.githubusercontent.com",
                    )
                ]
            ),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    cert_path, key_path = tmp_path / "cert.pem", tmp_path / "key.pem"
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    requests = []
    routes = {}

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            requests.append((self.command, self.path, dict(self.headers), body))
            status, headers, data = routes.get(self.path, (200, {}, b"artifact"))
            self.send_response(status)
            for name, value in headers.items():
                self.send_header(name, value)
            if "Content-Length" not in headers:
                self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        do_POST = do_GET
        do_CONNECT = do_GET

        def log_message(self, format, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.minimum_version = ssl.TLSVersion.TLSv1_2
    server_context.load_cert_chain(cert_path, key_path)
    server.socket = server_context.wrap_socket(server.socket, server_side=True)
    client_context = ssl.create_default_context(cafile=str(cert_path))
    if getattr(request, "param", None) == "tls1.1":
        # A server that stops at TLS 1.1, and a default context that would accept it.
        for context in (server_context, client_context):
            context.set_ciphers("DEFAULT:@SECLEVEL=0")
            context.minimum_version = ssl.TLSVersion.TLSv1
        server_context.maximum_version = ssl.TLSVersion.TLSv1_1
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    # The production resolver refusal is tested separately. Only the test dial target changes;
    # production request construction, TLS identity, redirect checks and reads run unchanged.
    monkeypatch.setattr(
        prep, "public_addresses", lambda _: [(socket.AF_INET, server.server_address)]
    )
    monkeypatch.setattr(prep.ssl, "create_default_context", lambda: client_context)

    def unrestricted(path, method="GET", headers=None, body=None):
        connection = http.client.HTTPSConnection(
            "127.0.0.1", server.server_port, context=client_context
        )
        try:
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            result = response.status, response.read()
        finally:
            connection.close()
        return result

    yield requests, routes, unrestricted
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


@pytest.mark.filterwarnings("ignore:ssl.TLSVersion.TLSv1:DeprecationWarning")
@pytest.mark.parametrize("receiver", ["tls1.1"], indirect=True)
def test_a_default_context_that_admits_tls_1_1_is_raised_to_tls_1_2(receiver):
    requests, _, unrestricted = receiver
    assert unrestricted("/repository/1.0/artifact.zip") == (200, b"artifact")
    requests.clear()
    with pytest.raises(prep.Refused, match="transfer-failed"):
        prep.fetch(artifact(), time.monotonic() + 5)
    assert not requests


def test_fixed_request_and_ignored_environment_proxy(receiver, monkeypatch):
    requests, _, _ = receiver
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:1")
    assert prep.fetch(artifact(), time.monotonic() + 5) == b"artifact"
    assert requests == [
        (
            "GET",
            "/repository/1.0/artifact.zip",
            {
                "Host": "approved.example",
                "User-Agent": "maf-dependency-preparation/1",
                "Accept": "application/octet-stream",
                "Accept-Encoding": "identity",
            },
            b"",
        )
    ]


@pytest.mark.parametrize(
    "path",
    [
        "/repository/1.0/../secret",
        "/repository/%2e%2e/secret",
        "/repository/%252e%252e/secret",
        "/repository//secret",
        "/repository/%2fsecret",
        "/repository/1.0/a?token=secret",
        "/repository/1.0/a?",
        "/repository/./a",
        "/repository/1.0/a\\secret",
    ],
)
def test_noncanonical_requests_are_accepted_by_control_but_refused(receiver, path):
    requests, _, unrestricted = receiver
    assert unrestricted(path) == (200, b"artifact")
    requests.clear()
    with pytest.raises(prep.Refused):
        prep.fetch(artifact(url="https://approved.example" + path), time.monotonic() + 5)
    assert not requests


@pytest.mark.parametrize(
    "field,value,method",
    [
        ("headers", {"X-Leak": "secret"}, "GET"),
        ("body", "secret", "GET"),
        ("method", "POST", "POST"),
        ("method", "CONNECT", "CONNECT"),
    ],
)
def test_request_content_controls_are_receiver_accepted(receiver, field, value, method):
    requests, _, unrestricted = receiver
    assert (
        unrestricted("/repository/1.0/artifact.zip", method, {"X-Leak": "secret"}, "secret")[0]
        == 200
    )
    requests.clear()
    with pytest.raises(prep.Refused, match="manifest-fields"):
        prep.fetch(artifact(**{field: value}), time.monotonic() + 5)
    assert not requests


@pytest.mark.parametrize(
    "location",
    [
        "https://approved.example/neighbor/1.0/artifact.zip",
        "https://approved.example/repository/2.0/artifact.zip",
        "https://approved.example/repository/1.0/other.zip",
        "https://denied.example/repository/1.0/artifact.zip",
        "https://approved.example/repository/1.0/artifact.zip?secret=value",
    ],
)
def test_redirect_cannot_expand_approved_transfer(receiver, location):
    requests, routes, unrestricted = receiver
    assert unrestricted(urlsplit_target(location))[0] == 200
    requests.clear()
    routes["/repository/1.0/artifact.zip"] = (302, {"Location": location}, b"")
    with pytest.raises(prep.Refused):
        prep.fetch(
            artifact(redirects=["https://approved.example/exact/asset.zip"]), time.monotonic() + 5
        )
    assert len(requests) == 1


def urlsplit_target(url):
    return "/" + url.split("/", 3)[3]


@pytest.mark.parametrize("observed", [0, 1])
def test_ordinary_redirect_chain_must_be_completed(receiver, observed):
    requests, routes, _ = receiver
    chain = ["https://approved.example/first.zip", "https://approved.example/last.zip"]
    if observed:
        routes["/repository/1.0/artifact.zip"] = (302, {"Location": chain[0]}, b"")
    with pytest.raises(prep.Refused, match="redirect-unapproved"):
        prep.fetch(artifact(redirects=chain), time.monotonic() + 5)
    assert len(requests) == observed + 1


@pytest.mark.parametrize(
    "head",
    [
        b"HTTP/1.1 200 OK\r\nX-Large: " + b"a" * 50000 + b"\r\n\r\n",
        b"HTTP/1.1 200 OK\r\n" + (b"X-Many: " + b"a" * 1000 + b"\r\n") * 60 + b"\r\n",
        b"HTTP/1.1 200 OK\r\nX-Folded: a\r\n" + (b" " + b"a" * 1000 + b"\r\n") * 60 + b"\r\n",
        b"HTTP/1.1 200 " + b"a" * 50000 + b"\r\n\r\n",
        b"HTTP/1.1 100 Continue\r\n\r\n" * 1500 + b"HTTP/1.1 200 OK\r\n\r\n",
    ],
    ids=["long-line", "many-headers", "folded", "long-status", "interim"],
)
def test_response_head_refused_before_accumulation(head):
    stream = io.BytesIO(head)

    class Socket:
        def makefile(self, *args):
            return stream

    response = prep.PinnedHTTPS.response_class(cast(socket.socket, Socket()))
    try:
        with pytest.raises(prep.Refused, match="response-headers"):
            response.begin()
        assert stream.tell() <= 32769
    finally:
        response.close()


@pytest.mark.parametrize("interim", [b"", b"HTTP/1.1 100 Continue\r\n\r\n"])
def test_response_head_boundary_preserves_body(interim):
    prefix = interim + b"HTTP/1.1 200 OK\r\nContent-Length: 40000\r\nX-Pad: "
    head = prefix + b"a" * (32768 - len(prefix) - 4) + b"\r\n\r\n"
    stream = io.BytesIO(head + b"b" * 40000)

    class Socket:
        def makefile(self, *args):
            return stream

    response = prep.PinnedHTTPS.response_class(cast(socket.socket, Socket()))
    try:
        response.begin()
        assert stream.tell() == 32768
        response.begin()
        assert response.read() == b"b" * 40000
    finally:
        response.close()


def test_exact_redirect_and_github_transfer_binding(receiver):
    requests, routes, _ = receiver
    routes["/repository/1.0/artifact.zip"] = (
        302,
        {"Location": "https://approved.example/exact/asset.zip"},
        b"",
    )
    assert (
        prep.fetch(
            artifact(redirects=["https://approved.example/exact/asset.zip"]), time.monotonic() + 5
        )
        == b"artifact"
    )
    github_path = "/owner/repo/releases/download/v1.0/provider.zip"
    asset_path = "/github-production-release-asset/123/abcdef-0123?sig=secret&jwt=secret"
    policy = artifact(url="https://github.com" + github_path, github_repository_id="123")
    routes[github_path] = (
        302,
        {"Location": "https://release-assets.githubusercontent.com" + asset_path},
        b"",
    )
    assert prep.fetch(policy, time.monotonic() + 5) == b"artifact"
    assert requests[-1][1] == asset_path
    routes[github_path] = (
        302,
        {
            "Location": "https://release-assets.githubusercontent.com"
            + asset_path.replace("/123/", "/1234/")
        },
        b"",
    )
    with pytest.raises(prep.Refused, match="redirect-artifact"):
        prep.fetch(policy, time.monotonic() + 5)
    routes[github_path] = (
        302,
        {"Location": "https://release-assets.githubusercontent.com" + asset_path + "&leak=secret"},
        b"",
    )
    with pytest.raises(prep.Refused, match="signed-fields"):
        prep.fetch(policy, time.monotonic() + 5)


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "10.0.0.1",
        "169.254.169.254",
        "::1",
        "fc00::1",
        "fe80::1",
        "::ffff:8.8.8.8",
        "2002:0808:0808::1",
    ],
)
def test_private_and_alternate_routes_refused(address, monkeypatch):
    monkeypatch.setattr(
        prep.socket,
        "getaddrinfo",
        lambda *a, **kw: [
            (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("8.8.8.8", 443)),
            (socket.AF_INET, socket.SOCK_STREAM, 0, "", (address, 443)),
        ],
    )
    with pytest.raises(prep.Refused, match="dns-"):
        prep.public_addresses("approved.example")


@pytest.mark.parametrize("failure", ["socket", "connect", "tls", "timeout", "all"])
def test_connection_tries_validated_addresses_with_shared_deadline(monkeypatch, failure):
    connection = prep.PinnedHTTPS("approved.example", time.monotonic() + 20)
    clock = [100.0]
    connection.deadline = 120.0
    monkeypatch.setattr(prep.time, "monotonic", lambda: clock[0])
    addresses = [
        (socket.AF_INET6, socket.SOCK_STREAM, 0, "", ("2606:4700:4700::1111", 443, 0, 0)),
        (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("1.1.1.1", 443)),
    ]
    resolutions, created, attempts, handshakes = [], [], [], []

    def resolve(host, port, **kwargs):
        resolutions.append((host, port))
        return addresses

    class Socket:
        def __init__(self, family):
            self.family = family
            self.timeout = None
            self.closed = False

        def settimeout(self, timeout):
            self.timeout = timeout

        def connect(self, address):
            attempts.append(address)
            if failure == "all" or self.family == socket.AF_INET6:
                if failure == "timeout":
                    clock[0] += self.timeout
                    raise TimeoutError("unreachable address")
                if failure in {"connect", "all"}:
                    raise OSError("unreachable address")

        def close(self):
            self.closed = True

    def create(family, kind):
        if family == socket.AF_INET6 and failure == "socket":
            raise OSError("IPv6 unavailable")
        raw = Socket(family)
        created.append(raw)
        return raw

    def wrap(raw, *, server_hostname):
        handshakes.append(server_hostname)
        if raw.family == socket.AF_INET6 and failure == "tls":
            raise ssl.SSLError("handshake failed")
        return raw

    monkeypatch.setattr(prep.socket, "getaddrinfo", resolve)
    monkeypatch.setattr(prep.socket, "socket", create)
    monkeypatch.setattr(connection.tls_context, "wrap_socket", wrap)
    if failure == "all":
        with pytest.raises(OSError):
            connection.connect()
        assert all(raw.closed for raw in created)
    else:
        connection.connect()
        assert connection.sock is created[-1]
        assert created[-1].family == socket.AF_INET
        assert all(raw.closed for raw in created[:-1])
        assert not created[-1].closed
        assert handshakes and set(handshakes) == {"approved.example"}
        assert clock[0] < connection.deadline
        if failure == "timeout":
            assert created[0].timeout == 10
        connection.close()
    assert resolutions == [("approved.example", 443)]
    assert attempts[-1] == addresses[-1][-1]


@pytest.mark.parametrize("header_delay", [12.0, 19.0])
def test_response_headers_use_shared_deadline_after_tls(monkeypatch, header_delay):
    connection = prep.PinnedHTTPS("approved.example", time.monotonic() + 20)
    clock = [100.0]
    connection.deadline = 120.0
    monkeypatch.setattr(prep.time, "monotonic", lambda: clock[0])
    addresses = [(socket.AF_INET, (address, 443)) for address in ("1.1.1.1", "8.8.8.8")]
    monkeypatch.setattr(prep, "public_addresses", lambda host: addresses)
    attempts = []

    class RawSocket:
        timeout = 0.0

        def settimeout(self, timeout):
            self.timeout = timeout

        def connect(self, address):
            attempts.append(address)
            clock[0] += 1

        def close(self):
            pass

    raw = RawSocket()

    class DelayedHeaders(io.BytesIO):
        waiting = True

        def readline(self, size: int | None = -1):
            if self.waiting:
                self.waiting = False
                clock[0] += min(header_delay, tls.timeout)
                if header_delay > tls.timeout:
                    raise TimeoutError("response head timeout")
            return super().readline(size)

    class TLSSocket(RawSocket):
        def sendall(self, data):
            pass

        def makefile(self, *args):
            return DelayedHeaders(b"HTTP/1.1 200 OK\r\nContent-Length: 8\r\n\r\nartifact")

    tls = TLSSocket()

    def wrap(raw, *, server_hostname):
        assert server_hostname == "approved.example"
        clock[0] += 1
        tls.timeout = raw.timeout
        return tls

    monkeypatch.setattr(prep.socket, "socket", lambda *args: raw)
    monkeypatch.setattr(connection.tls_context, "wrap_socket", wrap)
    try:
        connection.request("GET", "/artifact.zip")
        if header_delay < 18:
            response = connection.getresponse()
            assert response.read() == b"artifact"
            assert clock[0] == 114
        else:
            with pytest.raises(TimeoutError, match="response head timeout"):
                connection.getresponse()
            assert clock[0] <= connection.deadline
        assert attempts == [addresses[0][1]]
        assert raw.timeout == 9
    finally:
        connection.close()


def test_download_integrity_size_deadline_and_diagnostic_redaction(receiver, monkeypatch):
    _, routes, _ = receiver
    with pytest.raises(prep.Refused, match="artifact-mismatch"):
        prep.fetch(artifact(sha256="0" * 64), time.monotonic() + 5)
    with pytest.raises(prep.Refused, match="deadline"):
        prep.fetch(artifact(), time.monotonic() - 1)
    monkeypatch.setattr(prep, "MAX_ARCHIVE", 3)
    with pytest.raises(prep.Refused, match="download-size"):
        prep.fetch(artifact(), time.monotonic() + 5)
    routes["/repository/1.0/artifact.zip"] = (403, {}, b"secret signed-query contents")
    with pytest.raises(prep.Refused) as failure:
        prep.fetch(artifact(), time.monotonic() + 5)
    assert str(failure.value) == "response-status"


@pytest.mark.parametrize("engine", ["terraform", "opentofu"])
def test_preparation_preserves_identity_modules_and_lock(tmp_path, monkeypatch, engine):
    policy = manifest()
    policy["engine"] = engine
    policy["providers"][0]["source"] = (
        "registry."
        + ("terraform.io" if engine == "terraform" else "opentofu.org")
        + "/hashicorp/random"
    )
    provider_zip = bundle({"terraform-provider-random_v3.7.2": b"never executed"})
    module_zip = bundle(
        {
            "repo/module/main.tf": 'module "child" { source = "./child" }\n',
            "repo/module/child/main.tf.json": '{"output":{"hello":{"value":"world"}}}',
            "repo/module/.terraform.lock.hcl": 'provider "registry.terraform.io/hashicorp/random" {}\n',
        }
    )
    policy["providers"][0]["artifact"] = artifact(provider_zip)
    policy["modules"][0]["artifact"] = artifact(module_zip)
    monkeypatch.setattr(
        prep,
        "fetch",
        lambda spec, _, **limits: (
            provider_zip
            if spec["sha256"] == hashlib.sha256(provider_zip).hexdigest()
            else module_zip
        ),
    )
    identity = prep.prepare(policy, tmp_path / "first")
    receipt = json.loads((tmp_path / "first/receipt.json").read_text())
    assert receipt["policy_sha256"] == identity
    assert "url" not in json.dumps(receipt)
    assert receipt["providers"][0]["source"] == policy["providers"][0]["source"]
    assert (
        tmp_path / "first/modules/example/.terraform.lock.hcl"
    ).read_bytes() == b'provider "registry.terraform.io/hashicorp/random" {}\n'
    changed = copy.deepcopy(policy)
    changed["providers"][0]["artifact"]["redirects"] = ["https://approved.example/another/path"]
    assert prep.prepare(changed, tmp_path / "second") != identity


@pytest.mark.parametrize(
    "source,decision",
    [
        ("https://unapproved.example/module.zip", "module-remote"),
        ("../../outside", "file-segments"),
        ("./different", "module-graph-mismatch"),
        ("./${var.child}", "module-source"),
    ],
)
def test_module_graph_refuses_unapproved_edges(source, decision):
    data = bundle(
        {
            "repo/module/main.tf": f'module "child" {{ source = "{source}" }}',
            "repo/module/child/main.tf": 'output "hello" { value = "world" }',
        }
    )
    with pytest.raises(prep.Refused, match=decision):
        prep.module_files(manifest()["modules"][0], data, "terraform")


@pytest.mark.parametrize(
    "engine,extension",
    [("terraform", "tf.json"), ("opentofu", "tf.json"), ("opentofu", "tofu.json")],
)
@pytest.mark.parametrize(
    "configuration",
    [
        '{"module":{"remote":{"source":"https://example.com/unapproved.zip"}},'
        + '"module":{"child":{"source":"./child"}}}',
        '{"module":{"child":{"source":"https://example.com/unapproved.zip"},'
        + '"child":{"source":"./child"}}}',
        '{"module":{"child":{"source":"https://example.com/unapproved.zip","source":"./child"}}}',
    ],
)
def test_module_json_refuses_duplicate_keys_at_every_depth(engine, extension, configuration):
    data = bundle(
        {
            f"repo/module/main.{extension}": configuration,
            "repo/module/child/main.tf": 'output "hello" { value = "world" }',
        }
    )
    with pytest.raises(prep.Refused, match="module-json-duplicate"):
        prep.module_files(manifest()["modules"][0], data, engine)


@pytest.mark.parametrize(
    "files,decision",
    [
        ({"../escape": "bad"}, "file-segments"),
        ({"a//b": "bad"}, "file-path"),
        ({"A": "a", "a": "b"}, "archive-collision"),
        ({"CON.txt": "bad"}, "file-segments"),
    ],
)
def test_archive_paths(files, decision):
    with pytest.raises(prep.Refused, match=decision):
        prep.zip_files(bundle(files), limit=1000)


def test_archive_symlink_and_expansion():
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        entry = zipfile.ZipInfo("link")
        entry.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(entry, "target")
    with pytest.raises(prep.Refused, match="archive-special"):
        prep.zip_files(output.getvalue(), limit=1000)
    with pytest.raises(prep.Refused, match="archive-expansion"):
        prep.zip_files(bundle({"large": "x" * 1001}), limit=1000)


def test_package_hash_is_terraform_h1():
    data = bundle({"terraform-provider-random_v3.7.2": "binary", "LICENSE": "MIT"})
    lines = (
        f"{hashlib.sha256(b'MIT').hexdigest()}  LICENSE\n"
        f"{hashlib.sha256(b'binary').hexdigest()}  terraform-provider-random_v3.7.2\n"
    )
    expected = "h1:" + base64.b64encode(hashlib.sha256(lines.encode()).digest()).decode()
    assert prep.package_hash(data) == expected


def test_package_hash_ignores_directory_entries_and_zip_order():
    plain = bundle({"LICENSE": "MIT", "terraform-provider-random_v3.7.2": "binary"})
    with io.BytesIO() as output:
        with zipfile.ZipFile(output, "w") as archive:
            archive.writestr("docs/", "")
            archive.writestr("terraform-provider-random_v3.7.2", "binary")
            archive.writestr("LICENSE", "MIT")
        assert prep.package_hash(output.getvalue()) == prep.package_hash(plain)


def test_manifest_conflicts_unknown_policy_and_duplicate_json():
    value = manifest()
    value["providers"].append(copy.deepcopy(value["providers"][0]))
    with pytest.raises(prep.Refused, match="provider-conflict"):
        prep.checked_manifest(value)
    with pytest.raises(prep.Refused, match="manifest-duplicate"):
        prep.load_manifest(b'{"schema":1,"schema":2}')
    value = manifest()
    value["providers"][0]["artifact"]["url"] += "?sig=secret"
    with pytest.raises(prep.Refused, match="url-query"):
        prep.checked_manifest(value)


def test_cli_fails_without_publishing_or_leaking_content(tmp_path):
    source = tmp_path / "manifest.json"
    policy = manifest()
    policy["providers"][0]["artifact"]["url"] += "?secret=do-not-log"
    source.write_text(json.dumps(policy))
    output = tmp_path / "prepared"
    result = subprocess.run(
        [
            sys.executable,
            str(Path(prep.__file__)),
            "--manifest",
            str(source),
            "--output",
            str(output),
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 1 and "url-query" in result.stderr
    assert "secret" not in result.stderr and "do-not-log" not in result.stderr
    assert not output.exists()


def test_cli_deadline_kills_worker_and_publishes_nothing(tmp_path, monkeypatch, capsys):
    source = tmp_path / "manifest.json"
    source.write_text(json.dumps(manifest()))
    worker = tmp_path / "worker.py"
    worker.write_text(
        "import pathlib, time\n"
        "def _worker(output, progress):\n"
        "    pathlib.Path(progress).write_text('artifact 0\\n', newline='\\n')\n"
        "    time.sleep(30)\n"
    )
    timeouts = []
    run = subprocess.run

    def run_worker(*args, **kwargs):
        try:
            return run(*args, **kwargs)
        except subprocess.TimeoutExpired:
            timeouts.append(kwargs["timeout"])
            raise

    monkeypatch.setattr(subprocess, "run", run_worker)
    output = tmp_path / "prepared"
    monkeypatch.setattr(prep, "__file__", str(worker))
    monkeypatch.setattr(prep, "DEADLINE", 0.1)
    monkeypatch.setattr(
        sys, "argv", ["prepare", "--manifest", str(source), "--output", str(output)]
    )
    started = time.monotonic()
    with pytest.raises(SystemExit) as exited:
        prep.main()
    assert exited.value.code == 1
    assert time.monotonic() - started < 5
    assert timeouts == [0.1]
    assert not output.exists()
    assert not list(tmp_path.glob(".terraform-preparation-*"))
    assert capsys.readouterr().err == (
        "Dependency preparation refused: preparation-failed\n"
        "  artifact: provider registry.terraform.io/hashicorp/random 3.7.2 linux_amd64\n"
        "  url: https://approved.example/repository/1.0/artifact.zip\n"
    )


@pytest.mark.parametrize(
    "url",
    [
        "http://approved.example/artifact",
        "https://Approved.example/artifact",
        "https://approved.example:443/artifact",
        "https://user@approved.example/artifact",
        "https://127.0.0.1/artifact",
        "https://[::1]/artifact",
        "https://approved.example/artifact#fragment",
        "https://approved.example/artifact%0d%0aHeader:value",
    ],
)
def test_authority_and_encoding_refusals(url):
    with pytest.raises(prep.Refused):
        prep.canonical_url(url)


def test_tls_identity_stays_bound_to_approved_hostname(receiver):
    requests, _, _ = receiver
    with pytest.raises(prep.Refused, match="transfer-failed"):
        prep.fetch(artifact(url="https://other.example/artifact"), time.monotonic() + 5)
    assert not requests


def test_redirect_limit_signed_second_hop_and_complete_response(receiver):
    _, routes, _ = receiver
    chain = [f"https://approved.example/hop/{i}" for i in range(3)]
    routes["/repository/1.0/artifact.zip"] = (302, {"Location": chain[0]}, b"")
    for i in range(3):
        routes[f"/hop/{i}"] = (302, {"Location": chain[(i + 1) % 3]}, b"")
    with pytest.raises(prep.Refused, match="redirect-limit"):
        prep.fetch(artifact(redirects=chain), time.monotonic() + 5)
    source = "/owner/repo/releases/download/v1/asset.zip"
    target = "/github-production-release-asset/123/abc?sig=secret&jwt=secret"
    routes[source] = (
        302,
        {"Location": "https://release-assets.githubusercontent.com" + target},
        b"",
    )
    routes[target] = (302, {"Location": "https://approved.example/again"}, b"")
    with pytest.raises(prep.Refused, match="redirect-unapproved"):
        prep.fetch(
            artifact(url="https://github.com" + source, github_repository_id="123"),
            time.monotonic() + 5,
        )
    routes["/repository/1.0/artifact.zip"] = (
        200,
        {"Content-Length": "12", "Connection": "close"},
        b"artifact",
    )
    with pytest.raises(prep.Refused, match="download-incomplete"):
        prep.fetch(artifact(), time.monotonic() + 5)


@pytest.mark.parametrize(
    "extra,decision",
    [
        ({"repo/module/unlisted/main.tf": 'output "x" { value = 1 }'}, "module-graph-mismatch"),
        ({"repo/module/extra.tofu.json": "{}"}, "module-precedence"),
        ({"repo/module/override.tf": ""}, "module-override"),
        ({"repo/module/.terraform/credentials": "secret"}, "module-hidden"),
        ({"repo/module/.terraform.lock.hcl/secret.txt": "secret"}, "module-hidden"),
        ({"repo/module/child/.terraform.lock.hcl/secret.txt": "secret"}, "module-hidden"),
        ({"repo/module/terraform.tfstate": "{}"}, "module-state"),
    ],
)
def test_module_inventory_refuses_unlisted_or_ambiguous_configuration(extra, decision):
    data = bundle(
        {
            "repo/module/main.tf": 'module "child" { source = "./child" }',
            "repo/module/child/main.tf": 'output "hello" { value = "world" }',
            **extra,
        }
    )
    with pytest.raises(prep.Refused, match=decision):
        prep.module_files(manifest()["modules"][0], data, "opentofu")


@pytest.mark.parametrize("engine", ["terraform", "opentofu"])
@pytest.mark.parametrize("directory", ["", "child/"])
@pytest.mark.parametrize("suffix", ["tfstate", "tfstate.backup", "tfvars", "tfvars.json"])
@pytest.mark.parametrize("spelling", ["lower", "upper", "mixed"])
def test_module_state_and_variable_suffixes_are_case_insensitive(
    engine, directory, suffix, spelling
):
    if spelling == "upper":
        suffix = suffix.upper()
    elif spelling == "mixed":
        suffix = "".join(char.upper() if index % 2 else char for index, char in enumerate(suffix))
    data = bundle(
        {
            "repo/module/main.tf": 'module "child" { source = "./child" }',
            "repo/module/child/main.tf": 'output "hello" { value = "world" }',
            f"repo/module/{directory}secrets.{suffix}": "{}",
        }
    )
    with pytest.raises(prep.Refused, match="module-state"):
        prep.module_files(manifest()["modules"][0], data, engine)


def test_module_cycle_is_refused():
    module = manifest()["modules"][0]
    module["graph"]["child"] = {"parent": "."}
    data = bundle(
        {
            "repo/module/main.tf": 'module "child" { source = "./child" }',
            "repo/module/child/main.tf": 'module "parent" { source = "../" }',
        }
    )
    with pytest.raises(prep.Refused, match="module-cycle"):
        prep.module_files(module, data, "terraform")


@pytest.mark.parametrize("engine", ["terraform", "opentofu"])
def test_root_and_child_lockfiles_remain_allowed(engine):
    files = {
        "main.tf": 'module "child" { source = "./child" }',
        "child/main.tf": 'output "x" { value = 1 }',
        ".terraform.lock.hcl": "# root lock\n",
        "child/.terraform.lock.hcl": "# child lock\n",
    }
    data = bundle({"repo/module/" + name: text for name, text in files.items()})
    assert prep.module_files(manifest()["modules"][0], data, engine) == files


@pytest.mark.parametrize("version", ["3.7.2-.rc", "3.7.2-rc.", "3.7.2-rc..1"])
def test_preparation_refuses_empty_prerelease_components(version):
    policy = manifest()
    policy["providers"][0]["version"] = version
    with pytest.raises(prep.Refused, match="provider-version"):
        prep.checked_manifest(policy)


@pytest.mark.parametrize("platform", ["windows_amd64", "linux_arm64", "darwin_amd64"])
def test_preparation_refuses_unsupported_provider_platform(platform):
    policy = manifest()
    policy["providers"][0]["platform"] = platform
    with pytest.raises(prep.Refused, match="provider-platform"):
        prep.checked_manifest(policy)


@pytest.mark.parametrize(
    "decision", ["artifact-mismatch", "redirect-unapproved", "archive-expansion", "module-hidden"]
)
def test_cli_preserves_worker_refusal_without_publishing(tmp_path, monkeypatch, capsys, decision):
    source = tmp_path / "manifest.json"
    source.write_text(json.dumps(manifest()))
    worker = tmp_path / "worker.py"
    worker.write_text(
        f"import sys\nsys.path.insert(0, {str(Path(prep.__file__).parent)!r})\n"
        + "import terraform_dependencies as prep\n"
        + "def fail(manifest, output, *, progress=None):\n    output.mkdir()\n"
        + f"    prep.require(False, {decision!r})\n"
        + "prep.prepare = fail\n_worker = prep._worker\n"
    )
    output = tmp_path / "prepared"
    monkeypatch.setattr(prep, "__file__", str(worker))
    monkeypatch.setattr(
        sys, "argv", ["prepare", "--manifest", str(source), "--output", str(output)]
    )
    with pytest.raises(SystemExit) as exited:
        prep.main()
    assert exited.value.code == 1
    assert capsys.readouterr().err == f"Dependency preparation refused: {decision}\n"
    assert not output.exists()
    assert not list(tmp_path.glob(".terraform-preparation-*"))


@pytest.mark.parametrize("exit_code", [1, 255])
def test_cli_does_not_forward_arbitrary_worker_diagnostics(
    tmp_path, monkeypatch, capsys, exit_code
):
    source = tmp_path / "manifest.json"
    source.write_text(json.dumps(manifest()))
    worker = tmp_path / "worker.py"
    worker.write_text(
        "import sys\nprint('https://secret.example/?token=do-not-log', file=sys.stderr)\n"
        + f"sys.exit({exit_code})\n"
    )
    output = tmp_path / "prepared"
    monkeypatch.setattr(prep, "__file__", str(worker))
    monkeypatch.setattr(
        sys, "argv", ["prepare", "--manifest", str(source), "--output", str(output)]
    )
    with pytest.raises(SystemExit):
        prep.main()
    assert capsys.readouterr().err == "Dependency preparation refused: preparation-failed\n"
    assert not output.exists()


def test_preparation_records_the_artifact_and_status_a_download_stopped_on(receiver, tmp_path):
    _, routes, _ = receiver
    routes["/repository/1.0/artifact.zip"] = (404, {}, b"not found")
    progress = tmp_path / "progress"
    with pytest.raises(prep.Refused, match="^response-status$") as refused:
        prep.prepare(manifest(), tmp_path / "prepared", progress=progress)
    assert refused.value.status == 404
    assert progress.read_text(encoding="ascii") == "artifact 0\n"


@pytest.mark.parametrize(
    ("route", "decision", "status"),
    [
        (
            (302, {"Location": "https://denied.example/repository/1.0/artifact.zip"}, b""),
            "redirect-unapproved",
            302,
        ),
        ((307, {"Location": "https://approved.example/a.zip"}, b""), "redirect-unapproved", 307),
        ((200, {"Content-Encoding": "gzip"}, b"artifact"), "response-encoding", 200),
        ((200, {}, b"other"), "artifact-mismatch", 200),
    ],
)
def test_every_refusal_holding_a_response_carries_the_status_it_arrived_with(
    receiver, route, decision, status
):
    _, routes, _ = receiver
    routes["/repository/1.0/artifact.zip"] = route
    with pytest.raises(prep.Refused, match=f"^{decision}$") as refused:
        prep.fetch(artifact(), time.monotonic() + 5)
    assert refused.value.status == status


@pytest.mark.parametrize(
    "url", ["https://approved.example/a%2fb.zip", "https://denied.example:8443/a.zip"]
)
def test_a_refusal_raised_before_any_response_carries_no_status(url):
    with pytest.raises(prep.Refused) as refused:
        prep.fetch(artifact(url=url), time.monotonic() + 5)
    assert refused.value.status == 0


@pytest.mark.parametrize(
    ("policy", "note", "expected"),
    [
        (
            manifest,
            "artifact 0\nstatus 404\n",
            [
                "  artifact: provider registry.terraform.io/hashicorp/random 3.7.2 linux_amd64",
                "  url: https://approved.example/repository/1.0/artifact.zip",
                "  status: 404",
            ],
        ),
        (
            manifest,
            "artifact 1\n",
            [
                "  artifact: module example",
                "  url: https://approved.example/repository/1.0/artifact.zip",
            ],
        ),
    ],
)
def test_a_refusal_names_its_artifact_from_the_manifest(tmp_path, policy, note, expected):
    progress = tmp_path / "progress"
    progress.write_text(note, encoding="ascii", newline="\n")
    assert prep.refusal_context(progress, policy()) == "\n" + "\n".join(expected)


@pytest.mark.parametrize(
    "note",
    [
        None,
        b"",
        b"artifact 0",
        b"artifact 0\nstatus 404",
        b"artifact 9\n",
        b"status 404\n",
        b"artifact 0\nhttps://secret.example/?token=do-not-log\n",
        "artifact 0\n".encode("utf-16"),
    ],
)
def test_a_note_that_is_not_one_complete_position_names_nothing(tmp_path, note):
    progress = tmp_path / "progress"
    if note is not None:
        progress.write_bytes(note)
    assert prep.refusal_context(progress, manifest()) == ""


def test_cli_names_the_artifact_a_worker_refusal_stopped_on(tmp_path, monkeypatch, capsys):
    source = tmp_path / "manifest.json"
    source.write_text(json.dumps(manifest()))
    worker = tmp_path / "worker.py"
    worker.write_text(
        f"import sys\nsys.path.insert(0, {str(Path(prep.__file__).parent)!r})\n"
        "import terraform_dependencies as prep\n"
        "def fail(manifest, output, *, progress=None):\n"
        "    output.mkdir()\n"
        "    progress.write_text('artifact 1\\n', encoding='ascii', newline='\\n')\n"
        "    raise prep.Refused('response-status', 404)\n"
        "prep.prepare = fail\n_worker = prep._worker\n"
    )
    output = tmp_path / "prepared"
    monkeypatch.setattr(prep, "__file__", str(worker))
    monkeypatch.setattr(
        sys, "argv", ["prepare", "--manifest", str(source), "--output", str(output)]
    )
    with pytest.raises(SystemExit) as exited:
        prep.main()
    assert exited.value.code == 1
    assert capsys.readouterr().err == (
        "Dependency preparation refused: response-status\n"
        "  artifact: module example\n"
        "  url: https://approved.example/repository/1.0/artifact.zip\n"
        "  status: 404\n"
    )
    assert not output.exists()
    assert not list(tmp_path.glob(".terraform-preparation-*"))


def test_worker_exit_codes_cover_all_fixed_policy_refusals():
    decisions = set()
    for node in ast.walk(ast.parse(Path(prep.__file__).read_text())):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
            continue
        index = {"require": 1, "Refused": 0, "unique_json": 1}.get(node.func.id)
        if index is None or len(node.args) <= index:
            continue
        argument = node.args[index]
        if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
            decisions.add(argument.value)
    decisions.discard("preparation-failed")
    assert set(prep._WORKER_DECISIONS) == decisions
    assert len(prep._WORKER_DECISIONS) == len(decisions) < 192


@pytest.mark.parametrize("content", [b"\xff", b"\xc3"])
def test_module_invalid_utf8_has_fixed_refusal(content):
    module = manifest()["modules"][0]
    module["graph"] = {".": {}}
    with pytest.raises(prep.Refused, match="^module-text$"):
        prep.module_files(module, bundle({"repo/module/main.tf": content}), "terraform")


@pytest.mark.parametrize("change", ["limit", "implementation"])
def test_policy_identity_changes_with_request_contract(tmp_path, monkeypatch, change):
    policy = {"schema": 1, "engine": "terraform", "providers": [], "modules": []}
    first = prep.prepare(policy, tmp_path / "first")
    if change == "limit":
        monkeypatch.setattr(prep, "MAX_ARCHIVE", prep.MAX_ARCHIVE - 1)
    else:
        source = tmp_path / "policy.py"
        source.write_text(Path(prep.__file__).read_text() + "\n# changed policy implementation\n")
        monkeypatch.setattr(prep, "__file__", str(source))
    second = prep.prepare(policy, tmp_path / "second")
    assert first != second


def test_cli_rejects_worker_flag_without_reading_stdin(tmp_path):
    output = tmp_path / "prepared"
    process = subprocess.Popen(
        [sys.executable, str(Path(prep.__file__)), "--worker", "--output", str(output)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    completed = False
    try:
        process.wait(timeout=3)
        completed = True
    except subprocess.TimeoutExpired:
        process.kill()
    finally:
        _, stderr = process.communicate(timeout=5)
    assert completed, "worker flag bypassed supervision and blocked on stdin"
    assert process.returncode == 2
    assert b"unrecognized arguments: --worker" in stderr
    assert not output.exists()


def test_cli_supervised_success_publishes_once(tmp_path):
    source = tmp_path / "manifest.json"
    source.write_text(
        json.dumps({"schema": 1, "engine": "terraform", "providers": [], "modules": []})
    )
    output = tmp_path / "prepared"
    command = [
        sys.executable,
        str(Path(prep.__file__)),
        "--manifest",
        str(source),
        "--output",
        str(output),
    ]
    result = subprocess.run(command, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    receipt = (output / "receipt.json").read_bytes()
    assert not list(tmp_path.glob(".terraform-preparation-*"))
    repeated = subprocess.run(command, capture_output=True, text=True, timeout=10)
    assert repeated.returncode == 1 and "output-exists" in repeated.stderr
    assert (output / "receipt.json").read_bytes() == receipt


def test_policy_receipt_identifies_contract_and_normalizes_source_newlines(tmp_path, monkeypatch):
    policy = {"schema": 1, "engine": "terraform", "providers": [], "modules": []}
    source = tmp_path / "policy.py"
    source.write_bytes(b"# stable implementation\n")
    monkeypatch.setattr(prep, "__file__", str(source))
    first = prep.prepare(policy, tmp_path / "first")
    source.write_bytes(b"# stable implementation\r\n")
    second = prep.prepare(dict(reversed(list(policy.items()))), tmp_path / "second")
    assert first == second
    receipt = json.loads((tmp_path / "first/receipt.json").read_text())
    assert receipt["policy_contract"]["schema"] == 1
    assert receipt["policy_contract"]["limits"]["archive"] == prep.MAX_ARCHIVE
    manifest_digest = hashlib.sha256(
        json.dumps(policy, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    assert receipt["manifest_sha256"] == manifest_digest
    identity_input = {"manifest_sha256": manifest_digest, "contract": receipt["policy_contract"]}
    assert (
        hashlib.sha256(
            json.dumps(identity_input, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        == first
    )


@pytest.mark.parametrize("version", ["03.7.2", "3.07.2", "3.7.02", "3.7.2-01", "3.7.2-rc.01"])
def test_preparation_refuses_leading_zero_numeric_components(version):
    policy = manifest()
    policy["providers"][0]["version"] = version
    with pytest.raises(prep.Refused, match="provider-version"):
        prep.checked_manifest(policy)


@pytest.mark.parametrize("json_suffix", ["", ".json"])
def test_module_counterparts_do_not_rewrite_directory_names(json_suffix):
    module = manifest()["modules"][0]
    module["graph"] = {".": {"tf": "foo.tf", "tofu": "foo.tofu"}, "foo.tf": {}, "foo.tofu": {}}
    root = 'module "tf" { source = "./foo.tf" }\nmodule "tofu" { source = "./foo.tofu" }'
    files = {
        "main.tf": root,
        "foo.tf/main.tf" + json_suffix: "{}" if json_suffix else "",
        "foo.tofu/main.tofu" + json_suffix: "{}" if json_suffix else "",
    }
    data = bundle({"repo/module/" + name: text for name, text in files.items()})
    assert prep.module_files(module, data, "opentofu") == files


def test_request_host_occurrences_survive_receiver_capture(receiver, monkeypatch):
    _, routes, _ = receiver
    seen = []
    original = BaseHTTPRequestHandler.parse_request

    def capture(handler):
        parsed = original(handler)
        if parsed:
            seen.append(handler.headers.get_all("Host"))
        return parsed

    monkeypatch.setattr(BaseHTTPRequestHandler, "parse_request", capture)
    routes["/repository/1.0/artifact.zip"] = (302, {"Location": "https://github.com/approved"}, b"")
    policy = artifact(redirects=["https://github.com/approved"])
    assert prep.fetch(policy, time.monotonic() + 5) == b"artifact"
    assert seen == [["approved.example"], ["github.com"]]
    seen.clear()
    putrequest = prep.PinnedHTTPS.putrequest

    def duplicate_host(connection, *args, **kwargs):
        putrequest(connection, *args, **kwargs)
        connection.putheader("Host", connection.host)

    monkeypatch.setattr(prep.PinnedHTTPS, "putrequest", duplicate_host)
    assert prep.fetch(policy, time.monotonic() + 5) == b"artifact"
    assert seen == [["approved.example", "approved.example"], ["github.com", "github.com"]]


@pytest.mark.parametrize("json_suffix", ["", ".json"])
def test_module_counterpart_in_same_directory_still_refused(json_suffix):
    module = manifest()["modules"][0]
    module["graph"] = {".": {}}
    files = {
        "repo/module/main.tf" + json_suffix: "{}" if json_suffix else "",
        "repo/module/main.tofu" + json_suffix: "{}" if json_suffix else "",
    }
    with pytest.raises(prep.Refused, match="module-precedence"):
        prep.module_files(module, bundle(files), "opentofu")


NETWORK_REVISION = "b" * 40
SHARED_REVISION = "c" * 40
NETWORK_ROOT = f"terraform-network-{NETWORK_REVISION}/"
SHARED_SOURCE = "registry.terraform.io/Azure/shared/azure"


def registry_policy():
    policy = manifest()
    policy["modules"] = []
    policy["registry_modules"] = [
        {
            "name": "network",
            "source": "registry.terraform.io/Azure/network/azurerm",
            "version": "1.2.3",
            "revision": NETWORK_REVISION,
            "graph": {
                ".": {"shared": {"registry": "shared"}, "subnet": {"local": "modules/subnet"}},
                "modules/subnet": {"shared": {"registry": "shared"}},
            },
            "artifact": artifact(
                url=f"https://codeload.github.com/Azure/terraform-network/zip/{NETWORK_REVISION}"
            ),
        },
        {
            "name": "shared",
            "source": SHARED_SOURCE,
            "version": "0.6.0",
            "revision": SHARED_REVISION,
            "graph": {".": {}},
            "artifact": artifact(
                url=f"https://codeload.github.com/Azure/terraform-shared/zip/{SHARED_REVISION}"
            ),
        },
    ]
    return policy


def network_files(**changes):
    files = {
        "main.tf": (
            'module "shared" {\n  source  = "Azure/shared/azure"\n  version = "~> 0.6"\n}\n'
            'module "subnet" {\n  source = "./modules/subnet/"\n}\n'
            'resource "random_id" "suffix" {\n  byte_length = 4\n}\n'
        ),
        "terraform.tf": (
            "terraform {\n  required_providers {\n    random = {\n"
            '      source  = "hashicorp/random"\n      version = ">= 3.5, < 4.0"\n    }\n  }\n}\n'
        ),
        "modules/subnet/main.tf.json": json.dumps(
            {"module": {"shared": {"source": SHARED_SOURCE, "version": "0.6.0"}}}
        ),
        "README.md": "# network\n",
        ".gitignore": ".terraform\n",
        ".github/workflows/check.yml": "on: push\n",
        "examples/default/main.tf": 'module "x" {\n  source = "git::https://example.com/x"\n}\n',
    }
    files.update(changes)
    return {NETWORK_ROOT + name: data for name, data in files.items() if data is not None}


def verify_network(policy=None, **changes):
    policy = policy or registry_policy()
    catalog = {item["name"]: item for item in policy["registry_modules"]}
    data = bundle(network_files(**changes))
    return prep.registry_module_files(catalog["network"], data, catalog, policy["providers"])


def test_a_refusal_names_a_registry_module_by_its_registry_address(tmp_path):
    progress = tmp_path / "progress"
    progress.write_text("artifact 2\nstatus 403\n", encoding="ascii", newline="\n")
    assert prep.refusal_context(progress, registry_policy()) == (
        "\n  artifact: registry module registry.terraform.io/Azure/shared/azure 0.6.0"
        f"\n  url: https://codeload.github.com/Azure/terraform-shared/zip/{SHARED_REVISION}"
        "\n  status: 403"
    )


def test_registry_packages_bake_declared_directories_and_inventory(tmp_path, monkeypatch):
    policy = registry_policy()
    archives = {
        "network": bundle(network_files()),
        "shared": bundle({f"terraform-shared-{SHARED_REVISION}/main.tf": "terraform {}\n"}),
        "provider": bundle({"terraform-provider-random_v3.7.2": b"never executed"}),
    }
    policy["providers"][0]["artifact"] = artifact(archives["provider"])
    for item in policy["registry_modules"]:
        item["artifact"]["sha256"] = hashlib.sha256(archives[item["name"]]).hexdigest()
    by_digest = {hashlib.sha256(data).hexdigest(): data for data in archives.values()}
    monkeypatch.setattr(prep, "fetch", lambda spec, _, **limits: by_digest[spec["sha256"]])
    prep.prepare(policy, tmp_path / "prepared")
    receipt = json.loads((tmp_path / "prepared/receipt.json").read_text())
    network, shared = receipt["registry_modules"]
    baked = ["README.md", "main.tf", "modules/subnet/main.tf.json", "terraform.tf"]
    assert sorted(network["files"]) == baked
    for name, content in network_files().items():
        path = tmp_path / "prepared/registry/network" / name.removeprefix(NETWORK_ROOT)
        assert path.exists() == (name.removeprefix(NETWORK_ROOT) in baked)
        assert not path.exists() or path.read_bytes() == content.encode()
    nested = {"source": SHARED_SOURCE, "version": "0.6.0", "package": "shared", "dir": "."}
    assert network["inventories"] == {
        ".": [
            {"key": "shared", **nested},
            {
                "key": "subnet",
                "source": "./modules/subnet",
                "package": "network",
                "dir": "modules/subnet",
            },
            {"key": "subnet.shared", **nested},
        ],
        "modules/subnet": [{"key": "shared", **nested}],
    }
    assert shared["inventories"] == {".": []}
    assert "url" not in json.dumps(receipt)


def test_prepared_receipt_records_each_provider_files_and_h1(tmp_path, monkeypatch):
    policy = registry_policy()
    provider_archive = bundle({"terraform-provider-random_v3.7.2": b"never executed"})
    archives = {
        "network": bundle(network_files()),
        "shared": bundle({f"terraform-shared-{SHARED_REVISION}/main.tf": "terraform {}\n"}),
        "provider": provider_archive,
    }
    policy["providers"][0]["artifact"] = artifact(provider_archive)
    for item in policy["registry_modules"]:
        item["artifact"]["sha256"] = hashlib.sha256(archives[item["name"]]).hexdigest()
    by_digest = {hashlib.sha256(data).hexdigest(): data for data in archives.values()}
    monkeypatch.setattr(prep, "fetch", lambda spec, _, **limits: by_digest[spec["sha256"]])
    prep.prepare(policy, tmp_path / "prepared")
    receipt = json.loads((tmp_path / "prepared/receipt.json").read_text())
    provider = receipt["providers"][0]
    with zipfile.ZipFile(io.BytesIO(provider_archive)) as archive:
        files = {
            item.filename: hashlib.sha256(archive.read(item.filename)).hexdigest()
            for item in archive.infolist()
            if not item.is_dir()
        }
    lines = "".join(f"{digest}  {name}\n" for name, digest in sorted(files.items()))
    assert provider["files"] == files
    assert provider["h1"] == (
        "h1:" + base64.b64encode(hashlib.sha256(lines.encode()).digest()).decode()
    )
    stored = (
        tmp_path
        / "prepared"
        / "mirror"
        / provider["source"]
        / "terraform-provider-random_3.7.2_linux_amd64.zip"
    )
    assert stored.read_bytes() == provider_archive


def _set(path, value):
    def change(policy):
        target = policy
        for part in path[:-1]:
            target = target[part]
        target[path[-1]] = value

    return change


CODELOAD = "https://codeload.github.com/Azure/terraform-network/zip/"


@pytest.mark.parametrize(
    "change,decision",
    [
        (_set(["engine"], "opentofu"), "registry-engine"),
        (_set(["registry_modules", 0, "source"], "Azure/network/azurerm"), "registry-source"),
        (_set(["registry_modules", 0, "source"], "example.com/Azure/x/azurerm"), "registry-source"),
        (
            lambda policy: policy["registry_modules"][1].update(
                source="registry.terraform.io/azure/NETWORK/azurerm", version="1.2.3"
            ),
            "registry-conflict",
        ),
        (_set(["registry_modules", 1, "name"], "network"), "registry-conflict"),
        (_set(["registry_modules", 1, "name"], "Shared"), "module-name"),
        (
            _set(
                ["registry_modules", 0, "graph", ".", "shared"], {"registry": "shared", "dir": "x"}
            ),
            "registry-edge",
        ),
        (
            _set(
                ["registry_modules", 0, "graph", ".", "shared"], {"registry": "shared", "dir": "."}
            ),
            "registry-edge",
        ),
        (
            _set(["excluded"], [{"source": SHARED_SOURCE, "version": "1", "reason": "x"}]),
            "registry-excluded",
        ),
        (_set(["registry_modules", 0, "version"], "1.2.3-beta"), "registry-version"),
        (_set(["registry_modules", 0, "revision"], "main"), "registry-revision"),
        (
            _set(["registry_modules", 0, "artifact", "url"], CODELOAD + "d" * 40),
            "registry-revision",
        ),
        (
            _set(["registry_modules", 0, "artifact", "github_repository_id"], "123"),
            "github-source",
        ),
        (
            _set(["registry_modules", 1, "graph", "."], {"loop": {"registry": "network"}}),
            "module-cycle",
        ),
        (
            _set(["registry_modules", 0, "graph", ".", "shared"], {"registry": "absent"}),
            "registry-edge",
        ),
        (
            _set(
                ["registry_modules", 0, "graph", ".", "shared"],
                {"registry": "shared", "local": "."},
            ),
            "registry-edge",
        ),
        (_set(["registry_modules", 0, "graph", ".github"], {}), "module-hidden"),
        (
            _set(["registry_modules", 0, "graph", ".", "subnet"], {"local": "absent"}),
            "module-edge-target",
        ),
    ],
)
def test_registry_manifest_refusals(change, decision):
    policy = registry_policy()
    change(policy)
    with pytest.raises(prep.Refused, match=decision):
        prep.checked_manifest(policy)


def _module(body):
    return f'module "shared" {{\n{body}}}\n'


@pytest.mark.parametrize(
    "changes,decision",
    [
        ({"main.tf": _module('  source = "Azure/shared/azure"\n')}, "registry-version"),
        (
            {"main.tf": _module('  source = "Azure/shared/azure"\n  version = "~> 0.7"\n')},
            "registry-constraint",
        ),
        (
            {"main.tf": _module('  source = "Azure/shared/azure"\n  version = "~> 0"\n')},
            "registry-constraint",
        ),
        (
            {
                "main.tf": _module(
                    '  source = "example.com/Azure/shared/azure"\n  version = "0.6.0"\n'
                )
            },
            "registry-host",
        ),
        (
            {"main.tf": _module('  source = "Azure/other/azure"\n  version = "0.6.0"\n')},
            "registry-edge",
        ),
        ({"main.tf": _module('  source = "git::https://example.com/shared"\n')}, "module-remote"),
        ({"main.tf": _module("  source = var.source\n")}, "module-source"),
        (
            {"modules/subnet/extra.tf": 'module "x" {\n  source = "../../../outside"\n}\n'},
            "file-segments",
        ),
        (
            {"modules/subnet/extra.tf": 'module "x" {\n  source = "./more"\n}\n'},
            "module-graph-mismatch",
        ),
        (
            {"modules/subnet/main.tf.json": None, "modules/subnet/notes.md": "none"},
            "registry-directory",
        ),
        (
            {"terraform.tf": None, "time.tf": 'resource "time_sleep" "wait" {}\n'},
            "registry-provider",
        ),
        (
            {
                "terraform.tf": 'terraform {\n  required_providers {\n    azapi = {\n      source = "Azure/azapi"\n    }\n  }\n}\n'
            },
            "registry-provider",
        ),
        (
            {
                "terraform.tf": 'terraform {\n  required_providers {\n    random = "~> 4.0"\n  }\n}\n'
            },
            "registry-provider",
        ),
        ({"versions_override.tf": _module('  version = "~> 0.7"\n')}, "registry-constraint"),
        ({"orphan_override.tf": 'module "orphan" {\n  version = "1.0.0"\n}\n'}, "module-duplicate"),
        ({"broken.tf": 'module "x" {\n'}, "module-parse"),
        ({"modules/subnet/bad name.tf": "terraform {}\n"}, "file-path"),
        ({"terraform.tfstate": "{}"}, "module-state"),
        ({"notes.txt": b"\xff"}, "module-text"),
    ],
)
def test_registry_package_refusals(changes, decision):
    with pytest.raises(prep.Refused, match=decision):
        verify_network(**changes)


def test_registry_package_accepts_builtin_and_legacy_provider_requirements():
    requirements = (
        'terraform {\n  required_providers {\n    random = "~> 3.5"\n'
        '    terraform = {\n      source = "terraform.io/builtin/terraform"\n    }\n  }\n}\n'
    )
    files, sources = verify_network(
        **{"terraform.tf": requirements, "data.tf": 'resource "terraform_data" "x" {}\n'}
    )
    assert "data.tf" in files
    assert sources["."] == {"shared": SHARED_SOURCE, "subnet": "./modules/subnet"}


@pytest.mark.parametrize(
    "name,requirements",
    [
        (
            "terraform.tf",
            "terraform {\n  required_providers {\n    # pinned below\n    random = {\n"
            '      source  = "hashicorp/random" # the only provider\n'
            '      version = "~> 3.5"\n    }\n  }\n}\n',
        ),
        (
            "terraform.tf.json",
            json.dumps(
                {
                    "terraform": {
                        "required_providers": {
                            "//": "pinned below",
                            "random": {"source": "hashicorp/random", "version": "~> 3.5"},
                        }
                    }
                }
            ),
        ),
    ],
)
def test_registry_package_ignores_comments_among_provider_requirements(name, requirements):
    files, _ = verify_network(**{"terraform.tf": None, name: requirements})
    assert name in files


def test_registry_graph_must_reach_every_declared_directory():
    policy = registry_policy()
    policy["registry_modules"][0]["graph"]["modules/unused"] = {}
    with pytest.raises(prep.Refused, match="module-unreachable"):
        verify_network(policy, **{"modules/unused/main.tf": "terraform {}\n"})


def test_registry_inventory_is_bounded(monkeypatch):
    policy = registry_policy()
    catalog = {item["name"]: item for item in policy["registry_modules"]}
    _, sources = verify_network(policy)
    monkeypatch.setattr(prep, "MAX_INVENTORY", 2)
    with pytest.raises(prep.Refused, match="registry-inventory"):
        prep.registry_inventory("network", ".", catalog, {"network": sources, "shared": {".": {}}})


def versioned_policy():
    """Two versions of one source, the older one reached only through a subdirectory call."""
    policy = registry_policy()
    older = copy.deepcopy(policy["registry_modules"][0])
    older.update(name="network-1.0.0", version="1.0.0", graph={"modules/subnet": {}})
    policy["registry_modules"].append(older)
    policy["registry_modules"][0]["graph"]["."]["legacy"] = {
        "registry": "network-1.0.0",
        "dir": "modules/subnet",
    }
    return policy


LEGACY = 'module "legacy" {\n  source  = "Azure/network/azurerm//modules/subnet"\n  version = "~> 1.0.0"\n}\n'


def test_one_source_bakes_at_two_versions_through_a_subdirectory_call():
    policy = versioned_policy()
    prep.checked_manifest(policy)
    catalog = {item["name"]: item for item in policy["registry_modules"]}
    data = bundle(network_files(**{"legacy.tf": LEGACY}))
    _, sources = prep.registry_module_files(catalog["network"], data, catalog, policy["providers"])
    assert sources["."]["legacy"] == "registry.terraform.io/Azure/network/azurerm//modules/subnet"
    older = bundle(network_files(**{"modules/subnet/main.tf.json": "{}"}))
    files, _ = prep.registry_module_files(
        catalog["network-1.0.0"], older, catalog, policy["providers"]
    )
    assert sorted(files) == ["modules/subnet/main.tf.json"]
    everything = {
        "network": sources,
        "network-1.0.0": {"modules/subnet": {}},
        "shared": {".": {}},
    }
    records = prep.registry_inventory("network", ".", catalog, everything)
    assert {
        "key": "legacy",
        "source": sources["."]["legacy"],
        "version": "1.0.0",
        "package": "network-1.0.0",
        "dir": "modules/subnet",
    } in records


@pytest.mark.parametrize(
    "legacy,decision",
    [
        (LEGACY.replace("~> 1.0.0", "~> 1.2"), "registry-constraint"),
        (LEGACY.replace("//modules/subnet", ""), "registry-edge"),
        (LEGACY.replace("modules/subnet", "modules/../subnet"), "file-segments"),
    ],
)
def test_subdirectory_calls_must_match_their_declared_edge(legacy, decision):
    policy = versioned_policy()
    catalog = {item["name"]: item for item in policy["registry_modules"]}
    data = bundle(network_files(**{"legacy.tf": legacy}))
    with pytest.raises(prep.Refused, match=decision):
        prep.registry_module_files(catalog["network"], data, catalog, policy["providers"])


def test_a_package_directory_nobody_enters_is_unreachable():
    policy = versioned_policy()
    del policy["registry_modules"][0]["graph"]["."]["legacy"]
    catalog = {item["name"]: item for item in policy["registry_modules"]}
    older = bundle(network_files(**{"modules/subnet/main.tf.json": "{}"}))
    with pytest.raises(prep.Refused, match="module-unreachable"):
        prep.registry_module_files(catalog["network-1.0.0"], older, catalog, policy["providers"])


def test_only_baked_files_are_policy_and_terraform_ignores_tofu_files():
    files, _ = verify_network(
        **{
            "examples/bad name/main.tf": 'module "x" {\n  source = "git::https://example.com/x"\n}\n',
            "examples/Case.tf": "a",
            "examples/case.tf": "b",
            "docs/huge.bin": b"\xff" * (prep.MAX_TEXT + 1),
            "main.tofu": "this is not HCL !",
            "variables.tf": 'variable "in" {\r\n  default = <<-EOT\r\n  "{\r\n  EOT\r\n}\r\n',
        }
    )
    assert "main.tofu" not in files
    assert "variables.tf" in files
    assert not any(name.startswith(("examples/", "docs/")) for name in files)
    with pytest.raises(prep.Refused, match="archive-collision"):
        verify_network(**{"Main.tf": "terraform {}\n"})


@pytest.mark.parametrize(
    "version,constraint,expected",
    [
        ("2.12.0", "~> 2.12", True),
        ("3.0.0", "~> 2.12", False),
        ("0.4.0", "~> 0.3", True),
        ("3.6.0", "~> 3.5.0", False),
        ("3.5.9", "~> 3.5.0", True),
        ("4.90.0", ">= 4.81, < 5.1", True),
        ("5.1.0", ">= 4.81, < 5.1", False),
        ("0.6.0", "0.6.0", True),
        ("0.6.0", "= 0.6", True),
        ("0.6.0", "!= 0.6.0", False),
    ],
)
def test_version_constraints_follow_registry_semantics(version, constraint, expected):
    assert prep.satisfies(version, constraint, "registry-constraint") is expected


@pytest.mark.parametrize("constraint", ["~> 1", "", "latest", ">= 1.0.0-beta", "v1.0.0"])
def test_unsupported_version_constraints_are_refused(constraint):
    with pytest.raises(prep.Refused, match="registry-constraint"):
        prep.satisfies("1.0.0", constraint, "registry-constraint")


def test_provider_archives_are_checked_without_retaining_content():
    data = bundle({"terraform-provider-random_v3.7.2": b"x" * 5000})
    assert prep.zip_files(data, limit=5000, retain=False) == {
        "terraform-provider-random_v3.7.2": b""
    }
    with pytest.raises(prep.Refused, match="archive-expansion"):
        prep.zip_files(data, limit=4999, retain=False)
