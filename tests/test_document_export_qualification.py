"""Offline admission checks for the Docker export qualification host."""

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from maf_sandbox import Isolation
from maf_sandbox.testing import InProcessSandbox, InProcessSandboxBackend
from maf_sandbox_docker import DockerSandboxBackend
from pypdf.errors import PdfReadError
from pypdf.generic import DictionaryObject, NameObject

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import document_export_qualification as qualification


def test_container_backend_reaches_export(monkeypatch):
    backend = InProcessSandboxBackend(InProcessSandbox(), isolation=Isolation.CONTAINER)
    monkeypatch.setattr(DockerSandboxBackend, "create", AsyncMock(return_value=backend))
    monkeypatch.setattr(qualification, "docker", lambda *args: "sha256:fixture")
    export = AsyncMock(side_effect=RuntimeError("export reached"))
    monkeypatch.setattr(qualification, "export_pdf", export)

    with pytest.raises(RuntimeError, match="export reached"):
        asyncio.run(qualification.check("fixture"))

    export.assert_awaited_once()


@pytest.mark.parametrize("missing", [False, True])
def test_pdf_content_comparison_preserves_missing_text_failures(monkeypatch, tmp_path, missing):
    text = (
        "Export \ufb01xture\nwide table\ncontent\nline_69\nStart report\n"
        "Finish report\nSupplied SVG\nPage 1 of 2"
    )
    if missing:
        text = text.replace("Supplied SVG", "")
    descriptor = DictionaryObject(
        {
            NameObject("/FontName"): NameObject("/DejaVuSansMono"),
            NameObject("/FontFile2"): DictionaryObject(),
        }
    )
    font = DictionaryObject({NameObject("/FontDescriptor"): descriptor})

    class Page(dict):
        def extract_text(self):
            return text

    page = Page({"/Resources": {"/Font": {"/F1": font}}})
    monkeypatch.setattr(qualification, "PdfReader", lambda _: SimpleNamespace(pages=[page, page]))
    text_path = tmp_path / "fixture.txt"
    if missing:
        with pytest.raises(ValueError, match="missing extracted text: Supplied SVG"):
            qualification.inspect_pdf(b"pdf", text_path=text_path)
    else:
        result = qualification.inspect_pdf(b"pdf", text_path=text_path)
        assert result["pages"] == 2
    assert text_path.read_text("utf-8") == text + "\n" + text


def test_failed_pdf_validation_retains_fixture(monkeypatch, tmp_path):
    backend = InProcessSandboxBackend(InProcessSandbox(), isolation=Isolation.CONTAINER)
    monkeypatch.setattr(DockerSandboxBackend, "create", AsyncMock(return_value=backend))
    monkeypatch.setattr(qualification, "docker", lambda *args: "sha256:fixture")
    monkeypatch.setattr(qualification, "export_pdf", AsyncMock(return_value=b"invalid pdf"))

    with pytest.raises(PdfReadError):
        asyncio.run(qualification.check("fixture", directory=tmp_path))

    assert (tmp_path / "fixture-A4.pdf").read_bytes() == b"invalid pdf"
