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
from pypdf.generic import DictionaryObject, NameObject, RectangleObject

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


@pytest.fixture
def pdf_pages(monkeypatch):
    text = (
        "Export \ufb01xture\nwide table\ncontent\nline_69\nStart report\n"
        "Finish report\nSupplied SVG\nPage 1 of 2"
    )
    descriptor = DictionaryObject(
        {
            NameObject("/FontName"): NameObject("/DejaVuSansMono"),
            NameObject("/FontFile2"): DictionaryObject(),
        }
    )
    font = DictionaryObject({NameObject("/FontDescriptor"): descriptor})
    proportional = DictionaryObject(
        {
            NameObject("/FontDescriptor"): DictionaryObject(
                {
                    NameObject("/FontName"): NameObject("/ABCDEF+DejaVuSans"),
                    NameObject("/FontFile2"): DictionaryObject(),
                }
            )
        }
    )

    class Page(dict):
        def __init__(self):
            super().__init__({"/Resources": {"/Font": {"/F1": font, "/F2": proportional}}})
            self.text = text
            self.mediabox = RectangleObject((0, 0, 595.91998, 842.88))

        def extract_text(self):
            return self.text

    pages = [Page(), Page()]
    monkeypatch.setattr(qualification, "PdfReader", lambda _: SimpleNamespace(pages=pages))
    return pages


@pytest.mark.parametrize("missing", [False, True])
@pytest.mark.parametrize("mono_only", [False, True])
def test_pdf_content_comparison_preserves_missing_text_failures(
    pdf_pages, tmp_path, missing, mono_only
):
    for page in pdf_pages:
        if missing:
            page.text = page.text.replace("Supplied SVG", "")
        if mono_only:
            del page["/Resources"]["/Font"]["/F2"]
    text_path = tmp_path / "fixture.txt"
    if missing:
        with pytest.raises(ValueError, match="missing extracted text: Supplied SVG"):
            qualification.inspect_pdf(b"pdf", page_size="A4", text_path=text_path)
    elif mono_only:
        with pytest.raises(ValueError, match="text font missing"):
            qualification.inspect_pdf(b"pdf", page_size="A4", text_path=text_path)
    else:
        result = qualification.inspect_pdf(b"pdf", page_size="A4", text_path=text_path)
        assert result["pages"] == 2
    assert text_path.read_text("utf-8") == "\n".join(page.text for page in pdf_pages)


@pytest.mark.parametrize(
    "page_size,dimensions", [("A4", (595.91998, 842.88)), ("Letter", (612, 792))]
)
def test_pdf_dimensions_match_requested_size(pdf_pages, page_size, dimensions):
    for page in pdf_pages:
        page.mediabox = RectangleObject((0, 0, *dimensions))
    result = qualification.inspect_pdf(b"pdf", page_size=page_size)
    assert result["page_dimensions"] == [dimensions, dimensions]


@pytest.mark.parametrize("page_index", [0, 1])
def test_letter_qualification_refuses_any_a4_page(pdf_pages, tmp_path, page_index):
    pdf_pages[1 - page_index].mediabox = RectangleObject((0, 0, 612, 792))
    text_path = tmp_path / "wrong-size.txt"
    with pytest.raises(ValueError, match="page dimensions differ from Letter"):
        qualification.inspect_pdf(b"pdf", page_size="Letter", text_path=text_path)
    assert text_path.read_text("utf-8") == "\n".join(page.text for page in pdf_pages)


def test_page_dimensions_outside_rounding_tolerance_are_refused(pdf_pages):
    pdf_pages[0].mediabox = RectangleObject((0, 0, 614, 792))
    with pytest.raises(ValueError, match="page dimensions differ from Letter"):
        qualification.inspect_pdf(b"pdf", page_size="Letter")


def test_failed_pdf_validation_retains_fixture(monkeypatch, tmp_path):
    backend = InProcessSandboxBackend(InProcessSandbox(), isolation=Isolation.CONTAINER)
    monkeypatch.setattr(DockerSandboxBackend, "create", AsyncMock(return_value=backend))
    monkeypatch.setattr(qualification, "docker", lambda *args: "sha256:fixture")
    monkeypatch.setattr(qualification, "export_pdf", AsyncMock(return_value=b"invalid pdf"))

    with pytest.raises(PdfReadError):
        asyncio.run(qualification.check("fixture", directory=tmp_path))

    assert (tmp_path / "fixture-A4.pdf").read_bytes() == b"invalid pdf"
