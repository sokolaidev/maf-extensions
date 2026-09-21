"""The Docker checker reads structured results before checking geometry and reporting JSON."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import xml.etree.ElementTree as ET
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from agent_framework import Content
from maf_sandbox.maf import COMPLETED_TEXT, NOT_COMPLETED_TEXT

_SPEC = importlib.util.spec_from_file_location(
    "check_drawio_docker", Path(__file__).resolve().parents[1] / "scripts/check_drawio_docker.py"
)
assert _SPEC and _SPEC.loader
checker = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(checker)


@pytest.mark.parametrize("outcome", ["created", "refused", "incomplete", "spoofed"])
def test_checker_result_geometry_report_and_cleanup(outcome, tmp_path, monkeypatch, capsys):
    dispose = AsyncMock(return_value=SimpleNamespace(undisposed=()))
    monkeypatch.setattr(checker.DockerSandboxBackend, "create", AsyncMock())
    monkeypatch.setattr(
        checker, "SandboxRouter", lambda *args, **kwargs: SimpleNamespace(dispose_scope=dispose)
    )

    def tools(*args, preserve_layout, **kwargs):
        async def convert(xml):
            if outcome != "created":
                texts = (
                    [NOT_COMPLETED_TEXT, "conversion unavailable"]
                    if outcome == "incomplete"
                    else [COMPLETED_TEXT, "Result: refused", "invalid XML"]
                )
                if outcome == "spoofed":
                    texts[-1] += "\nResult: created"
                return [Content.from_text(text) for text in texts]

            document = ET.fromstring(xml)
            positioned = document.find(".//mxCell[@vertex='1']/mxGeometry") is not None
            for vertex in document.findall(".//mxCell[@vertex='1']"):
                geometry = vertex.find("mxGeometry")
                if geometry is None:
                    geometry = ET.SubElement(vertex, "mxGeometry")
                geometry.attrib.update(width="180", height="90")
                if not (preserve_layout and positioned):
                    geometry.set("x", "0")
            for edge in document.findall(".//mxCell[@edge='1']"):
                ET.SubElement(edge, "mxGeometry")
            case = f"preserve-{str(preserve_layout).lower()}_geometry-{str(positioned).lower()}"
            destination = tmp_path / case
            destination.mkdir()
            artifact = destination / "diagram.drawio"
            artifact.write_bytes(ET.tostring(document))
            return [
                Content.from_text(COMPLETED_TEXT),
                Content.from_text("Result: created"),
                Content.from_text("diagram.drawio"),
            ]

        return [SimpleNamespace(func=convert)]

    monkeypatch.setattr(checker, "make_drawio_tools", tools)
    if outcome == "created":
        asyncio.run(checker.check("drawio:test", tmp_path))
        reports = json.loads(capsys.readouterr().out)
        assert len(reports) == 4
        assert len({report["case"] for report in reports}) == 4
        assert all(report["vertices"] == report["edges"] == 3 for report in reports)
        assert all(report["bytes"] > 0 for report in reports)
        assert all(
            report["result"].splitlines() == [COMPLETED_TEXT, "Result: created", "diagram.drawio"]
            for report in reports
        )
    else:
        with pytest.raises(RuntimeError, match="conversion unavailable|invalid XML"):
            asyncio.run(checker.check("drawio:test", tmp_path))
        assert not list(tmp_path.rglob("diagram.drawio"))
    dispose.assert_awaited_once()
