"""The export verifier requires cleanup evidence from every call, including timeout."""

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
from PIL import Image

_SPEC = importlib.util.spec_from_file_location(
    "check_drawio_exports", Path(__file__).resolve().parents[1] / "scripts/check_drawio_exports.py"
)
assert _SPEC and _SPEC.loader
checker = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(checker)


@pytest.mark.parametrize("indicator", ["image", "use", "rect", None])
def test_export_output_requires_image_indicator_in_either_native_form(tmp_path, indicator):
    for page in (1, 2):
        for format in ("png", "jpg"):
            Image.new("RGB", (200 * page, 200), "red").save(tmp_path / f"diagram-{page}.{format}")
        root = ET.Element("svg", xmlns="http://www.w3.org/2000/svg")
        root.text = f"Page {page} Résumé fontFamily=Missing; data:font/ttf;base64, #00ff00"
        ET.SubElement(root, "image", href="data:image/png;base64,AA==")
        if indicator is not None:
            ET.SubElement(root, indicator, width="30", height="30")
        (tmp_path / f"diagram-{page}.svg").write_bytes(ET.tostring(root, encoding="utf-8"))
    (tmp_path / "diagram.drawio").write_text("<mxfile/>", encoding="utf-8")
    if indicator in {"image", "use"}:
        assert checker.verify_output(tmp_path)["pages"] == 2
    else:
        with pytest.raises(AssertionError, match="Indicator image was omitted"):
            checker.verify_output(tmp_path)


@pytest.mark.parametrize("timeout_event", ["clean", "unclean", "missing", "duplicate"])
def test_export_checker_requires_clean_timeout_event(timeout_event, tmp_path, monkeypatch, capsys):
    backend = SimpleNamespace(
        acquire=AsyncMock(
            return_value=SimpleNamespace(
                exec=AsyncMock(return_value=SimpleNamespace(exit_code=0, stdout="{}", stderr=""))
            )
        ),
        dispose=AsyncMock(return_value=None),
    )
    dispose_scope = AsyncMock(return_value=SimpleNamespace(undisposed=()))
    observer = None
    timeout_called = False

    def router(*args, **kwargs):
        nonlocal observer
        observer = kwargs["observer"]
        return SimpleNamespace(dispose_scope=dispose_scope)

    monkeypatch.setattr(checker, "SandboxRouter", router)
    monkeypatch.setattr(checker, "verify_output", lambda output: {})
    (tmp_path / "diagram.drawio").write_text("<mxfile/>", encoding="utf-8")
    stored = tmp_path / "stored"
    stored.mkdir()
    Image.new("RGBA", (801, 1), (0, 0, 0, 0)).save(stored / "diagram-2.png")
    Image.new("RGB", (801, 1)).save(stored / "diagram-2.jpg")

    def tools(*args, exec_timeout_seconds, **kwargs):
        async def convert(**inputs):
            nonlocal timeout_called
            assert observer is not None
            if exec_timeout_seconds == 0.05:
                timeout_called = True
                if timeout_event != "missing":
                    observer.tool_call_ended(
                        SimpleNamespace(unclean=int(timeout_event == "unclean"))
                    )
                if timeout_event == "duplicate":
                    observer.tool_call_ended(SimpleNamespace(unclean=0))
                texts = [NOT_COMPLETED_TEXT]
            else:
                observer.tool_call_ended(SimpleNamespace(unclean=0))
                texts = [COMPLETED_TEXT, "Result: created"]
                if exec_timeout_seconds == 60:
                    texts = [
                        COMPLETED_TEXT,
                        "Result: refused",
                        (
                            "Prepared document; Placeholder labels; Processing instructions; "
                            "text and fonts; Unsupported resource stencil; Active SVG; nested images"
                        ),
                    ]
            return [Content.from_text(text) for text in texts]

        return [SimpleNamespace(func=convert)]

    monkeypatch.setattr(checker, "make_drawio_tools", tools)
    monkeypatch.setattr(checker, "make_drawio_export_tools", tools)
    if timeout_event == "clean":
        asyncio.run(checker.check("drawio:test", tmp_path, backend))
        report = json.loads(capsys.readouterr().out)
        assert report["clean_calls"] == 31
        assert report["timeout"] == "incomplete without artifact delivery"
    else:
        with pytest.raises(AssertionError):
            asyncio.run(checker.check("drawio:test", tmp_path, backend))
        assert capsys.readouterr().out == ""
    assert timeout_called
    backend.dispose.assert_awaited_once()
    dispose_scope.assert_awaited_once()
