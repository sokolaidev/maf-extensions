"""Fixed, standard-library guest program for validating and laying out draw.io XML."""

from __future__ import annotations

import argparse
import math
import subprocess
import sys
import tempfile
import threading
import time
import xml.etree.ElementTree as ET
from collections import defaultdict, deque
from collections.abc import Iterator
from pathlib import Path
from typing import BinaryIO, cast

MAX_INPUT_BYTES = 1024 * 1024
MAX_OUTPUT_BYTES = 2 * 1024 * 1024
MAX_DIAGNOSTIC = 2048
_MAX_CELLS = 1000
_MAX_PAGES = 8
_PIXELS_PER_INCH = 96
_MARGIN = 40


class DiagramError(ValueError):
    """An input or layout failure safe to report as untrusted tool diagnostics."""


def _number(value: str, field: str) -> float:
    try:
        result = float(value)
    except ValueError as exc:
        raise DiagramError(f"{field} must be a finite number") from exc
    if not math.isfinite(result) or abs(result) > 1_000_000:
        raise DiagramError(f"{field} must be finite and within +/-1000000")
    return result


def _parse(xml: str) -> ET.Element:
    if len(xml.encode("utf-8")) > MAX_INPUT_BYTES:
        raise DiagramError("XML exceeds the 1 MiB input limit")
    if "<!DOCTYPE" in xml.upper() or "<!ENTITY" in xml.upper():
        raise DiagramError("DTD and entity declarations are not supported")
    parser: ET.XMLPullParser[ET.Element] = ET.XMLPullParser(events=("start", "end"))
    depth = count = 0
    root: ET.Element | None = None
    try:
        for offset in range(0, len(xml), 4096):
            parser.feed(xml[offset : offset + 4096])
            for event, element in cast(Iterator[tuple[str, ET.Element]], parser.read_events()):
                if event == "start":
                    depth += 1
                    count += 1
                    if root is None:
                        root = element
                    if depth > 64 or count > 20_000:
                        raise DiagramError("XML exceeds the depth or element limit")
                else:
                    depth -= 1
        parser.close()
    except ET.ParseError as exc:
        raise DiagramError(f"Invalid XML: {exc}") from exc
    if root is None:
        raise DiagramError("XML is empty")
    if any((element.text or "").strip() or (element.tail or "").strip() for element in root.iter()):
        raise DiagramError("Supply uncompressed draw.io XML with labels in attributes")
    return root


def _pages(document: ET.Element) -> list[ET.Element]:
    pages = list(document)
    if document.tag != "mxfile" or not 1 <= len(pages) <= _MAX_PAGES:
        raise DiagramError("Supply an mxfile with 1 to 8 uncompressed diagram pages")
    models: list[ET.Element] = []
    page_ids: set[str] = set()
    for page in pages:
        page_id = page.get("id")
        if page_id is not None:
            if page_id in page_ids:
                raise DiagramError(f"Duplicate page ID {page_id!r}")
            page_ids.add(page_id)
        if page.tag != "diagram" or len(page) != 1 or page[0].tag != "mxGraphModel":
            raise DiagramError("Each diagram must contain one uncompressed mxGraphModel")
        models.append(page[0])
    return models


def _cells(model: ET.Element) -> dict[str, ET.Element]:
    if len(model) != 1 or model[0].tag != "root":
        raise DiagramError("mxGraphModel must contain one root")
    cells: dict[str, ET.Element] = {}
    for item in model[0]:
        if item.tag == "mxCell":
            cell, identifier = item, item.get("id")
        elif item.tag in {"object", "UserObject"} and len(item) == 1:
            cell, identifier = item[0], item.get("id")
            if cell.tag != "mxCell" or cell.get("id") not in {None, identifier}:
                raise DiagramError("An object must wrap one mxCell with the same ID or no ID")
        else:
            raise DiagramError("root accepts mxCell and object/UserObject wrappers only")
        if not identifier or len(identifier) > 128:
            raise DiagramError("Every cell needs an ID of 1 to 128 characters")
        if identifier in cells:
            raise DiagramError(f"Duplicate cell ID {identifier!r}")
        cells[identifier] = cell
    if len(cells) > _MAX_CELLS:
        raise DiagramError("A page may contain at most 1000 cells")
    if "0" not in cells or "1" not in cells or cells["1"].get("parent") != "0":
        raise DiagramError("Structural cells 0 and 1 are required; cell 1 must have parent 0")
    if cells["0"].get("parent") is not None:
        raise DiagramError("Root cell 0 must not have a parent")
    for identifier, cell in cells.items():
        vertex, edge = cell.get("vertex", "0"), cell.get("edge", "0")
        if vertex not in {"0", "1"} or edge not in {"0", "1"} or vertex == edge == "1":
            raise DiagramError(f"Cell {identifier!r} has invalid vertex/edge flags")
        parent = cell.get("parent")
        if identifier == "0" or parent == "0":
            if vertex == "1" or edge == "1":
                raise DiagramError("Root and layer cells cannot be vertices or edges")
        elif parent not in cells or (vertex != "1" and edge != "1"):
            raise DiagramError(f"Cell {identifier!r} needs a valid parent and vertex/edge flag")
        seen = {identifier}
        while parent is not None:
            if parent in seen or parent not in cells:
                raise DiagramError(f"Cell {identifier!r} has a missing or cyclic parent")
            seen.add(parent)
            parent = cells[parent].get("parent")
        for endpoint in ("source", "target"):
            target = cell.get(endpoint)
            if target is not None and (
                edge != "1" or target not in cells or cells[target].get("vertex") != "1"
            ):
                raise DiagramError(f"Cell {identifier!r}.{endpoint} must reference a vertex")
        _validate_geometry(identifier, cell, cells)
    return cells


def _validate_geometry(identifier: str, cell: ET.Element, cells: dict[str, ET.Element]) -> None:
    geometries = cell.findall("mxGeometry")
    if len(cell) != len(geometries) or len(geometries) > 1:
        raise DiagramError(f"Cell {identifier!r} accepts at most one mxGeometry child")
    if not geometries:
        return
    geometry = geometries[0]
    if geometry.get("as") != "geometry" or geometry.get("relative", "0") not in {"0", "1"}:
        raise DiagramError(f"Cell {identifier!r} needs as='geometry' and relative=0 or 1")
    child_roles: set[str] = set()
    for child in geometry:
        role = child.get("as", "")
        valid = (
            child.tag == "mxPoint"
            and role in {"sourcePoint", "targetPoint", "offset"}
            and not len(child)
            or child.tag == "mxRectangle"
            and role == "alternateBounds"
            and not len(child)
            or child.tag == "Array"
            and role == "points"
            and all(point.tag == "mxPoint" and not len(point) for point in child)
        )
        if not valid or role in child_roles:
            raise DiagramError(f"Cell {identifier!r} has an invalid or duplicate geometry child")
        child_roles.add(role)
    for element in geometry.iter():
        if element.tag not in {"mxGeometry", "mxPoint", "mxRectangle", "Array"}:
            raise DiagramError(f"Unsupported geometry element in cell {identifier!r}")
        for field in ("x", "y", "width", "height"):
            if field in element.attrib:
                number = _number(element.attrib[field], f"Cell {identifier!r}.{field}")
                if field in {"width", "height"} and number < 0:
                    raise DiagramError(f"Cell {identifier!r}.{field} cannot be negative")
    parent = cells.get(cell.get("parent", ""))
    edge_label = parent is not None and parent.get("edge") == "1"
    if cell.get("vertex") == "1" and not edge_label:
        for field in ("width", "height"):
            if field in geometry.attrib and float(geometry.attrib[field]) <= 0:
                raise DiagramError(f"Vertex {identifier!r}.{field} must be positive")


def _has_layout(cells: dict[str, ET.Element]) -> bool:
    complete = True
    for identifier, cell in cells.items():
        geometry = cell.find("mxGeometry")
        parent = cells.get(cell.get("parent", ""))
        if cell.get("vertex") == "1":
            if parent is not None and parent.get("edge") == "1":
                complete &= geometry is not None and geometry.get("relative") == "1"
            else:
                complete &= geometry is not None and all(
                    dimension in geometry.attrib for dimension in ("width", "height")
                )
        if cell.get("edge") == "1":
            for endpoint in ("source", "target"):
                if cell.get(endpoint) is None and (
                    geometry is None or geometry.find(f"mxPoint[@as='{endpoint}Point']") is None
                ):
                    raise DiagramError(f"Edge {identifier!r} needs {endpoint} or {endpoint}Point")
    return complete


def _dot(source: str, deadline: float) -> str:
    if deadline <= time.monotonic():
        raise DiagramError("Automatic layout timed out")
    # Only generated identifiers and validated dimensions enter DOT, never labels or styles.
    # A seekable input avoids a blocked pipe write spending the supervision deadline.
    with tempfile.TemporaryFile() as dot_input:
        dot_input.write(source.encode("ascii"))
        dot_input.seek(0)
        return _capture_dot(dot_input.fileno(), deadline)


def _capture_dot(input_descriptor: int, deadline: float) -> str:
    with subprocess.Popen(
        ["dot", "-Tplain"], stdin=input_descriptor, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    ) as process:
        assert process.stdout is not None and process.stderr is not None
        buffers = [bytearray(), bytearray()]
        overflow = threading.Event()

        def drain(stream: BinaryIO, buffer: bytearray, limit: int) -> None:
            while chunk := stream.read(65536):
                available = limit - len(buffer)
                buffer.extend(chunk[:available])
                if len(chunk) > available:
                    overflow.set()
                    process.kill()

        threads = [
            threading.Thread(
                target=drain, args=(process.stdout, buffers[0], MAX_OUTPUT_BYTES), daemon=True
            ),
            threading.Thread(
                target=drain, args=(process.stderr, buffers[1], MAX_DIAGNOSTIC), daemon=True
            ),
        ]
        for thread in threads:
            thread.start()
        try:
            process.wait(timeout=max(0.001, deadline - time.monotonic()))
        except subprocess.TimeoutExpired as exc:
            process.kill()
            process.wait()
            raise DiagramError("Automatic layout timed out") from exc
        finally:
            for thread in threads:
                thread.join(timeout=1)
        if overflow.is_set() or any(thread.is_alive() for thread in threads):
            raise DiagramError("Graphviz exceeded its output limit")
        if process.returncode != 0:
            raise DiagramError("Graphviz could not lay out this graph")
        return buffers[0].decode("ascii")


def _style_without(style: str, keys: set[str]) -> str:
    return ";".join(part for part in style.split(";") if part.split("=", 1)[0] not in keys)


def _layout(cells: dict[str, ET.Element], direction: str, deadline: float) -> None:
    vertices = {key: cell for key, cell in cells.items() if cell.get("vertex") == "1"}
    edges = [cell for cell in cells.values() if cell.get("edge") == "1"]
    if len(vertices) > 200 or len(edges) > 600:
        raise DiagramError("Automatic layout supports at most 200 vertices and 600 edges per page")
    for cell in [*vertices.values(), *edges]:
        parent = cells[cell.attrib["parent"]]
        geometry = cell.find("mxGeometry")
        if parent.get("parent") != "0" or (
            cell.get("vertex") == "1" and geometry is not None and geometry.get("relative") == "1"
        ):
            raise DiagramError(
                "Automatic layout requires flat graphs; supply complete geometry for groups, "
                "ports and edge labels and use preserve_layout=True"
            )
        if cell.get("edge") == "1" and any(
            cell.get(key) not in vertices for key in ("source", "target")
        ):
            raise DiagramError("Automatic layout requires edges with source and target vertices")
        if cell.get("collapsed") == "1":
            raise DiagramError("Automatic layout does not support collapsed cells")
    if not vertices:
        return
    names = {key: f"n{index}" for index, key in enumerate(vertices)}
    by_name = {names[key]: cell for key, cell in vertices.items()}
    dimensions: dict[str, tuple[float, float]] = {}
    lines = [
        f"digraph G {{ graph [rankdir={direction}, splines=polyline, nodesep=0.5, ranksep=0.75];",
        'node [shape=box, fixedsize=true, label=""]; edge [arrowhead=none];',
    ]
    for key, cell in vertices.items():
        geometry = cell.find("mxGeometry")
        width = float(geometry.get("width", "160")) if geometry is not None else 160.0
        height = float(geometry.get("height", "80")) if geometry is not None else 80.0
        dimensions[names[key]] = (width, height)
        lines.append(
            f"{names[key]} [width={width / _PIXELS_PER_INCH:.6f}, "
            f"height={height / _PIXELS_PER_INCH:.6f}];"
        )
    by_pair: dict[tuple[str, str], deque[ET.Element]] = defaultdict(deque)
    for cell in edges:
        pair = names[cell.attrib["source"]], names[cell.attrib["target"]]
        by_pair[pair].append(cell)
        lines.append(f"{pair[0]} -> {pair[1]};")
    lines.append("}")
    output = _dot("\n".join(lines), deadline)
    records = [line.split() for line in output.splitlines()]
    if not records or records[0][0] != "graph" or records[-1] != ["stop"]:
        raise DiagramError("Graphviz returned an incomplete layout")
    graph_height = _number(records[0][3], "Layout height") * _PIXELS_PER_INCH

    def point(x: str, y: str) -> dict[str, str]:
        return {
            "x": f"{_number(x, 'Layout x') * _PIXELS_PER_INCH + _MARGIN:.3f}",
            "y": f"{graph_height - _number(y, 'Layout y') * _PIXELS_PER_INCH + _MARGIN:.3f}",
        }

    positioned: set[str] = set()
    for record in records[1:-1]:
        if record[0] == "node":
            name = record[1]
            if name not in by_name or name in positioned:
                raise DiagramError("Graphviz returned unexpected vertices")
            positioned.add(name)
            cell = by_name[name]
            center = point(record[2], record[3])
            width, height = dimensions[name]
            geometry = cell.find("mxGeometry")
            if geometry is None:
                geometry = ET.SubElement(cell, "mxGeometry")
            geometry.clear()
            geometry.attrib.update(
                {
                    "as": "geometry",
                    "x": f"{float(center['x']) - width / 2:.3f}",
                    "y": f"{float(center['y']) - height / 2:.3f}",
                    "width": f"{width:g}",
                    "height": f"{height:g}",
                }
            )
        elif record[0] == "edge":
            pair = (record[1], record[2])
            if not by_pair[pair]:
                raise DiagramError("Graphviz returned unexpected edges")
            cell = by_pair[pair].popleft()
            geometry = cell.find("mxGeometry")
            if geometry is None:
                geometry = ET.SubElement(cell, "mxGeometry")
            geometry.clear()
            geometry.attrib.update({"as": "geometry", "relative": "1"})
            points = ET.SubElement(geometry, "Array", {"as": "points"})
            previous: dict[str, str] | None = None
            count = int(record[3])
            if count < 2 or len(record) != 6 + 2 * count:
                raise DiagramError("Graphviz returned invalid connector points")
            for index in range(count):
                current = point(record[4 + index * 2], record[5 + index * 2])
                if current != previous:
                    ET.SubElement(points, "mxPoint", current)
                previous = current
            routing = {
                "edgeStyle",
                "curved",
                "orthogonal",
                "entryX",
                "entryY",
                "entryDx",
                "entryDy",
                "exitX",
                "exitY",
                "exitDx",
                "exitDy",
                "entryPerimeter",
                "exitPerimeter",
            }
            cell.set(
                "style",
                _style_without(cell.get("style", ""), routing).rstrip(";")
                + ";edgeStyle=none;curved=0;",
            )
        else:
            raise DiagramError("Graphviz returned an unknown layout record")
    if positioned != set(by_name) or any(by_pair.values()):
        raise DiagramError("Graphviz did not position every vertex and edge")
    for cell in cells.values():
        if "style" in cell.attrib:
            cell.set("style", _style_without(cell.attrib["style"], {"childLayout"}))


def convert(
    xml: str, *, preserve_layout: bool = True, direction: str = "TB", timeout: float = 55
) -> str:
    """Validate XML and complete each page's geometry before serializing an editable mxfile."""
    if type(preserve_layout) is not bool or direction not in {"TB", "LR"}:
        raise ValueError("preserve_layout must be bool and direction must be TB or LR")
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be positive and finite")
    document = _parse(xml)
    if document.tag == "mxGraphModel":
        model = document
        document = ET.Element("mxfile")
        ET.SubElement(document, "diagram", {"id": "page-1", "name": "Diagram"}).append(model)
    deadline = time.monotonic() + timeout
    for index, model in enumerate(_pages(document), 1):
        try:
            cells = _cells(model)
            has_layout = _has_layout(cells)
            if not preserve_layout or not has_layout:
                _layout(cells, direction, deadline)
            for cell in cells.values():
                if cell.get("edge") == "1" and cell.find("mxGeometry") is None:
                    ET.SubElement(cell, "mxGeometry", {"as": "geometry", "relative": "1"})
            if not _has_layout(_cells(model)):
                raise DiagramError("Layout left incomplete vertex geometry")
        except DiagramError as exc:
            raise DiagramError(f"Page {index}: {exc}") from exc
    document.set("compressed", "false")
    result = ET.tostring(document, encoding="unicode") + "\n"
    if len(result.encode("utf-8")) > MAX_OUTPUT_BYTES:
        raise DiagramError("Diagram exceeds the 2 MiB output limit")
    return result


def main() -> int:
    """Read fixed call-local files; write output only after all pages validate."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--preserve-layout", choices=("true", "false"), required=True)
    parser.add_argument("--direction", choices=("TB", "LR"), required=True)
    parser.add_argument("--timeout", type=float, required=True)
    args = parser.parse_args()
    try:
        with Path("input.xml").open("rb") as source:
            data = source.read(MAX_INPUT_BYTES + 1)
        if len(data) > MAX_INPUT_BYTES:
            raise DiagramError("XML exceeds the 1 MiB input limit")
        result = convert(
            data.decode("utf-8"),
            preserve_layout=args.preserve_layout == "true",
            direction=args.direction,
            timeout=args.timeout,
        )
        Path("diagram.drawio").write_text(result, encoding="utf-8", newline="\n")
    except (DiagramError, UnicodeError) as exc:
        print(str(exc)[:MAX_DIAGNOSTIC], file=sys.stderr)
        return 2
    except FileNotFoundError:
        print("The draw.io sandbox needs Python 3 and Graphviz dot", file=sys.stderr)
        return 3
    except (OSError, ValueError, IndexError, KeyError):
        print("The draw.io converter could not complete the file", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
