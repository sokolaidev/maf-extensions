"""Qualify the release proxy through Docker using a controlled TLS origin and synthetic grants."""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import re
import subprocess
import tarfile
import time
from datetime import UTC, datetime, timedelta
from importlib.metadata import version
from typing import Any
from uuid import uuid4

from container_release import ROOT, require_digest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from maf_sandbox import (
    Capability,
    Egress,
    EgressRule,
    IdentityScope,
    IsolationScope,
    SandboxKey,
    SandboxSpec,
)
from maf_sandbox.credentials import CredentialGateway, CredentialGrant, CredentialRequest
from maf_sandbox_docker import DockerSandboxBackend, DockerSandboxConfig
from maf_sandbox_docker._proxy.policy import read_decisions

GUEST = "python@sha256:8d9d0b8bcf6506481eae4907c18f5e3e7902e629f5f6d684f9e7c32e85e3ddf0"
PROXY_SOURCE = ROOT / "packages/maf-sandbox-docker/src/maf_sandbox_docker/_proxy"
SERVER = """import hashlib, ssl
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
class Handler(BaseHTTPRequestHandler):
 def do_GET(self):
  body = hashlib.sha256(self.headers.get('Authorization', '').encode()).hexdigest().encode()
  self.send_response(200); self.send_header('Content-Length', str(len(body))); self.end_headers(); self.wfile.write(body)
 def log_message(self, *args): pass
server = ThreadingHTTPServer(('0.0.0.0', 8443), Handler)
ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); ctx.load_cert_chain('/tmp/cert.pem', '/tmp/key.pem')
server.socket = ctx.wrap_socket(server.socket, server_side=True); server.serve_forever()
"""
PROBE = """import http.client, json, os, ssl, sys
from urllib.parse import urlsplit
proxy = urlsplit(os.environ['HTTPS_PROXY'])
client = http.client.HTTPSConnection(proxy.hostname, proxy.port, context=ssl.create_default_context(), timeout=5)
client.set_tunnel(sys.argv[1], 8443)
try:
 client.request(sys.argv[2], sys.argv[3], headers={'Authorization': 'Bearer guest-placeholder'})
 response = client.getresponse(); print(json.dumps([response.status, response.read().decode()]))
except OSError as error:
 print(json.dumps([0, type(error).__name__]))
finally: client.close()
"""


def require(condition: bool, message: str) -> None:
    """Keep release gates active under optimized Python."""
    if not condition:
        raise ValueError(message)


def docker(*args: str, data: bytes | None = None, timeout: int = 90) -> bytes:
    """Keep fixture keys and synthetic credentials out of command lines and retained output."""
    return subprocess.run(
        ["docker", *args], input=data, capture_output=True, check=True, timeout=timeout
    ).stdout


def inspect(name: str) -> dict[str, Any]:
    """Read the engine object without retaining environment or labels."""
    return json.loads(docker("inspect", name))[0]


def remaining(scope: str) -> tuple[list[str], list[str]]:
    """Include stopped containers and detached networks in cleanup evidence."""
    selector = f"label=maf-sandbox.scope={scope}"
    return (
        docker("ps", "-aq", "--filter", selector).decode().split(),
        docker("network", "ls", "-q", "--filter", selector).decode().split(),
    )


def certificate() -> tuple[bytes, bytes]:
    """Create a disposable CA for the controlled origin only."""
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
            x509.SubjectAlternativeName(
                [x509.DNSName("api.example.com"), x509.DNSName("other.example.com")]
            ),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    return cert.public_bytes(serialization.Encoding.PEM), key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )


def start_origin(name: str, guest_id: str, scope: str, cert: bytes, key: bytes) -> str:
    """Start the digest-pinned fixture and wait for its TLS listener."""
    docker(
        "run",
        "-d",
        "--name",
        name,
        "--label",
        f"maf-sandbox.scope={scope}",
        "--network",
        "bridge",
        "--read-only",
        "--tmpfs",
        "/tmp:rw,noexec,size=16m",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--memory",
        "128m",
        "--pids-limit",
        "64",
        "--cpus",
        "1",
        "--pull=never",
        guest_id,
        "sh",
        "-c",
        "until [ -f /tmp/ready ]; do sleep 0.1; done; exec python /tmp/server.py",
    )
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w") as bundle:
        for filename, content in {
            "cert.pem": cert,
            "key.pem": key,
            "server.py": SERVER.encode(),
            "ready": b"",
        }.items():
            item = tarfile.TarInfo(filename)
            item.size = len(content)
            item.mode = 0o600
            bundle.addfile(item, io.BytesIO(content))
    docker("exec", "-i", name, "tar", "xf", "-", "-C", "/tmp", data=archive.getvalue())
    docker(
        "exec",
        name,
        "python",
        "-c",
        "import socket,time\n"
        "for _ in range(100):\n"
        " try: socket.create_connection(('127.0.0.1',8443),timeout=0.2).close(); break\n"
        " except OSError: time.sleep(0.1)\n"
        "else: raise RuntimeError('origin did not start')",
    )
    return inspect(name)["NetworkSettings"]["Networks"]["bridge"]["IPAddress"]


def observation(name: str, image_id: str, guest_id: str) -> dict[str, Any]:
    """Require the selected proxy and a guest whose only network is the internal bridge."""
    guest, proxy = inspect(name), inspect(name + "-proxy")
    network = name + "-net"
    net = json.loads(docker("network", "inspect", network))[0]
    require(proxy["Image"] == proxy["Config"]["Image"] == image_id, "Proxy image substitution")
    require(guest["Image"] == guest["Config"]["Image"] == guest_id, "Guest image substitution")
    require(
        proxy["State"]["Running"] and guest["State"]["Running"], "Qualification container stopped"
    )
    require(set(guest["NetworkSettings"]["Networks"]) == {network}, "Guest has another network")
    require(
        set(proxy["NetworkSettings"]["Networks"]) == {network, "bridge"},
        "Unexpected proxy networks",
    )
    require(net["Internal"] is True and net["Driver"] == "bridge", "Guest bridge is not internal")
    require(
        not any(entry.get("Gateway") for entry in net["IPAM"]["Config"]),
        "Guest bridge has a host gateway",
    )
    require(proxy["Config"]["User"] == "65532:65532", "Proxy is not the unprivileged release user")
    require("ALL" in proxy["HostConfig"]["CapDrop"], "Proxy retains capabilities")
    require(not proxy["HostConfig"]["CapAdd"], "Proxy adds capabilities")
    require(proxy["HostConfig"]["Privileged"] is False, "Proxy is privileged")
    require(
        proxy["HostConfig"]["SecurityOpt"] == ["no-new-privileges"],
        "Proxy can acquire privileges",
    )
    return {
        "proxyImageId": image_id,
        "guestImageId": guest_id,
        "proxyContainerId": proxy["Id"],
        "guestContainerId": guest["Id"],
        "internalBridge": True,
        "hostGateway": False,
        "proxyUser": "65532:65532",
        "capabilitiesDropped": "ALL",
        "capabilitiesAdded": [],
        "privileged": False,
        "noNewPrivileges": True,
    }


def runtime(proxy: str) -> dict[str, Any]:
    """Bind the executable, notices and policy source to retained hashes."""
    files = [
        "/usr/local/bin/iron-proxy",
        "/licenses/iron-proxy-LICENSE",
        "/licenses/maf-extensions-LICENSE",
        "/usr/share/maf-egress-proxy/iron.patch",
        "/usr/share/maf-egress-proxy/credential_gate.go",
        "/licenses/Go-LICENSE",
        "/usr/share/maf-egress-proxy/build-info.txt",
    ]
    output = docker("exec", proxy, "sha256sum", *files).decode()
    recorded = {line.split(maxsplit=1)[1]: line.split()[0] for line in output.splitlines()}
    require(set(recorded) == set(files), "Missing proxy runtime or notices")
    for installed, source in {
        files[2]: ROOT / "LICENSE",
        files[3]: PROXY_SOURCE / "iron.patch",
        files[4]: PROXY_SOURCE / "credential_gate.go",
    }.items():
        require(
            recorded[installed] == hashlib.sha256(source.read_bytes()).hexdigest(),
            "Proxy notice or patch differs from source",
        )
    docker("exec", proxy, "test", "-s", files[1])
    docker("exec", proxy, "test", "-s", files[5])
    build = docker("exec", proxy, "cat", files[6]).decode()
    lines = build.splitlines()
    compiler = lines[0].split()[-1]
    modules = {
        parts[1]: parts[2]
        for line in lines[1:]
        if len(parts := line.split()) >= 3 and parts[0] == "dep"
    }
    recipe = (PROXY_SOURCE / "Dockerfile").read_text(encoding="utf-8")
    pinned = re.search(r"FROM golang:([0-9.]+)-", recipe)
    require(
        pinned is not None and compiler == "go" + pinned[1], "Proxy compiler differs from build pin"
    )
    manifest = (PROXY_SOURCE / "go.mod").read_text(encoding="utf-8")
    overrides = dict(
        re.findall(
            r"^\s*(google\.golang\.org/grpc|golang\.org/x/net)\s+(v[0-9.]+)(?:\s|$)",
            manifest,
            re.MULTILINE,
        )
    )
    require(
        len(overrides) == 2
        and all(modules.get(name) == value for name, value in overrides.items()),
        "Proxy modules differ from build pins",
    )
    return {
        "sha256": recorded,
        "goVersion": compiler,
        "modules": modules,
        "dependencyManifests": {
            name: hashlib.sha256((PROXY_SOURCE / name).read_bytes()).hexdigest()
            for name in ("go.mod", "go.sum")
        },
    }


def probe(
    name: str, host: str = "api.example.com", method: str = "GET", path: str = "/v1/who"
) -> list[Any]:
    """Send a guest request without exposing its synthetic authorization in output."""
    return json.loads(docker("exec", name, "python", "-c", PROBE, host, method, path))


async def check(image_id: str) -> dict[str, Any]:
    """Exercise exact-image allow/deny, credential replacement, expiry, isolation and cleanup."""
    require_digest(image_id)
    server = json.loads(docker("version", "--format", "{{json .Server}}"))
    require(server["Os"] == "linux" and server["Arch"] == "amd64", "Requires Linux/amd64 Docker")
    docker("pull", "--platform", "linux/amd64", GUEST, timeout=300)
    guest_id = inspect(GUEST)["Id"]
    require_digest(guest_id)
    scope = "egress-release-" + uuid4().hex
    origin = scope + "-origin"
    cert, key = certificate()
    token = "synthetic-" + uuid4().hex
    expiry = 0.0
    requests: list[CredentialRequest] = []

    async def provider(request: CredentialRequest) -> list[CredentialGrant]:
        nonlocal expiry
        requests.append(request)
        expiry = min(request.expires_at, time.time() + 30)
        return [CredentialGrant("release-audience", "https://api.example.com:8443", token, expiry)]

    backend = await DockerSandboxBackend.create(
        DockerSandboxConfig(
            egress_proxy_image=image_id,
            credential_gateway=CredentialGateway(provider, 120),
            memory="128m",
            cpus=1,
        )
    )
    owner = SandboxKey(scope, "thread", "agent", "call")
    spec = SandboxSpec(
        kind="egress-release",
        image=guest_id,
        requires=frozenset({Capability.EXEC, Capability.ATTACHED_IDENTITY}),
        egress=Egress.ALLOWLIST,
        egress_allow=(
            EgressRule(
                "api.example.com", methods=("GET",), paths=("/v1/*",), authority="release-audience"
            ),
            EgressRule("other.example.com"),
        ),
        isolation_scope=IsolationScope.CALL,
        max_identity_scope=IdentityScope.PER_SANDBOX,
        max_identity_retention_seconds=120,
    )
    checks = []
    try:
        ip = start_origin(origin, guest_id, scope, cert, key)
        sandbox = await backend.acquire(owner, spec)
        name = sandbox.container_name
        proxy = name + "-proxy"
        observed = observation(name, image_id, guest_id)
        identity = runtime(proxy)
        require(
            len(requests) == 1 and requests[0].instance_id == sandbox.instance_id,
            "Grant is not bound to this sandbox",
        )
        docker(
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
        docker(
            "exec",
            "-i",
            "-u",
            "0",
            proxy,
            "sh",
            "-c",
            "cat >> /etc/hosts",
            data=f"{ip} api.example.com other.example.com\n".encode(),
        )
        for label, host, method, path, expected in (
            (
                "allowed-credential-replacement",
                "api.example.com",
                "GET",
                "/v1/who",
                [200, hashlib.sha256(("Bearer " + token).encode()).hexdigest()],
            ),
            (
                "unbound-host-strips-authorization",
                "other.example.com",
                "GET",
                "/v1/who",
                [200, hashlib.sha256(b"").hexdigest()],
            ),
            ("denied-path", "api.example.com", "GET", "/admin", None),
            ("denied-method", "api.example.com", "POST", "/v1/who", None),
            ("denied-host", "denied.example.com", "GET", "/v1/who", None),
        ):
            answer = probe(name, host, method, path)
            require(
                answer == expected
                if expected
                else answer[0] == 403 or (label == "denied-host" and answer[0] == 0),
                f"Proxy check failed: {label}",
            )
            checks.append({"name": label, "passed": True})
        direct = docker(
            "exec",
            name,
            "python",
            "-c",
            "import socket,sys\n"
            "try: socket.create_connection((sys.argv[1],8443),timeout=1)\n"
            "except OSError: print('denied')\n"
            "else: print('bypass')",
            ip,
        )
        require(direct.strip() == b"denied", "Guest bypassed proxy to origin")
        checks.append({"name": "direct-origin-denied", "passed": True})
        docker(
            "exec",
            name,
            "sh",
            "-c",
            "test ! -e /run/maf-proxy/grant.json && test ! -e /run/maf-proxy/ca.key",
        )
        metadata = json.dumps(inspect(name)) + json.dumps(inspect(proxy))
        require(token not in metadata, "Credential exposed in engine metadata")
        logs = subprocess.run(
            ["docker", "logs", proxy], check=True, capture_output=True, timeout=30
        )
        require(token.encode() not in logs.stdout + logs.stderr, "Credential exposed in proxy logs")
        decisions, truncated = read_decisions((logs.stdout + logs.stderr).decode(), 1000)
        require(
            not truncated
            and any(
                item.host == "denied.example.com" and item.decision == "DENY" for item in decisions
            ),
            "Denied host lacks a policy refusal",
        )
        checks.append({"name": "credential-not-in-guest-metadata-or-logs", "passed": True})
        await asyncio.sleep(max(0, expiry - time.time() + 0.2))
        require(probe(name)[0] == 403, "Expired grant still authorizes traffic")
        checks.append({"name": "expired-grant-denied", "passed": True})
    finally:
        try:
            disposed = await backend.dispose_scope(scope, "thread")
            require(not disposed.undisposed, "Proxy backend cleanup failed")
        finally:
            # The origin uses this scope only for independent cleanup verification.
            found = docker("ps", "-aq", "--filter", f"name=^/{origin}$").decode().split()
            if found:
                docker("rm", "-f", *found)
        containers, networks = remaining(scope)
        require(not containers and not networks, "Proxy qualification left resources behind")
    return {
        "backend": "maf-sandbox-docker",
        "sdkSource": "checkout",
        "packages": {name: version(name) for name in ("maf-sandbox", "maf-sandbox-docker")},
        "docker": {key: server[key] for key in ("Version", "Os", "Arch")},
        "runtime": identity,
        "observation": observed,
        "checks": checks,
        "fixture": {
            "image": GUEST,
            "imageId": guest_id,
            "origin": "controlled HTTPS on Docker bridge",
            "customCaAndHosts": True,
            "credentials": "synthetic; values omitted",
            "policy": {
                "api.example.com": "GET /v1/* with bound authority",
                "other.example.com": "unbound allowlisted host",
            },
        },
        "containersRemaining": [],
        "networksRemaining": [],
    }
