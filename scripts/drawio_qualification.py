"""Qualify editable Draw.io through the checkout SDK against an immutable image."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import subprocess
import xml.etree.ElementTree as ET
from importlib.metadata import version
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from uuid import uuid4

from check_drawio_docker import _input
from container_release import ROOT, require_digest
from maf_sandbox import (
    Isolation,
    Sandbox,
    SandboxKey,
    SandboxObserver,
    SandboxRouter,
    SandboxSpec,
    ToolCallEnded,
    make_file_system_sink,
)
from maf_sandbox.maf import COMPLETED_TEXT, list_no_files, make_caller_context
from maf_sandbox_docker import DockerSandboxBackend, DockerSandboxConfig
from maf_sandbox_drawio import make_drawio_tools


def require(condition: bool, message: str) -> None:
    """Keep qualification gates enabled under optimized Python."""
    if not condition:
        raise ValueError(message)


def docker(*args: str) -> str:
    """Read bounded engine evidence from the same environment used by the SDK."""
    return subprocess.run(
        ["docker", *args], check=True, capture_output=True, text=True, encoding="utf-8", timeout=30
    ).stdout


def owned(scope: str) -> list[str]:
    """Include stopped containers when verifying cleanup of this unique scope."""
    return docker("ps", "-aq", "--no-trunc", "--filter", f"label=maf-sandbox.scope={scope}").split()


def observation(scope: str, image_id: str) -> dict[str, Any]:
    """Refuse image substitution or networking on the container about to process input."""
    ids = owned(scope)
    require(len(ids) == 1, "Expected one Draw.io qualification container")
    value = json.loads(docker("inspect", ids[0]))[0]
    require(value["State"]["Running"] is True, "Draw.io container is not running")
    require(
        value["Image"] == value["Config"]["Image"] == image_id,
        "Draw.io container differs from the selected image",
    )
    require(value["HostConfig"]["NetworkMode"] == "none", "Draw.io networking is not closed")
    require(
        set(value["NetworkSettings"]["Networks"]) <= {"none"},
        "Draw.io container has an additional network",
    )
    return {"imageId": value["Image"], "network": "none", "containerId": ids[0]}


def geometry(raw: bytes, preserve: bool, positioned: bool) -> dict[str, Any]:
    """Validate the returned editable graph, labels, edges and requested layout policy."""
    document = ET.fromstring(raw)
    require(document.tag == "mxfile", "Draw.io output is not an editable document")
    vertices = document.findall(".//mxCell[@vertex='1']")
    edges = document.findall(".//mxCell[@edge='1']")
    expected = ET.fromstring(_input(positioned))
    for actual, originals in (
        (vertices, expected.findall(".//mxCell[@vertex='1']")),
        (edges, expected.findall(".//mxCell[@edge='1']")),
    ):
        require(len(actual) == len(originals) == 3, "Draw.io lost graph elements")
        for cell, original in zip(actual, originals, strict=True):
            require(
                all(
                    cell.get(key) == original.get(key)
                    for key in ("id", "value", "source", "target", "vertex", "edge")
                ),
                "Draw.io changed graph identity or labels",
            )
    for vertex in vertices:
        shape = vertex.find("mxGeometry")
        if shape is None:
            raise ValueError("Draw.io vertex lacks geometry")
        coordinates = {key: float(shape.attrib[key]) for key in ("x", "y", "width", "height")}
        require(all(math.isfinite(value) for value in coordinates.values()), "Nonfinite geometry")
        if preserve and positioned:
            require(
                coordinates == {"x": 900, "y": 900, "width": 180, "height": 90},
                "Draw.io changed supplied geometry",
            )
        require(
            (shape.get("x") == "900") is (preserve and positioned)
            and float(shape.attrib["width"]) > 0
            and float(shape.attrib["height"]) > 0,
            "Draw.io layout policy failed",
        )
    require(all(edge.find("mxGeometry") is not None for edge in edges), "Edges lack geometry")
    return {"sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw), "vertices": 3, "edges": 3}


async def check(image_id: str) -> dict[str, Any]:
    """Exercise four layout combinations and invalid XML, retaining engine and cleanup evidence."""
    require_digest(image_id)
    server = json.loads(docker("version", "--format", "{{json .Server}}"))
    require(server["Os"] == "linux" and server["Arch"] == "amd64", "Requires Linux/amd64 Docker")
    scope = f"drawio-release-{uuid4().hex}"
    calls: list[ToolCallEnded] = []
    observations: list[dict[str, Any]] = []
    runtime: dict[str, Any] = {}

    class Observer(SandboxObserver):
        def tool_call_ended(self, event: ToolCallEnded) -> None:
            calls.append(event)

    class Router(SandboxRouter):
        async def acquire(self, key: SandboxKey, spec: SandboxSpec, **kwargs: Any) -> Sandbox:
            sandbox = await super().acquire(key, spec, **kwargs)
            observations.append(await asyncio.to_thread(observation, scope, image_id))
            if not runtime:
                probe = await sandbox.exec(
                    [
                        "python3",
                        "-c",
                        (
                            "import hashlib,json,platform,subprocess; from pathlib import Path; "
                            "print(json.dumps({'python':platform.python_version(), "
                            "'graphviz':subprocess.run(['dot','-V'],check=True,capture_output=True,text=True).stderr.strip(), "
                            "'notices':{p:hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in "
                            "['/usr/lib/python3.13/LICENSE.txt','/usr/share/doc/graphviz/COPYING',"
                            "'/usr/share/licenses/drawio/DEJAVU-LICENSE']}}))"
                        ),
                    ],
                    timeout=30,
                    working_directory="/tmp",
                )
                require(probe.exit_code == 0, "Draw.io runtime identity or notices unavailable")
                runtime.update(json.loads(probe.stdout))
            return sandbox

    backend = await DockerSandboxBackend.create(DockerSandboxConfig(memory="256m", cpus=1))
    router = Router([backend], min_isolation=Isolation.CONTAINER, observer=Observer())
    context = make_caller_context(list_no_files, lambda: scope, lambda: "diagram")
    reports = []
    try:
        with TemporaryDirectory(prefix="drawio-release-") as temporary:
            for preserve, positioned in (
                (True, True),
                (True, False),
                (False, True),
                (False, False),
                (None, None),
            ):
                invalid = preserve is None
                name = (
                    "invalid-xml"
                    if invalid
                    else f"preserve-{str(preserve).lower()}_geometry-{str(positioned).lower()}"
                )
                destination = Path(temporary) / name
                [tool] = make_drawio_tools(
                    router,
                    "drawio-release",
                    context,
                    make_file_system_sink(destination, existing="refuse"),
                    image=image_id,
                    preserve_layout=bool(preserve),
                )
                before = len(observations)
                reply = await tool.func(xml="<mxfile>" if invalid else _input(bool(positioned)))
                texts = [item.text or "" for item in reply]
                verdict = "refused" if invalid else "created"
                require(
                    texts[:2] == [COMPLETED_TEXT, f"Result: {verdict}"],
                    f"Draw.io {name} did not {verdict}",
                )
                require(
                    len(calls) == len(reports) + 1 and calls[-1].unclean == 0,
                    "Draw.io call cleanup failed",
                )
                require(len(observations) == before + 1, "Draw.io call lacks engine observation")
                require(
                    not await asyncio.to_thread(owned, scope), "Draw.io call left containers behind"
                )
                artifacts = (
                    sorted(p.name for p in destination.iterdir()) if destination.exists() else []
                )
                require(
                    artifacts == ([] if invalid else ["diagram.drawio"]),
                    "Unexpected Draw.io outputs",
                )
                reports.append(
                    {
                        "name": name,
                        "passed": True,
                        "verdict": verdict,
                        "unclean": calls[-1].unclean,
                        **(
                            {}
                            if invalid
                            else geometry(
                                (destination / "diagram.drawio").read_bytes(),
                                bool(preserve),
                                bool(positioned),
                            )
                        ),
                    }
                )
    finally:
        purge = await router.dispose_scope(scope, "diagram")
        require(not purge.undisposed, "Draw.io scope cleanup failed")
        require(
            not await asyncio.to_thread(owned, scope),
            "Draw.io qualification left containers behind",
        )
    expected_notice = hashlib.sha256(
        (ROOT / "images/drawio-sandbox/DEJAVU-LICENSE").read_bytes()
    ).hexdigest()
    require(
        runtime["notices"]["/usr/share/licenses/drawio/DEJAVU-LICENSE"] == expected_notice,
        "Draw.io font notice differs from source",
    )
    return {
        "backend": "maf-sandbox-docker",
        "sdkSource": "checkout",
        "packages": {
            name: version(name)
            for name in (
                "maf-sandbox",
                "maf-sandbox-docker",
                "maf-sandbox-drawio",
                "agent-framework-core",
            )
        },
        "docker": {key: server[key] for key in ("Version", "Os", "Arch")},
        "versions": runtime,
        "observations": observations,
        "checks": reports,
        "containersRemaining": [],
    }
