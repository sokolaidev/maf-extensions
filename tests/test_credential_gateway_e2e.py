"""Opt-in real Docker/WSLC credential isolation and orphan-expiry validation.

Set MAF_CREDENTIAL_E2E=docker,wslc and build/load MAF_CREDENTIAL_PROXY_IMAGE on each engine.
The only credentials are synthetic. The upstream and its CA are local test fixtures.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import subprocess
import sys
import tarfile
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from maf_sandbox import (
    CallerContext,
    Capability,
    Egress,
    EgressRule,
    IdentityScope,
    IsolationScope,
    ListedFile,
    SandboxKey,
    SandboxRouter,
    SandboxSpec,
)
from maf_sandbox.credentials import CredentialGateway, CredentialGrant
from maf_sandbox_codeact import make_codeact_tools
from maf_sandbox_docker import DockerSandboxBackend, DockerSandboxConfig
from maf_sandbox_wslc import WslcSandboxBackend, WslcSandboxConfig

PROXY = os.environ.get("MAF_CREDENTIAL_PROXY_IMAGE", "maf-credentials:757")
GUEST = "python@sha256:8d9d0b8bcf6506481eae4907c18f5e3e7902e629f5f6d684f9e7c32e85e3ddf0"
ENGINES = os.environ.get("MAF_CREDENTIAL_E2E", "").split(",")

SERVER = """import hashlib, ssl
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
class Handler(BaseHTTPRequestHandler):
 protocol_version = "HTTP/1.1"
 def do_GET(self):
  body = hashlib.sha256(self.headers.get("Authorization", "").encode()).hexdigest().encode()
  self.send_response(200); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
 def log_message(self, *args): pass
s = ThreadingHTTPServer(("0.0.0.0", 8443), Handler)
ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); ctx.load_cert_chain("/tmp/cert.pem", "/tmp/key.pem")
s.socket = ctx.wrap_socket(s.socket, server_side=True); s.serve_forever()
"""

PROBE = """import http.client, json, os, ssl, sys, time
from urllib.parse import urlsplit
p = urlsplit(os.environ["HTTPS_PROXY"])
c = http.client.HTTPSConnection(p.hostname, p.port, context=ssl.create_default_context(), timeout=5)
c.set_tunnel("api.example.com", 8443)
def request():
 try:
  c.request(sys.argv[1], sys.argv[2], headers={"Authorization": "Bearer copied-foreign-placeholder"})
  r=c.getresponse(); body=r.read().decode(); return [r.status, body, c.sock.getsockname()[1] if c.sock else None]
 except Exception as exc: return [0, type(exc).__name__, None]
first=request()
if len(sys.argv)>3:
 end=time.monotonic()+float(sys.argv[3])
 while time.monotonic()<end:
  time.sleep(min(0.5, max(0, end-time.monotonic())))
  if time.monotonic()<end: request()
 print(json.dumps([first, request()]))
else: print(json.dumps(first))
"""


def cli(engine, *args, data=None, check=True):
    return subprocess.run([engine, *args], input=data, capture_output=True, check=check, timeout=90)


def container(engine, *args, **kwargs):
    return cli(engine, "container", *args, **kwargs)


def inspect(engine, name):
    return json.loads(container(engine, "inspect", name).stdout)[0]


def make_backend(engine, provider, lifetime=300):
    gateway = CredentialGateway(provider, lifetime)
    if engine == "docker":
        return DockerSandboxBackend(
            DockerSandboxConfig(egress_proxy_image=PROXY, credential_gateway=gateway)
        )
    return WslcSandboxBackend(
        WslcSandboxConfig(egress_proxy_image=PROXY, credential_gateway=gateway)
    )


def spec(lifetime=300):
    return SandboxSpec(
        kind="credential-e2e",
        image=GUEST,
        requires=frozenset({Capability.EXEC, Capability.ATTACHED_IDENTITY}),
        egress=Egress.ALLOWLIST,
        egress_allow=(
            EgressRule(
                "api.example.com", methods=("GET",), paths=("/v1/*",), authority="api-audience"
            ),
        ),
        isolation_scope=IsolationScope.CALL,
        max_identity_scope=IdentityScope.PER_SANDBOX,
        max_identity_retention_seconds=lifetime,
    )


def certificate():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "api.example.com")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(UTC) - timedelta(minutes=1))
        .not_valid_after(datetime.now(UTC) + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName("api.example.com")]), critical=False
        )
        .sign(key, hashes.SHA256())
    )
    return cert.public_bytes(serialization.Encoding.PEM), key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )


@pytest.fixture(params=["docker", "wslc"])
def upstream(request):
    engine = request.param
    if engine not in ENGINES:
        pytest.skip("set MAF_CREDENTIAL_E2E to run real credential gateway checks")
    name = "maf-credential-fixture-" + uuid.uuid4().hex[:12]
    cert, key = certificate()
    container(
        engine,
        "run",
        "-d",
        "--name",
        name,
        GUEST,
        "sh",
        "-c",
        "until [ -f /tmp/ready ]; do sleep 0.1; done; exec python /tmp/server.py",
    )
    try:
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w") as archive:
            for filename, data in {
                "cert.pem": cert,
                "key.pem": key,
                "server.py": SERVER.encode(),
                "ready": b"",
            }.items():
                info = tarfile.TarInfo(filename)
                info.size = len(data)
                archive.addfile(info, io.BytesIO(data))
        container(
            engine, "exec", "-i", name, "tar", "xf", "-", "-C", "/tmp", data=buffer.getvalue()
        )
        ip = inspect(engine, name)["NetworkSettings"]["Networks"]["bridge"]["IPAddress"]
        yield engine, name, ip, cert
    finally:
        container(engine, "rm", "-f", name, check=False)


def trust_fixture(engine, name, ip, cert):
    proxy = name + "-proxy"
    container(
        engine,
        "exec",
        "-i",
        "-u",
        "0",
        proxy,
        "sh",
        "-c",
        "cat >> /etc/ssl/certs/ca-certificates.crt",
        data=cert,
    )
    container(
        engine,
        "exec",
        "-i",
        "-u",
        "0",
        proxy,
        "sh",
        "-c",
        "cat >> /etc/hosts",
        data=f"{ip} api.example.com\n".encode(),
    )


def probe(engine, name, method="GET", path="/v1/who", delay=None):
    args = ["exec", name, "python", "-c", PROBE, method, path]
    if delay is not None:
        args.append(str(delay))
    result = container(engine, *args)
    return json.loads(result.stdout)


def test_users_agents_calls_containers_and_host_replicas(upstream):
    engine, _, ip, cert = upstream
    requests = []
    expected = {}

    async def provider(request):
        requests.append(request)
        token = "synthetic-" + request.generation
        expected[request.instance_id] = hashlib.sha256(("Bearer " + token).encode()).hexdigest()
        return [
            CredentialGrant(
                "api-audience", "https://api.example.com:8443", token, request.expires_at
            )
        ]

    async def check():
        a, b = make_backend(engine, provider), make_backend(engine, provider)
        scope = "test-" + uuid.uuid4().hex
        keys = [
            SandboxKey(scope + "/alice", "thread", "agent-a", "call-1"),
            SandboxKey(scope + "/bob", "thread", "agent-a", "call-1"),
            SandboxKey(scope + "/alice", "thread", "agent-b", "call-1"),
            SandboxKey(scope + "/alice", "thread", "agent-a", "call-2"),
        ]
        assignments = [(a, key) for key in keys] + [(b, keys[0])]
        sandboxes = []
        try:
            # Each acquire runs concurrently; two host replicas deliberately receive the same key.
            results = await asyncio.gather(
                *(backend.acquire(key, spec()) for backend, key in assignments),
                return_exceptions=True,
            )
            sandboxes = [result for result in results if not isinstance(result, BaseException)]
            assert len(sandboxes) == 5, [type(result).__name__ for result in results]
            assert len({s.instance_id for s in sandboxes}) == 5
            assert len({request.generation for request in requests}) == 5
            for sandbox in sandboxes:
                trust_fixture(engine, sandbox.container_name, ip, cert)
                response = probe(engine, sandbox.container_name)
                assert response[:2] == [200, expected[sandbox.instance_id]]
                assert probe(engine, sandbox.container_name, path="/admin")[0] == 403
                assert probe(engine, sandbox.container_name, method="POST")[0] == 403
                metadata = json.dumps(inspect(engine, sandbox.instance_id)) + json.dumps(
                    inspect(engine, sandbox.container_name + "-proxy")
                )
                assert all(
                    "synthetic-" + request.generation not in metadata for request in requests
                )
                hidden = container(
                    engine,
                    "exec",
                    sandbox.container_name,
                    "sh",
                    "-c",
                    "test ! -e /run/maf-proxy/grant.json && test ! -e /run/maf-proxy/ca.key",
                )
                assert hidden.returncode == 0
            foreign = inspect(engine, sandboxes[1].container_name + "-proxy")
            targets = [(ip, 8443)] + [
                (network["IPAddress"], 3128)
                for network in foreign["NetworkSettings"]["Networks"].values()
                if network.get("IPAddress")
            ]
            direct = container(
                engine,
                "exec",
                sandboxes[0].container_name,
                "python",
                "-c",
                "import json,socket,sys\n"
                "for host,port in json.loads(sys.argv[1]):\n"
                " try: socket.create_connection((host,port),timeout=0.5); print('bypass')\n"
                " except OSError: print('denied')\n",
                json.dumps(targets),
            )
            assert direct.stdout.decode().splitlines() == ["denied"] * len(targets)
            # Disposing one generation cannot revoke a sibling host's same-key grant.
            assert (
                await a.dispose(keys[0], kind=spec().kind, instance_id=sandboxes[0].instance_id)
                is None
            )
            assert probe(engine, sandboxes[4].container_name)[0] == 200
            # A restarted gateway cannot restore the old immutable grant file.
            container(engine, "restart", sandboxes[1].container_name + "-proxy")
            assert probe(engine, sandboxes[1].container_name)[0] != 200
        finally:
            for backend, key in assignments:
                await backend.dispose(key, kind=spec().kind)

    asyncio.run(check())


def test_cancellation_during_authorization_removes_only_its_generation(upstream):
    engine, _, ip, cert = upstream

    async def check():
        entered = asyncio.Event()

        async def blocked(request):
            entered.set()
            await asyncio.Future()
            return []

        backend = make_backend(engine, blocked)
        key = SandboxKey("cancel-" + uuid.uuid4().hex, "thread", "agent", "call")
        task = asyncio.create_task(backend.acquire(key, spec()))
        try:
            await asyncio.wait_for(entered.wait(), 60)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            listed = container(
                engine, "list", "-a", "--filter", "label=maf-sandbox.scope=" + key.scope
            )
            assert key.scope.encode() not in listed.stdout
            # Query by the unique generation label scope and ask only for IDs.
            if engine == "docker":
                assert not container(
                    engine, "list", "-aq", "--filter", "label=maf-sandbox.scope=" + key.scope
                ).stdout.strip()
            else:
                listed_json = container(
                    engine,
                    "list",
                    "-a",
                    "--filter",
                    "label=maf-sandbox.scope=" + key.scope,
                    "--format",
                    "json",
                ).stdout
                rows = json.loads(listed_json or b"[]")
                assert not rows
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            await backend.dispose(key, kind=spec().kind)

    asyncio.run(check())


def test_host_process_exit_expires_an_existing_tls_connection(upstream):
    engine, _, ip, cert = upstream
    child = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), engine],
        capture_output=True,
        check=True,
        timeout=90,
    )
    record = json.loads(child.stdout)
    name = record["name"]
    try:
        trust_fixture(engine, name, ip, cert)
        responses = probe(engine, name, delay=max(0, record["expires_at"] - time.time() + 0.2))
        assert responses[0][0] == 200, responses
        assert responses[1][0] == 502, responses
        assert responses[0][2] == responses[1][2], (
            "the expiry check must reuse the same TLS connection"
        )
        assert probe(engine, name)[0] == 502
    finally:
        container(engine, "rm", "-f", name + "-proxy", name, check=False)
        cli(engine, "network", "rm", name + "-net", check=False)


def test_codeact_calls_receive_distinct_grants_and_dispose_them(upstream):
    engine, _, ip, cert = upstream

    async def check():
        seen = []

        async def provider(request):
            seen.append(request)
            name = inspect(engine, request.instance_id)["Name"].removeprefix("/")
            trust_fixture(engine, name, ip, cert)
            return [
                CredentialGrant(
                    "api-audience",
                    "https://api.example.com:8443",
                    "synthetic-codeact-" + request.generation,
                    request.expires_at,
                )
            ]

        backend = make_backend(engine, provider)
        router = SandboxRouter(
            [backend], min_isolation=backend.isolation, max_identity_scope=IdentityScope.PER_SANDBOX
        )
        scope = "codeact-" + uuid.uuid4().hex

        async def list_files(_store: object) -> list[ListedFile]:
            return []

        context = CallerContext(
            current_scope=lambda: scope, current_thread_id=lambda: "thread", list_files=list_files
        )
        tool = make_codeact_tools(
            router,
            "agent",
            context,
            image=GUEST,
            egress_allow=spec().egress_allow,
            credential_retention_seconds=300,
        )[0]
        body = getattr(tool, "func", None) or getattr(tool, "__wrapped__", None) or tool
        try:
            answers = await asyncio.gather(
                *(
                    body(
                        code="import urllib.request\nprint(urllib.request.urlopen('https://api.example.com:8443/v1/who').status)"
                    )
                    for _ in range(2)
                )
            )
            for answer in answers:
                text = (
                    answer
                    if isinstance(answer, str)
                    else "\n".join(str(item.text) for item in answer)
                )
                assert "200" in text and "Result: ok" in text, text
            assert len(seen) == 2
            assert len({request.key.call_id for request in seen}) == 2
            assert len({request.instance_id for request in seen}) == 2
            assert all(
                request.key.scope == scope and request.key.agent_id == "agent" for request in seen
            )
            for request in seen:
                assert (
                    container(engine, "inspect", request.instance_id, check=False).returncode != 0
                )
        finally:
            await backend.dispose_scope(scope, "thread")

    asyncio.run(check())


if __name__ == "__main__":

    async def owner():
        async def issue(request):
            return [
                CredentialGrant(
                    "api-audience",
                    "https://api.example.com:8443",
                    "synthetic-orphan",
                    request.expires_at,
                )
            ]

        backend = make_backend(sys.argv[1], issue, 15)
        key = SandboxKey("orphan-" + uuid.uuid4().hex, "thread", "agent", "call")
        started = time.time()
        sandbox = await backend.acquire(key, spec(15))
        print(json.dumps({"name": sandbox.container_name, "expires_at": started + 15}), flush=True)
        os._exit(0)

    asyncio.run(owner())
