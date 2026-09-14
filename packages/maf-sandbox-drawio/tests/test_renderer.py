"""Native XML preservation, automatic placement, and bounded converter failures."""

from __future__ import annotations

import shutil
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from maf_sandbox_drawio import _renderer as renderer
from maf_sandbox_drawio._renderer import DiagramError, convert


def model(*, positioned: bool = False) -> ET.Element:
    graph = ET.Element("mxGraphModel")
    root = ET.SubElement(graph, "root")
    ET.SubElement(root, "mxCell", id="0")
    ET.SubElement(root, "mxCell", {"id": "1", "parent": "0"})
    for identifier in ("a", "b"):
        cell = ET.SubElement(
            root, "mxCell", {"id": identifier, "parent": "1", "vertex": "1", "value": identifier}
        )
        if positioned:
            ET.SubElement(cell, "mxGeometry", {"as": "geometry", "width": "160", "height": "80"})
    ET.SubElement(
        root, "mxCell", {"id": "e", "parent": "1", "edge": "1", "source": "a", "target": "b"}
    )
    return graph


def xml(graph: ET.Element) -> str:
    return ET.tostring(graph, encoding="unicode")


def cell(graph: ET.Element, identifier: str) -> ET.Element:
    result = graph.find(f".//mxCell[@id='{identifier}']")
    assert result is not None
    return result


def geometry(graph: ET.Element, identifier: str) -> ET.Element:
    result = cell(graph, identifier).find("mxGeometry")
    assert result is not None
    return result


@pytest.fixture
def dot(monkeypatch: pytest.MonkeyPatch):
    calls: list[str] = []

    def layout(source: str, deadline: float) -> str:
        assert deadline > time.monotonic()
        calls.append(source)
        return (
            'graph 1 2 3\nnode n0 1 2.5 1.66667 0.83333 "" solid box black lightgrey\n'
            'node n1 1 0.5 1.66667 0.83333 "" solid box black lightgrey\n'
            "edge n0 n1 2 1 2 1 1 solid black\nstop\n"
        )

    monkeypatch.setattr(renderer, "_dot", layout)
    return calls


@pytest.mark.parametrize(
    "preserve,positioned,expected",
    [(True, True, 0), (True, False, 1), (False, True, 1), (False, False, 1)],
)
def test_layout_policy(preserve: bool, positioned: bool, expected: int, dot: list[str]):
    source = model(positioned=positioned)
    result = ET.fromstring(convert(xml(source), preserve_layout=preserve))
    assert len(dot) == expected
    assert geometry(result, "a").get("width") == "160"
    assert geometry(result, "e").get("relative") == "1"
    if expected:
        assert float(geometry(result, "a").attrib["y"]) < float(geometry(result, "b").attrib["y"])
        assert geometry(result, "e").find("Array/mxPoint") is not None
    else:
        assert geometry(result, "a").attrib == geometry(source, "a").attrib


def test_zero_defaults_overlap_and_waypoints_are_preserved(dot: list[str]):
    source = model(positioned=True)
    geometry(source, "a").set("x", "0.000")
    geometry(source, "a").set("y", "-10")
    edge_geometry = ET.SubElement(
        cell(source, "e"), "mxGeometry", {"as": "geometry", "relative": "1", "x": "0.25"}
    )
    points = ET.SubElement(edge_geometry, "Array", {"as": "points"})
    ET.SubElement(points, "mxPoint", x="350", y="42.5")
    result = ET.fromstring(convert(xml(source)))
    assert not dot
    for identifier in ("a", "b", "e"):
        assert xml(geometry(source, identifier)) == xml(geometry(result, identifier))


@pytest.mark.parametrize("missing", ["geometry", "width", "height"])
def test_partial_layout_is_automatic(missing: str, dot: list[str]):
    source = model(positioned=True)
    if missing == "geometry":
        cell(source, "b").remove(geometry(source, "b"))
    else:
        del geometry(source, "b").attrib[missing]
    convert(xml(source))
    assert len(dot) == 1


def test_mixed_pages_only_layout_the_missing_page(dot: list[str]):
    document = ET.Element("mxfile", host="example", custom="keep")
    for identifier, positioned in (("placed", True), ("missing", False)):
        ET.SubElement(document, "diagram", id=identifier, name=identifier).append(
            model(positioned=positioned)
        )
    result = ET.fromstring(convert(xml(document)))
    assert len(dot) == 1
    assert result.attrib == {"host": "example", "custom": "keep", "compressed": "false"}
    assert geometry(result[0], "a").get("x") is None
    assert geometry(result[1], "a").get("x") is not None


def test_unknown_metadata_and_labels_never_enter_dot(dot: list[str]):
    source = model()
    a = cell(source, "b")
    source[0].remove(a)
    wrapper = ET.SubElement(
        source[0], "object", id="b", label="<b>Résumé & 中文</b>", custom="keep"
    )
    del a.attrib["id"]
    wrapper.append(a)
    a.set(
        "style", "shape=document;fillColor=#dae8fc;image=file:///private;childLayout=stackLayout;"
    )
    cell(source, "e").set(
        "style", "endArrow=block;strokeColor=#123456;exitX=0;edgeStyle=orthogonalEdgeStyle;"
    )
    result = ET.fromstring(convert(xml(source)))
    saved = result.find(".//object")
    assert saved is not None and saved.attrib == wrapper.attrib
    assert "fillColor=#dae8fc" in saved[0].attrib["style"]
    assert "childLayout" not in saved[0].attrib["style"]
    assert "exitX" not in cell(result, "e").attrib["style"]
    assert "strokeColor=#123456" in cell(result, "e").attrib["style"]
    assert not any(value in dot[0] for value in ("private", "Résumé", "document", "#dae8fc"))


def test_nested_geometry_is_preserved_but_auto_refuses_it(dot: list[str]):
    source = model(positioned=True)
    cell(source, "b").set("parent", "a")
    geometry(source, "b").set("relative", "1")
    saved = ET.fromstring(convert(xml(source)))
    assert cell(saved, "b").get("parent") == "a"
    assert xml(geometry(saved, "b")) == xml(geometry(source, "b"))
    with pytest.raises(DiagramError, match="flat graphs"):
        convert(xml(source), preserve_layout=False)
    cell(source, "b").remove(geometry(source, "b"))
    with pytest.raises(DiagramError, match="flat graphs"):
        convert(xml(source))
    assert not dot


def test_detached_edge_requires_points_and_preservation(dot: list[str]):
    source = model(positioned=True)
    del cell(source, "e").attrib["source"]
    with pytest.raises(DiagramError, match="sourcePoint"):
        convert(xml(source))
    edge_geometry = ET.SubElement(
        cell(source, "e"), "mxGeometry", {"as": "geometry", "relative": "1"}
    )
    ET.SubElement(edge_geometry, "mxPoint", {"as": "sourcePoint", "x": "5", "y": "5"})
    convert(xml(source))
    with pytest.raises(DiagramError, match="source and target"):
        convert(xml(source), preserve_layout=False)
    assert not dot


@pytest.mark.parametrize(
    "field,value",
    [
        ("width", "0"),
        ("height", "-1"),
        ("x", "NaN"),
        ("y", "inf"),
        ("width", "one"),
        ("x", "1000001"),
    ],
)
@pytest.mark.parametrize("preserve", [True, False])
def test_invalid_supplied_geometry_is_never_repaired(
    field: str, value: str, preserve: bool, dot: list[str]
):
    source = model(positioned=True)
    geometry(source, "a").set(field, value)
    with pytest.raises(DiagramError, match="Page 1"):
        convert(xml(source), preserve_layout=preserve)
    assert not dot


@pytest.mark.parametrize(
    "mutation,message",
    [
        (lambda g: cell(g, "b").set("id", "a"), "Duplicate cell"),
        (lambda g: cell(g, "e").set("target", "missing"), "target must reference"),
        (lambda g: cell(g, "a").set("parent", "missing"), "valid parent"),
        (lambda g: cell(g, "a").set("parent", "a"), "cyclic parent"),
        (lambda g: cell(g, "a").set("edge", "1"), "invalid vertex/edge"),
        (lambda g: cell(g, "1").set("parent", "a"), "Structural cells"),
        (lambda g: cell(g, "0").set("parent", "1"), "must not have a parent"),
        (lambda g: cell(g, "1").set("vertex", "1"), "layer cells"),
    ],
)
def test_invalid_graphs_are_rejected(mutation, message: str, dot: list[str]):
    source = model()
    mutation(source)
    with pytest.raises(DiagramError, match=message):
        convert(xml(source))
    assert not dot


@pytest.mark.parametrize(
    "source,message",
    [
        ("", "Invalid XML"),
        ("<mxfile>", "Invalid XML"),
        ('<!DOCTYPE x [<!ENTITY e SYSTEM "file:///private">]><mxfile/>', "DTD"),
        ("<mxfile><diagram>compressed</diagram></mxfile>", "uncompressed"),
        ("<mxGraphModel><root/><root/></mxGraphModel>", "one root"),
        ("<x>" * 65 + "</x>" * 65, "depth"),
        ("x" * (renderer.MAX_INPUT_BYTES + 1), "1 MiB"),
    ],
    ids=["empty", "truncated", "dtd", "compressed", "roots", "depth", "size"],
)
def test_invalid_xml(source: str, message: str):
    with pytest.raises(DiagramError, match=message):
        convert(source)


@pytest.mark.skipif(shutil.which("dot") is None, reason="Graphviz is not installed")
@pytest.mark.parametrize("direction", ["TB", "LR"])
def test_real_graphviz_cycles_layers_self_loops_parallel_and_disconnected_nodes(direction: str):
    source = model()
    root = source[0]
    ET.SubElement(root, "mxCell", {"id": "layer2", "parent": "0"})
    ET.SubElement(
        root,
        "mxCell",
        {"id": "isolated", "parent": "layer2", "vertex": "1", "value": "Disconnected"},
    )
    for identifier, start, end in (("cycle", "b", "a"), ("self", "a", "a"), ("parallel", "a", "b")):
        ET.SubElement(
            root,
            "mxCell",
            {"id": identifier, "parent": "1", "edge": "1", "source": start, "target": end},
        )
    result = ET.fromstring(convert(xml(source), direction=direction))
    rectangles = [geometry(result, identifier) for identifier in ("a", "b", "isolated")]
    for i, first in enumerate(rectangles):
        for second in rectangles[i + 1 :]:
            assert any(
                float(first.attrib[axis]) + float(first.attrib[size]) <= float(second.attrib[axis])
                or float(second.attrib[axis]) + float(second.attrib[size])
                <= float(first.attrib[axis])
                for axis, size in (("x", "width"), ("y", "height"))
            )
    assert cell(result, "isolated").get("parent") == "layer2"
    for identifier in ("e", "cycle", "self", "parallel"):
        assert len(geometry(result, identifier).findall("Array/mxPoint")) >= 2
        for endpoint in ("source", "target"):
            assert cell(result, identifier).get(endpoint) == cell(source, identifier).get(endpoint)


def test_layout_timeout_and_output_flood_are_bounded(monkeypatch: pytest.MonkeyPatch):
    popen = subprocess.Popen

    def process_for(code: str):
        def launch(_args, **kwargs):
            return popen([sys.executable, "-u", "-c", code], **kwargs)

        monkeypatch.setattr(subprocess, "Popen", launch)

    process_for("import time; time.sleep(30)")
    started = time.monotonic()
    with pytest.raises(DiagramError, match="timed out"):
        renderer._dot("x" * 100_000, time.monotonic() + 0.2)
    assert time.monotonic() - started < 5
    for stream in ("stdout", "stderr"):
        process_for(f"import sys; sys.{stream}.buffer.write(b'x' * 4000000)")
        with pytest.raises(DiagramError, match="output limit"):
            renderer._dot("", time.monotonic() + 5)


def test_cli_never_writes_a_partial_file(tmp_path: Path):
    document = ET.Element("mxfile")
    ET.SubElement(document, "diagram", id="good").append(model(positioned=True))
    ET.SubElement(document, "diagram", id="bad").append(ET.Element("mxGraphModel"))
    (tmp_path / "input.xml").write_text(xml(document), encoding="utf-8")
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            renderer.__file__,
            "--preserve-layout",
            "true",
            "--direction",
            "TB",
            "--timeout",
            "5",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 2
    assert "Page 2" in result.stderr
    assert not (tmp_path / "diagram.drawio").exists()
