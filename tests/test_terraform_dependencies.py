"""Request receiver controls and dependency preparation without running provider code."""

from __future__ import annotations

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
def receiver(tmp_path, monkeypatch):
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
    source.write_text(
        json.dumps({"schema": 1, "engine": "terraform", "providers": [], "modules": []})
    )
    worker = tmp_path / "worker.py"
    worker.write_text("import time\ntime.sleep(30)\n")
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
    assert not output.exists()
    assert not list(tmp_path.glob(".terraform-preparation-*"))
    assert "preparation-failed" in capsys.readouterr().err


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
