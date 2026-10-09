"""Offline refusal and workflow tests for the opt-in Graphviz SDK qualification."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import struct
import sys
import zlib
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "samples/07_docker_diagram"
sys.path.insert(0, str(SAMPLE))
SPEC = importlib.util.spec_from_file_location("graphviz_qualification", SAMPLE / "qualify.py")
assert SPEC and SPEC.loader
qualification = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(qualification)


def chunk(kind, value):
    return (
        struct.pack(">I", len(value)) + kind + value + struct.pack(">I", zlib.crc32(kind + value))
    )


def png(
    *,
    pixels=b"\0\xff\0\0",
    width=1,
    height=1,
    depth=8,
    color=2,
    compression=0,
    filtering=0,
    interlace=0,
):

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(
            b"IHDR",
            struct.pack(">IIBBBBB", width, height, depth, color, compression, filtering, interlace),
        )
        + chunk(b"IDAT", zlib.compress(pixels))
        + chunk(b"IEND", b"")
    )


@pytest.mark.parametrize(
    "damage",
    [
        lambda p: p[:33],
        lambda p: p[:-1],
        lambda p: p + b"junk",
        lambda p: p[:45] + bytes([p[45] ^ 1]) + p[46:],
    ],
)
def test_png_refuses_incomplete_or_corrupt_artifacts(damage):
    with pytest.raises(ValueError):
        qualification.png_details(damage(png()))


def test_png_records_verified_dimensions_and_digest():
    value = qualification.png_details(png())
    assert value["width"] == value["height"] == 1
    assert len(value["sha256"]) == 64


@pytest.mark.parametrize("fault", ["image", "network", "extra-network", "stopped", "missing"])
def test_engine_refuses_wrong_identity_or_network(fault):
    value = {
        "Config": {"Image": "selected", "User": "1000"},
        "Image": "config",
        "State": {"Running": True},
        "HostConfig": {"NetworkMode": "none", "ReadonlyRootfs": False, "CapDrop": ["ALL"]},
        "NetworkSettings": {"Networks": {"none": {}}},
    }
    if fault == "image":
        value["Image"] = "different"
    elif fault == "network":
        value["HostConfig"]["NetworkMode"] = "bridge"
    elif fault == "extra-network":
        value["NetworkSettings"]["Networks"]["bridge"] = {}
    elif fault == "stopped":
        value["State"]["Running"] = False
    engine = qualification.Engine.__new__(qualification.Engine)
    engine.owned = lambda _: [] if fault == "missing" else ["container"]
    engine.command = lambda *args: json.dumps([value])
    with pytest.raises(ValueError):
        engine.inspect("scope", "selected", "config")


def test_source_installs_cannot_claim_published_sdk(monkeypatch):
    package = SimpleNamespace(
        metadata={"Name": "maf-sandbox"},
        version="0.48.0",
        read_text=lambda _: '{"dir_info":{"editable":true}}',
    )
    monkeypatch.setattr(qualification, "distributions", lambda: [package])
    with pytest.raises(ValueError, match="published"):
        qualification.installed_packages()


@pytest.mark.parametrize(
    "fault", [None, "render-leftover", "timeout-leftover", "no-timeout", "external-network"]
)
def test_cleanup_recovery_cannot_turn_sdk_failure_into_success(tmp_path, monkeypatch, fault):
    policy = json.loads((SAMPLE / "graphviz-policy.json").read_text(encoding="utf-8"))
    image = f"{qualification.PREFIX}/graphviz@{policy['registryDigest']}"
    owned = []
    purged = []

    class Engine:
        context = "qualification"

        def command(self, *args, **kwargs):
            if args[0] == "version":
                return json.dumps({"Version": "test", "Os": "linux", "Arch": "amd64"})
            if args[0] == "image":
                return json.dumps([{"RepoDigests": [image], "Id": policy["imageId"]}])
            return ""

        def owned(self, _scope):
            return list(owned)

    class Sandbox:
        async def exec(self, command, **kwargs):
            if command[0] == "sh":
                return SimpleNamespace(
                    exit_code=0,
                    stdout="/bin/sleep\ninterface:lo:0x9\ninterface:tunl0:0x80\n"
                    + ("interface:eth0:0x1\n" if fault == "external-network" else ""),
                )
            if fault != "timeout-leftover":
                owned.clear()
            if fault != "no-timeout":
                raise TimeoutError
            return SimpleNamespace(exit_code=0, stdout="")

    class Router:
        def __init__(self, *args):
            self.observations = []

        async def acquire(self, *args):
            owned.append("container")
            self.observations.append({"containerId": "container"})
            return Sandbox()

        async def dispose_scope(self, *args):
            purged.extend(owned)
            owned.clear()

    def tools(router, agent, context, sink, **kwargs):
        async def render(dot):
            await router.acquire()
            valid = "ingest" in dot
            if fault != "render-leftover" or not valid:
                owned.clear()
            if valid:
                directory = tmp_path / "render"
                directory.mkdir()
                (directory / "diagram.png").write_bytes(png())
                return "artifact:diagram.png"
            return "dot could not render the diagram (exit 1): syntax error"

        return [render]

    monkeypatch.setattr(qualification, "Engine", Engine)
    monkeypatch.setattr(qualification, "ObservedRouter", Router)
    monkeypatch.setattr(qualification, "DockerSandboxBackend", lambda _: None)
    monkeypatch.setattr(qualification, "make_diagram_tools", tools)
    result = {}
    if fault:
        with pytest.raises(ValueError):
            asyncio.run(qualification.exercise(policy, tmp_path, result))
        if "leftover" in fault:
            assert purged == ["container"]
    else:
        asyncio.run(qualification.exercise(policy, tmp_path, result))
        assert all(check["passed"] for check in result["checks"].values())
    assert result["cleanup"]["containersRemaining"] == []


def test_failed_identity_never_reaches_docker_and_retains_failure(tmp_path, monkeypatch):
    output = tmp_path / "output"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "qualify",
            "--policy",
            str(SAMPLE / "graphviz-policy.json"),
            "--evidence",
            str(tmp_path),
            "--output",
            str(output),
        ],
    )
    monkeypatch.setattr(qualification, "installed_packages", lambda: {})
    monkeypatch.setattr(qualification.subprocess, "check_output", lambda *a, **k: "source")

    def refuse(*args):
        raise ValueError("Untrusted release")

    monkeypatch.setattr(qualification, "verify_release", refuse)
    monkeypatch.setattr(qualification, "exercise", lambda *a: pytest.fail("Image code executed"))
    with pytest.raises(ValueError, match="Untrusted"):
        qualification.main()
    report = json.loads((output / "qualification.json").read_text())
    assert report["passed"] is False
    assert report["failureType"] == "ValueError"


def test_manual_workflow_installs_published_wheels_and_keeps_failure_evidence():
    workflow = yaml.safe_load(
        (ROOT / ".github/workflows/graphviz-sdk-qualification.yml").read_text()
    )
    assert set(workflow.get("on", workflow.get(True))) == {"workflow_dispatch"}
    job = workflow["jobs"]["qualify"]
    assert "refs/heads/main" in job["if"]
    commands = "\n".join(step.get("run", "") for step in job["steps"])
    assert "uv sync" not in commands and "docker build" not in commands
    assert "uv run --isolated --no-project --python 3.12" in commands
    source = (SAMPLE / "qualify.py").read_text(encoding="utf-8")
    assert "maf-sandbox==0.48.0" in source and "maf-sandbox-docker==0.27.0" in source
    upload = job["steps"][-1]
    assert upload["if"] == "always()" and upload["with"]["if-no-files-found"] == "error"


@pytest.mark.parametrize(
    "status", ["clean", "stale", "unavailable", "vulnerable", "no-longer-monitored"]
)
def test_monitoring_is_reported_separately_from_verified_identity(monkeypatch, status):
    verified = {"releaseIdentityVerified": True, "monitoringStatus": status}
    monkeypatch.setattr(
        qualification.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(stdout=json.dumps(verified)),
    )
    assert qualification.verify_release(Path("policy"), Path("evidence")) == verified


@pytest.mark.parametrize(
    "change",
    [
        {"pixels": b"\0"},
        {"pixels": b"\0\xff\0\0extra"},
        {"height": 2},
        {"pixels": b"\x05\xff\0\0"},
        {"depth": 16},
        {"color": 3},
        {"compression": 1},
        {"filtering": 1},
        {"interlace": 1},
        {"width": 16 * 1024 * 1024},
    ],
)
def test_png_refuses_invalid_or_unsupported_scanlines(change):
    with pytest.raises(ValueError):
        qualification.png_details(png(**change))


@pytest.mark.parametrize("color,channels", [(2, 3), (6, 4)])
@pytest.mark.parametrize("filter_byte", range(5))
def test_png_accepts_complete_rgb_and_rgba_scanlines(color, channels, filter_byte):
    row = bytes([filter_byte]) + bytes(2 * channels)
    details = qualification.png_details(png(width=2, height=3, color=color, pixels=row * 3))
    assert (details["width"], details["height"]) == (2, 3)


@pytest.mark.parametrize("layout", ["header-after-data", "unknown-critical", "separated-data"])
def test_png_refuses_invalid_chunk_layout(layout):
    original = png()
    signature, header, image_data, end = (
        original[:8],
        original[8:33],
        original[33:-12],
        original[-12:],
    )
    if layout == "header-after-data":
        malformed = signature + image_data + header + end
    elif layout == "unknown-critical":
        malformed = signature + header + chunk(b"ABCD", b"") + image_data + end
    else:
        malformed = (
            signature
            + header
            + image_data
            + chunk(b"tEXt", b"key\0value")
            + chunk(b"IDAT", b"")
            + end
        )
    with pytest.raises(ValueError):
        qualification.png_details(malformed)
