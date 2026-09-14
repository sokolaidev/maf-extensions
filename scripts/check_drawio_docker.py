"""Exercise draw.io layout and file delivery through a real Docker sandbox, without a model."""

from __future__ import annotations

import argparse
import asyncio
import json
import xml.etree.ElementTree as ET
from pathlib import Path
from uuid import uuid4

from maf_sandbox import Isolation, SandboxRouter, make_file_system_sink
from maf_sandbox.maf import list_no_files, make_caller_context
from maf_sandbox_docker import DockerSandboxBackend, DockerSandboxConfig
from maf_sandbox_drawio import make_drawio_tools


def _input(positioned: bool) -> str:
    document = ET.Element("mxfile")
    graph = ET.SubElement(
        ET.SubElement(document, "diagram", id="pipeline", name="Pipeline"), "mxGraphModel"
    )
    root = ET.SubElement(graph, "root")
    ET.SubElement(root, "mxCell", id="0")
    ET.SubElement(root, "mxCell", {"id": "1", "parent": "0"})
    for identifier, label, style in (
        ("input", "Model XML", "rounded=1;fillColor=#dae8fc;strokeColor=#6c8ebf;"),
        ("validate", "Validate & lay out", "rhombus;fillColor=#fff2cc;strokeColor=#d6b656;"),
        ("file", "diagram.drawio", "shape=document;fillColor=#d5e8d4;strokeColor=#82b366;"),
    ):
        vertex = ET.SubElement(
            root,
            "mxCell",
            {
                "id": identifier,
                "value": label,
                "vertex": "1",
                "parent": "1",
                "style": style + "whiteSpace=wrap;html=1;fontSize=16;",
            },
        )
        if positioned:
            ET.SubElement(
                vertex,
                "mxGeometry",
                {"as": "geometry", "x": "900", "y": "900", "width": "180", "height": "90"},
            )
    for identifier, source, target, label in (
        ("check", "input", "validate", ""),
        ("save", "validate", "file", "valid"),
        ("retry", "validate", "input", "repair"),
    ):
        ET.SubElement(
            root,
            "mxCell",
            {
                "id": identifier,
                "edge": "1",
                "parent": "1",
                "source": source,
                "target": target,
                "value": label,
                "style": "endArrow=block;html=1;strokeColor=#475569;",
            },
        )
    return ET.tostring(document, encoding="unicode")


async def check(image: str, output: Path) -> None:
    """Assert actual landed geometry for all four layout-policy combinations."""
    scope = f"drawio-check-{uuid4().hex}"
    router = SandboxRouter(
        [DockerSandboxBackend(DockerSandboxConfig(memory="256m", cpus=1))],
        min_isolation=Isolation.CONTAINER,
    )
    context = make_caller_context(list_no_files, lambda: scope, lambda: "diagram")
    reports: list[dict[str, object]] = []
    try:
        for preserve, positioned in ((True, True), (True, False), (False, True), (False, False)):
            name = f"preserve-{str(preserve).lower()}_geometry-{str(positioned).lower()}"
            destination = output / name
            [tool] = make_drawio_tools(
                router,
                "drawio-check",
                context,
                make_file_system_sink(destination, existing="replace"),
                image=image,
                preserve_layout=preserve,
            )
            reply = await tool.func(xml=_input(positioned))
            if reply.startswith("Error:"):
                raise RuntimeError(reply)
            artifact = destination / "diagram.drawio"
            document = ET.fromstring(artifact.read_bytes())
            vertices = document.findall(".//mxCell[@vertex='1']")
            edges = document.findall(".//mxCell[@edge='1']")
            assert len(vertices) == len(edges) == 3
            for vertex in vertices:
                geometry = vertex.find("mxGeometry")
                assert geometry is not None
                assert (geometry.get("x") == "900") is (preserve and positioned)
                assert float(geometry.attrib["width"]) > 0 and float(geometry.attrib["height"]) > 0
            for edge in edges:
                assert edge.find("mxGeometry") is not None
            reports.append(
                {
                    "case": name,
                    "bytes": artifact.stat().st_size,
                    "vertices": len(vertices),
                    "edges": len(edges),
                    "result": reply,
                }
            )
    finally:
        purge = await router.dispose_scope(scope, "diagram")
        assert not purge.undisposed, purge
    print(json.dumps(reports, indent=2))


def main() -> None:
    """Read the image and output destination from the host's command line."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(check(args.image, args.output))


if __name__ == "__main__":
    main()
