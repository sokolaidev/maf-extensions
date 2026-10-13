"""Draw.io release probes reject wrong artifacts, engine state and incomplete cleanup."""

from __future__ import annotations

import asyncio
import hashlib
import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from agent_framework import Content
from maf_sandbox.maf import COMPLETED_TEXT

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import drawio_qualification as drawio  # noqa: E402
from container_release_approval import UNAVAILABLE, evidence_rows  # noqa: E402

IMAGE = "sha256:" + "a" * 64


def document(preserve=True, positioned=True):
    root = ET.fromstring(drawio._input(positioned))
    for cell in root.findall(".//mxCell[@vertex='1']"):
        shape = cell.find("mxGeometry")
        if shape is None:
            shape = ET.SubElement(cell, "mxGeometry")
        shape.attrib.update(
            width="180",
            height="90",
            x="900" if preserve and positioned else "0",
            y="900" if preserve and positioned else "0",
        )
    for cell in root.findall(".//mxCell[@edge='1']"):
        ET.SubElement(cell, "mxGeometry")
    return root


@pytest.mark.parametrize(
    "fault", [None, "image", "reference", "network", "extra-network", "stopped", "missing"]
)
def test_engine_observation_refuses_wrong_boundary(monkeypatch, fault):
    value = {
        "Image": IMAGE,
        "Config": {"Image": IMAGE},
        "State": {"Running": True},
        "HostConfig": {"NetworkMode": "none"},
        "NetworkSettings": {"Networks": {"none": {}}},
    }
    if fault == "image":
        value["Image"] = "other"
    if fault == "reference":
        value["Config"]["Image"] = "other"
    if fault == "network":
        value["HostConfig"]["NetworkMode"] = "bridge"
    if fault == "extra-network":
        value["NetworkSettings"]["Networks"]["bridge"] = {}
    if fault == "stopped":
        value["State"]["Running"] = False
    monkeypatch.setattr(drawio, "owned", lambda _: [] if fault == "missing" else ["container"])
    monkeypatch.setattr(drawio, "docker", lambda *args: json.dumps([value]))
    if fault:
        with pytest.raises(ValueError):
            drawio.observation("scope", IMAGE)
    else:
        assert drawio.observation("scope", IMAGE)["network"] == "none"


@pytest.mark.parametrize(
    "fault", ["label", "edge", "missing", "geometry", "layout", "vertical-layout", "nonfinite"]
)
def test_returned_document_must_preserve_graph_and_layout(fault):
    root = document()
    vertex = root.find(".//mxCell[@vertex='1']")
    if fault == "label":
        vertex.set("value", "wrong")
    if fault == "edge":
        root.find(".//mxCell[@edge='1']").set("source", "wrong")
    if fault == "missing":
        vertex.set("vertex", "0")
    if fault == "geometry":
        vertex.find("mxGeometry").set("width", "0")
    if fault == "layout":
        vertex.find("mxGeometry").set("x", "0")
    if fault == "vertical-layout":
        vertex.find("mxGeometry").set("y", "0")
    if fault == "nonfinite":
        vertex.find("mxGeometry").set("width", "inf")
    with pytest.raises(ValueError):
        drawio.geometry(ET.tostring(root), True, True)


@pytest.mark.parametrize(
    "fault",
    [
        None,
        "invalid-accepted",
        "missing-output",
        "leftover",
        "unclean",
        "purge",
        "notice",
        "no-observation",
    ],
)
def test_release_probe_requires_each_case_and_cleanup(monkeypatch, fault):
    live = []
    purges = []
    destination = None
    observer = None
    notices = {
        "/usr/share/licenses/drawio/DEJAVU-LICENSE": hashlib.sha256(
            (drawio.ROOT / "images/drawio-sandbox/DEJAVU-LICENSE").read_bytes()
        ).hexdigest()
    }
    if fault == "notice":
        notices[next(iter(notices))] = "wrong"
    sandbox = SimpleNamespace(
        exec=AsyncMock(
            return_value=SimpleNamespace(
                exit_code=0,
                stdout=json.dumps({"python": "test", "graphviz": "test", "notices": notices}),
            )
        )
    )

    class Router:
        def __init__(self, *args, **kwargs):
            nonlocal observer
            observer = kwargs["observer"]

        async def acquire(self, *args, **kwargs):
            live.append("container")
            return sandbox

        async def dispose_scope(self, *args):
            purges.append(list(live))
            live.clear()
            return SimpleNamespace(undisposed=("container",) if fault == "purge" else ())

    def sink(path, **kwargs):
        nonlocal destination
        destination = path
        return None

    def tools(router, *args, preserve_layout, **kwargs):
        async def convert(xml):
            if fault != "no-observation":
                await router.acquire(None, None)
            invalid = xml == "<mxfile>"
            verdict = "created" if not invalid or fault == "invalid-accepted" else "refused"
            if not invalid and fault != "missing-output":
                positioned = (
                    ET.fromstring(xml).find(".//mxCell[@vertex='1']/mxGeometry") is not None
                )
                assert destination is not None
                destination.mkdir()
                (destination / "diagram.drawio").write_bytes(
                    ET.tostring(document(preserve_layout, positioned))
                )
            observer.tool_call_ended(SimpleNamespace(unclean=int(fault == "unclean")))
            if fault != "leftover":
                live.clear()
            return [Content.from_text(COMPLETED_TEXT), Content.from_text(f"Result: {verdict}")]

        return [SimpleNamespace(func=convert)]

    monkeypatch.setattr(
        drawio, "docker", lambda *args: '{"Version":"test","Os":"linux","Arch":"amd64"}'
    )
    monkeypatch.setattr(drawio, "owned", lambda _: list(live))
    monkeypatch.setattr(drawio, "observation", lambda *args: {"imageId": IMAGE, "network": "none"})
    monkeypatch.setattr(drawio, "SandboxRouter", Router)
    monkeypatch.setattr(drawio.DockerSandboxBackend, "create", AsyncMock())
    monkeypatch.setattr(drawio, "make_file_system_sink", sink)
    monkeypatch.setattr(drawio, "make_drawio_tools", tools)
    if fault:
        with pytest.raises(ValueError):
            asyncio.run(drawio.check(IMAGE))
    else:
        result = asyncio.run(drawio.check(IMAGE))
        assert len(result["checks"]) == len(result["observations"]) == 5
        assert result["checks"][-1]["verdict"] == "refused"
        assert result["sdkSource"] == "checkout"
        assert result["containersRemaining"] == []
    assert len(purges) == 1
    if fault == "leftover":
        assert purges == [["container"]]


def test_runtime_hook_and_approval_present_actual_checks(monkeypatch, tmp_path):
    from check_container_release_image import check

    result = {
        "checks": [{"name": "invalid-xml", "passed": True}],
        "backend": "maf-sandbox-docker",
        "containersRemaining": [],
    }
    probe = AsyncMock(return_value=result)
    monkeypatch.setattr(drawio, "check", probe)
    runtime = check("drawio", IMAGE, tmp_path)
    probe.assert_awaited_once_with(IMAGE)
    assert runtime["kind"] == "offline-sdk-consumer"
    candidate = {"profile": "drawio", "imageId": IMAGE}
    rows = evidence_rows({"runtime.json": runtime}, candidate)
    assert ("Draw.io check", result["checks"][0]) in rows
    assert ("Draw.io check", UNAVAILABLE) in evidence_rows({}, candidate)
