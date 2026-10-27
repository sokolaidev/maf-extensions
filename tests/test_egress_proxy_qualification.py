"""Proxy release qualification refuses substitutions, policy bypass and incomplete cleanup."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from maf_sandbox.credentials import CredentialRequest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import egress_proxy_qualification as proxy  # noqa: E402
from check_container_release_image import check  # noqa: E402
from container_release_approval import UNAVAILABLE, evidence_rows  # noqa: E402

pytestmark = pytest.mark.workflow

IMAGE = "sha256:" + "a" * 64
GUEST = "sha256:" + "b" * 64


@pytest.mark.parametrize(
    "fault",
    [
        None,
        "image",
        "reference",
        "guest",
        "stopped",
        "extra-guest-network",
        "extra-proxy-network",
        "external",
        "gateway",
        "root",
        "capabilities",
        "added-capabilities",
        "privileged-mode",
        "privileges",
        "privileges-false-equals",
        "privileges-false-colon",
        "privileges-conflicting",
        "privileges-lookalike",
    ],
)
def test_observation_refuses_identity_and_network_drift(monkeypatch, fault):
    guest = {
        "Image": GUEST,
        "Config": {"Image": GUEST},
        "State": {"Running": True},
        "Id": "guest",
        "NetworkSettings": {"Networks": {"guest-net": {}}},
    }
    sidecar = {
        "Image": IMAGE,
        "Config": {"Image": IMAGE, "User": "65532:65532"},
        "State": {"Running": True},
        "Id": "proxy",
        "HostConfig": {
            "CapDrop": ["ALL"],
            "CapAdd": None,
            "Privileged": False,
            "SecurityOpt": ["no-new-privileges"],
        },
        "NetworkSettings": {"Networks": {"guest-net": {}, "bridge": {}}},
    }
    network = {"Internal": True, "Driver": "bridge", "IPAM": {"Config": [{}]}}
    if fault == "image":
        sidecar["Image"] = GUEST
    if fault == "reference":
        sidecar["Config"]["Image"] = "mutable:tag"
    if fault == "guest":
        guest["Image"] = IMAGE
    if fault == "stopped":
        sidecar["State"]["Running"] = False
    if fault == "extra-guest-network":
        guest["NetworkSettings"]["Networks"]["bridge"] = {}
    if fault == "extra-proxy-network":
        sidecar["NetworkSettings"]["Networks"]["foreign"] = {}
    if fault == "external":
        network["Internal"] = False
    if fault == "gateway":
        network["IPAM"]["Config"] = [{"Gateway": "172.18.0.1"}]
    if fault == "root":
        sidecar["Config"]["User"] = "0"
    if fault == "capabilities":
        sidecar["HostConfig"]["CapDrop"] = []
    if fault == "added-capabilities":
        sidecar["HostConfig"]["CapAdd"] = ["NET_ADMIN"]
    if fault == "privileged-mode":
        sidecar["HostConfig"]["Privileged"] = True
    if fault == "privileges":
        sidecar["HostConfig"]["SecurityOpt"] = []
    if fault == "privileges-false-equals":
        sidecar["HostConfig"]["SecurityOpt"] = ["no-new-privileges=false"]
    if fault == "privileges-false-colon":
        sidecar["HostConfig"]["SecurityOpt"] = ["no-new-privileges:false"]
    if fault == "privileges-conflicting":
        sidecar["HostConfig"]["SecurityOpt"] = ["no-new-privileges", "no-new-privileges=false"]
    if fault == "privileges-lookalike":
        sidecar["HostConfig"]["SecurityOpt"] = ["no-new-privileges-extra"]
    monkeypatch.setattr(
        proxy, "inspect", lambda name: copy.deepcopy(sidecar if name.endswith("-proxy") else guest)
    )
    monkeypatch.setattr(proxy, "docker", lambda *args: json.dumps([network]).encode())
    if fault:
        with pytest.raises(ValueError):
            proxy.observation("guest", IMAGE, GUEST)
    else:
        result = proxy.observation("guest", IMAGE, GUEST)
        assert result["proxyImageId"] == IMAGE
        assert result["capabilitiesAdded"] == []
        assert result["privileged"] is False
        sidecar["HostConfig"]["CapAdd"] = []
        assert proxy.observation("guest", IMAGE, GUEST) == result


@pytest.mark.parametrize(
    "fault",
    [
        None,
        "allow",
        "strip",
        "path",
        "method",
        "host",
        "audit",
        "bypass",
        "metadata",
        "logs",
        "expiry",
        "dispose",
        "containers",
        "networks",
        "origin",
    ],
)
def test_qualification_requires_every_case_and_always_cleans_up(monkeypatch, fault):
    token = ""
    expired = False
    removed = []
    disposed = []

    class Backend:
        def __init__(self, config):
            self.config = config

        async def acquire(self, owner, spec):
            nonlocal token
            request = CredentialRequest(
                owner,
                spec.kind,
                "instance",
                "generation",
                frozenset({"release-audience"}),
                time.time() + 120,
            )
            [grant] = await self.config.credential_gateway.provider(request)
            token = grant.token
            return SimpleNamespace(container_name="guest", instance_id="instance")

        async def dispose_scope(self, *args):
            disposed.append(args)
            return SimpleNamespace(undisposed=["failed"] if fault == "dispose" else [])

    monkeypatch.setattr(proxy.DockerSandboxBackend, "create", AsyncMock(side_effect=Backend))
    monkeypatch.setattr(proxy, "certificate", lambda: (b"test-ca", b"test-key"))
    monkeypatch.setattr(proxy, "observation", lambda *args: {"proxyImageId": IMAGE})
    monkeypatch.setattr(proxy, "runtime", lambda *args: {"sha256": {"binary": "c" * 64}})
    monkeypatch.setattr(
        proxy,
        "remaining",
        lambda scope: (
            ["left"] if fault == "containers" else [],
            ["left"] if fault == "networks" else [],
        ),
    )
    monkeypatch.setattr(
        proxy,
        "inspect",
        lambda name: {"Id": GUEST, "metadata": token if fault == "metadata" else "redacted"},
    )

    def origin(*args):
        if fault == "origin":
            raise ValueError("origin failed")
        return "172.18.0.2"

    monkeypatch.setattr(proxy, "start_origin", origin)

    def docker(*args, **kwargs):
        if args[0] == "version":
            return json.dumps({"Version": "29", "Os": "linux", "Arch": "amd64"}).encode()
        if args[0] == "ps":
            return b"origin-id\n"
        if args[0] == "rm":
            removed.append(args)
            return b""
        if args[0] == "exec" and "python" in args:
            return b"bypass" if fault == "bypass" else b"denied"
        return b""

    monkeypatch.setattr(proxy, "docker", docker)

    def probe(name, host="api.example.com", method="GET", path="/v1/who"):
        if expired:
            return [200 if fault == "expiry" else 403, ""]
        if host == "denied.example.com":
            return [200 if fault == "host" else 0, "OSError"]
        if path == "/admin":
            return [200 if fault == "path" else 403, ""]
        if method == "POST":
            return [200 if fault == "method" else 403, ""]
        if host == "other.example.com":
            return [200, "wrong" if fault == "strip" else hashlib.sha256(b"").hexdigest()]
        return [
            200,
            "wrong"
            if fault == "allow"
            else hashlib.sha256(("Bearer " + token).encode()).hexdigest(),
        ]

    monkeypatch.setattr(proxy, "probe", probe)
    monkeypatch.setattr(
        proxy.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(
            [], 0, (token.encode() if fault == "logs" else b""), b""
        ),
    )
    monkeypatch.setattr(
        proxy,
        "read_decisions",
        lambda *args: (
            ()
            if fault == "audit"
            else (SimpleNamespace(host="denied.example.com", decision="DENY"),),
            False,
        ),
    )

    async def sleep(delay):
        nonlocal expired
        expired = True

    monkeypatch.setattr(proxy.asyncio, "sleep", sleep)
    if fault:
        with pytest.raises(ValueError):
            asyncio.run(proxy.check(IMAGE))
    else:
        result = asyncio.run(proxy.check(IMAGE))
        assert len(result["checks"]) == 8
        assert all(case["passed"] for case in result["checks"])
        assert result["containersRemaining"] == result["networksRemaining"] == []
        assert token not in json.dumps(result)
    assert len(disposed) == 1
    assert removed == [("rm", "-f", "origin-id")]


def test_release_dispatch_and_approval_require_profile_evidence(monkeypatch, tmp_path):
    result = {
        "containersRemaining": [],
        "networksRemaining": [],
        "checks": [{"name": "denied-path", "passed": True}],
    }
    checker = AsyncMock(return_value=result)
    monkeypatch.setattr(proxy, "check", checker)
    runtime = check("egress-proxy", IMAGE, tmp_path)
    checker.assert_awaited_once_with(IMAGE)
    assert runtime["kind"] == "controlled-docker-consumer"
    assert runtime["egressProxy"] == result
    candidate = {"profile": "egress-proxy", "imageId": IMAGE}
    rows = dict(evidence_rows({"runtime.json": runtime}, candidate))
    assert rows["Proxy consumer checks"] == result["checks"]
    assert rows["Proxy runtime identity and notices"] == UNAVAILABLE
    checker.side_effect = ValueError("failed runtime")
    with pytest.raises(ValueError, match="failed runtime"):
        check("egress-proxy", IMAGE, tmp_path)


def test_mutable_proxy_rejected_before_engine_access(monkeypatch):
    monkeypatch.setattr(proxy, "docker", lambda *args, **kwargs: pytest.fail("mutable execution"))
    with pytest.raises(ValueError):
        asyncio.run(proxy.check("proxy:latest"))


@pytest.mark.parametrize(
    "fault", [None, "patch", "compiler", "module", "notice", "grpc-host", "net-host"]
)
def test_runtime_checks_notices_and_actual_build_versions(monkeypatch, fault):
    files = {
        "/usr/local/bin/iron-proxy": "c" * 64,
        "/licenses/iron-proxy-LICENSE": "d" * 64,
        "/licenses/maf-extensions-LICENSE": hashlib.sha256(
            (proxy.ROOT / "LICENSE").read_bytes()
        ).hexdigest(),
        "/usr/share/maf-egress-proxy/iron.patch": hashlib.sha256(
            (proxy.PROXY_SOURCE / "iron.patch").read_bytes()
        ).hexdigest(),
        "/usr/share/maf-egress-proxy/credential_gate.go": hashlib.sha256(
            (proxy.PROXY_SOURCE / "credential_gate.go").read_bytes()
        ).hexdigest(),
        "/licenses/Go-LICENSE": "e" * 64,
        "/usr/share/maf-egress-proxy/build-info.txt": "f" * 64,
    }
    recipe = (proxy.PROXY_SOURCE / "Dockerfile").read_text(encoding="utf-8")
    pinned = proxy.re.search(r"FROM golang:([0-9.]+)-", recipe)
    assert pinned is not None
    compiler = pinned[1]
    manifest = (proxy.PROXY_SOURCE / "go.mod").read_text(encoding="utf-8")
    modules = proxy.re.findall(
        r"^\s*(google\.golang\.org/grpc|golang\.org/x/net)\s+(v[0-9.]+)(?:\s|$)",
        manifest,
        proxy.re.MULTILINE,
    )
    if fault in {"grpc-host", "net-host"}:
        module = "google.golang.org/grpc" if fault == "grpc-host" else "golang.org/x/net"
        lookalike = module.replace(".", "x")
        modules = [(lookalike if name == module else name, value) for name, value in modules]
        manifest = manifest.replace(module, lookalike)
        read_text = Path.read_text
        monkeypatch.setattr(
            Path,
            "read_text",
            lambda path, *args, **kwargs: (
                manifest
                if path == proxy.PROXY_SOURCE / "go.mod"
                else read_text(path, *args, **kwargs)
            ),
        )
    if fault == "patch":
        files["/usr/share/maf-egress-proxy/iron.patch"] = "0" * 64
    if fault == "compiler":
        compiler = "0.0.0"
    if fault == "module":
        modules = [(name, "v0.0.0") for name, _ in modules]
    build = f"/iron-proxy: go{compiler}\n" + "".join(
        f"\tdep\t{name}\t{value}\tchecksum\n" for name, value in modules
    )

    def docker(*args):
        if "sha256sum" in args:
            return "\n".join(f"{value}  {name}" for name, value in files.items()).encode()
        if "cat" in args:
            return build.encode()
        if fault == "notice":
            raise subprocess.CalledProcessError(1, ["test", "-s"])
        return b""

    monkeypatch.setattr(proxy, "docker", docker)
    if fault:
        with pytest.raises((ValueError, subprocess.CalledProcessError)):
            proxy.runtime("proxy")
    else:
        assert proxy.runtime("proxy")["goVersion"] == f"go{compiler}"
