"""Verify native offline exports and refusal cases through the real Docker backend."""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import struct
import xml.etree.ElementTree as ET
import zlib
from pathlib import Path
from uuid import uuid4

from maf_sandbox import Isolation, ListedFile, SandboxRouter, make_file_system_sink
from maf_sandbox.maf import COMPLETED_TEXT, list_no_files, make_caller_context
from maf_sandbox_docker import DockerSandboxBackend, DockerSandboxConfig
from maf_sandbox_drawio import DrawioExport, make_drawio_export_tools, make_drawio_tools
from PIL import Image


def diagram() -> str:
    """Two distinguishable pages exercise geometry, Unicode, HTML and bundled Azure assets."""
    document = ET.Element("mxfile")
    for page in range(1, 3):
        model = ET.SubElement(ET.SubElement(document, "diagram", id=str(page)), "mxGraphModel")
        root = ET.SubElement(model, "root")
        ET.SubElement(root, "mxCell", id="0")
        ET.SubElement(root, "mxCell", {"id": "1", "parent": "0"})
        vertex = ET.SubElement(
            root,
            "mxCell",
            {
                "id": "a",
                "parent": "1",
                "vertex": "1",
                "value": f"<b>Page {page}</b><br>Résumé Ω<br>fontFamily=Missing;",
                "style": "shape=label;rounded=1;html=1;indicatorWidth=30;indicatorHeight=30;"
                + (
                    "indicatorImage=img/lib/azure2/ai_machine_learning/Azure_OpenAI.svg;"
                    if page == 1
                    else "indicatorShape=ellipse;indicatorColor=#00ff00;"
                )
                + "fillColor="
                + ("#ffcccc" if page == 1 else "#ccccff")
                + ";",
            },
        )
        ET.SubElement(
            vertex,
            "mxGeometry",
            {"as": "geometry", "x": "0", "y": "0", "width": str(200 * page), "height": "100"},
        )
        icon = ET.SubElement(
            root,
            "mxCell",
            {
                "id": "b",
                "parent": "1",
                "vertex": "1",
                "value": "Azure",
                "style": "shape=image;image=img/lib/azure2/ai_machine_learning/Azure_OpenAI.svg;",
            },
        )
        ET.SubElement(
            icon,
            "mxGeometry",
            {"as": "geometry", "x": "40", "y": "160", "width": "60", "height": "60"},
        )
        edge = ET.SubElement(
            root,
            "mxCell",
            {
                "id": "e",
                "parent": "1",
                "edge": "1",
                "source": "a",
                "target": "b",
                "style": "endArrow=block;",
            },
        )
        ET.SubElement(edge, "mxGeometry", {"as": "geometry", "relative": "1"})
    return ET.tostring(document, encoding="unicode")


def repeated_assets() -> str:
    """A small source expands past the prepared-XML limit using an otherwise accepted icon."""
    document = ET.Element("mxfile")
    for page in range(8):
        model = ET.SubElement(ET.SubElement(document, "diagram", id=str(page)), "mxGraphModel")
        root = ET.SubElement(model, "root")
        ET.SubElement(root, "mxCell", id="0")
        ET.SubElement(root, "mxCell", {"id": "1", "parent": "0"})
        for cell in range(32):
            vertex = ET.SubElement(
                root,
                "mxCell",
                {
                    "id": f"n{cell}",
                    "parent": "1",
                    "vertex": "1",
                    "style": "shape=image;image=img/lib/ibm/miscellaneous/cognitive_services.svg;",
                },
            )
            ET.SubElement(
                vertex,
                "mxGeometry",
                {
                    "as": "geometry",
                    "x": str(cell % 8 * 60),
                    "y": str(cell // 8 * 60),
                    "width": "40",
                    "height": "40",
                },
            )
    return ET.tostring(document, encoding="unicode")


def verify_output(output: Path) -> dict[str, object]:
    """Decode raster artifacts and check distinct pages and embedded SVG assets/fonts."""
    sizes = []
    for page in (1, 2):
        for format in ("png", "jpg"):
            with Image.open(output / f"diagram-{page}.{format}") as image:
                image.load()
                assert image.format == {"png": "PNG", "jpg": "JPEG"}[format]
                assert image.width > 100 and image.height > 100
                assert image.convert("RGB").getextrema() != ((255, 255),) * 3
                sizes.append(image.size)
        svg = (output / f"diagram-{page}.svg").read_text("utf-8")
        root = ET.fromstring(svg)
        assert root.tag == "{http://www.w3.org/2000/svg}svg"
        assert f"Page {page}" in svg and "Résumé" in svg
        assert "fontFamily=Missing;" in svg
        assert "data:image/" in svg and "data:font/ttf;base64," in svg
        if page == 1:
            assert any(
                item.get("width") == "30" and item.get("height") == "30"
                for item in root.iter("{http://www.w3.org/2000/svg}use")
            ), "Indicator image was omitted"
        else:
            assert "#00ff00" in svg.lower(), "Indicator shape was omitted"
        assert "file://" not in svg and "https://" not in svg
    assert sizes[0] != sizes[2], "Page selection returned the same geometry"
    assert (output / "diagram.drawio").is_file()
    return {"decoded_raster_sizes": sizes, "pages": 2, "formats": ["png", "jpg", "svg"]}


async def check(image: str, output: Path) -> None:
    """Exercise the closed-egress kind and verify no artifact lands on resource refusal."""
    scope = "drawio-exports-" + uuid4().hex
    backend = await DockerSandboxBackend.create(DockerSandboxConfig(memory="1g", cpus=2))
    router = SandboxRouter([backend], min_isolation=Isolation.CONTAINER)
    context = make_caller_context(list_no_files, lambda: scope, lambda: "exports")
    report: dict[str, object] = {}
    try:
        [tool] = make_drawio_tools(
            router,
            "export-check",
            context,
            make_file_system_sink(output, existing="replace"),
            image=image,
            export=DrawioExport(formats=("png", "jpg", "svg")),
            exec_timeout_seconds=120,
        )
        reply = await tool.func(xml=diagram())
        texts = [item.text for item in reply]
        if texts[:2] != [COMPLETED_TEXT, "Result: created"]:
            raise RuntimeError(str(texts))
        report.update(verify_output(output))
        source = (output / "diagram.drawio").read_text("utf-8")

        class Store:
            async def read(self, name: str) -> str:
                assert name == "saved.drawio"
                return source

        async def listing(store: object) -> list[ListedFile]:
            return [ListedFile("saved.drawio", None)]

        [stored] = make_drawio_export_tools(
            router,
            "export-check",
            make_caller_context(listing, lambda: scope, lambda: "exports"),
            make_file_system_sink(output / "stored", existing="replace"),
            Store(),
            image=image,
            export=DrawioExport(
                formats=("png", "jpg"), pages=(2,), scale=2, transparent=True, jpeg_quality=75
            ),
            exec_timeout_seconds=90,
        )
        answer = [item.text for item in await stored.func(file="saved.drawio")]
        assert answer[:2] == [COMPLETED_TEXT, "Result: created"], answer
        with Image.open(output / "stored/diagram-2.png") as picture:
            assert picture.width > 800 and picture.mode == "RGBA"
            assert picture.getchannel("A").getextrema()[0] == 0
        with Image.open(output / "stored/diagram-2.jpg") as picture:
            assert picture.width > 800 and picture.mode == "RGB"
        assert source == (output / "diagram.drawio").read_text("utf-8")
        report["stored_export"] = {
            "pages": [2],
            "scale": 2,
            "transparent_png": True,
            "opaque_jpg": True,
        }
        header = b"IHDR" + struct.pack(">IIBBBBB", 6000, 6000, 8, 2, 0, 0, 0)
        oversized = (
            b"\x89PNG\r\n\x1a\n"
            + struct.pack(">I", 13)
            + header
            + struct.pack(">I", zlib.crc32(header))
        )
        oversized += b"\x00\x00\x00\x00IDAT\x35\xaf\x06\x1e"
        cases = {
            "repeated-bundled-asset": repeated_assets(),
            "remote-image": diagram().replace(
                "img/lib/azure2/ai_machine_learning/Azure_OpenAI.svg",
                "https://example.invalid/icon.svg",
            ),
            "missing-asset": diagram().replace("Azure_OpenAI.svg", "Missing.svg"),
            "unknown-shape": diagram().replace("rounded=1", "shape=mxgraph.missing.fake"),
            "unknown-indicator-shape": diagram().replace(
                "indicatorShape=ellipse", "indicatorShape=missing"
            ),
            "absolute-indicator-image": diagram().replace(
                "indicatorImage=img/lib/azure2/ai_machine_learning/Azure_OpenAI.svg",
                "indicatorImage=/etc/passwd",
            ),
            "relative-indicator-image": diagram().replace(
                "indicatorImage=img/lib/azure2/ai_machine_learning/Azure_OpenAI.svg",
                "indicatorImage=../../secret.png",
            ),
            "oversized-embedded-image": diagram().replace(
                "indicatorImage=img/lib/azure2/ai_machine_learning/Azure_OpenAI.svg",
                "indicatorImage=data:image/png," + base64.b64encode(oversized).decode(),
            ),
            "huge-canvas": diagram().replace('width="200"', 'width="100000"'),
        }
        for encoding in ("utf-16", "utf-32"):
            resource = (
                '<!DOCTYPE svg [<!ENTITY text "expanded">]>'
                '<svg xmlns="http://www.w3.org/2000/svg"><text>&text;</text></svg>'
            ).encode(encoding)
            cases[encoding + "-embedded-xml"] = diagram().replace(
                "indicatorImage=img/lib/azure2/ai_machine_learning/Azure_OpenAI.svg",
                "indicatorImage=data:image/svg+xml," + base64.b64encode(resource).decode(),
            )
        for name, xml in cases.items():
            destination = output / name
            [refusal] = make_drawio_tools(
                router,
                "export-check",
                context,
                make_file_system_sink(destination, existing="replace"),
                image=image,
                export=DrawioExport(),
                exec_timeout_seconds=60,
            )
            answer = [item.text for item in await refusal.func(xml=xml)]
            assert answer[:2] == [COMPLETED_TEXT, "Result: refused"], (name, answer)
            if name == "repeated-bundled-asset":
                assert "Prepared document" in " ".join(text or "" for text in answer), answer
            assert not destination.exists() or not list(destination.iterdir())
        report["refusals"] = list(cases)
        [bounded] = make_drawio_tools(
            router,
            "export-check",
            context,
            make_file_system_sink(output / "timeout", existing="replace"),
            image=image,
            export=DrawioExport(),
            exec_timeout_seconds=0.05,
        )
        answer = [item.text for item in await bounded.func(xml=diagram())]
        assert answer[0] != COMPLETED_TEXT and not any(
            (text or "").startswith("Result:") for text in answer
        )
        assert not (output / "timeout").exists()
        report["timeout"] = "incomplete without artifact delivery"
    finally:
        purge = await router.dispose_scope(scope, "exports")
        assert not purge.undisposed, purge
    print(json.dumps(report, indent=2))


def main() -> None:
    """Read the host's candidate image and artifact destination."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(check(args.image, args.output))


if __name__ == "__main__":
    main()
