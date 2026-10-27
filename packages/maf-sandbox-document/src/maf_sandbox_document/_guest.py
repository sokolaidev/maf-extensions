"""Fixed document converter, executed only in a disposable POSIX guest."""

from __future__ import annotations

import importlib
import io
import json
import math
import re
import sys
import xml.etree.ElementTree as ET
import xml.parsers.expat
import zipfile
from contextlib import closing
from pathlib import Path, PurePosixPath
from typing import Any

MAX_INPUT_BYTES = 16 * 1024 * 1024
MAX_MEMBER_BYTES = 16 * 1024 * 1024
MAX_TOTAL_BYTES = 64 * 1024 * 1024
MAX_MEMBERS = 2048
MAX_RATIO = 200
MAX_PAGE_CHARS = 100_000
MAX_OUTPUT_BYTES = 32 * 1024 * 1024
MAX_OUTPUT_TOTAL = 64 * 1024 * 1024
MAX_METADATA_BYTES = 64 * 1024
MAX_DIAGNOSTIC = 2000
MEDIA_TYPES = {
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "rtf": "application/rtf",
}


class Refused(ValueError):
    """A named document guard refused the input."""


def selected_pages(total: int, pages: list[int] | None) -> list[int]:
    """Clamp a 1-based inclusive range to the available pages."""
    start, stop = pages or [1, total]
    return list(range(min(start, total), min(stop, total) + 1)) if total else []


def _xml_guard(data: bytes) -> None:
    def refuse(*args: Any) -> Any:
        raise Refused("XML DTDs and entities are refused")

    parser = xml.parsers.expat.ParserCreate()
    parser.StartDoctypeDeclHandler = refuse
    parser.EntityDeclHandler = refuse
    parser.ExternalEntityRefHandler = refuse
    parser.SetParamEntityParsing(xml.parsers.expat.XML_PARAM_ENTITY_PARSING_NEVER)
    parser.Parse(data, True)


def guarded_zip(data: bytes, kind: str) -> io.BytesIO:
    """Verify metadata and actual expanded bytes, then rebuild a stored-only archive."""
    rebuilt = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(data)) as archive, zipfile.ZipFile(rebuilt, "w") as output:
        members = archive.infolist()
        if len(members) > MAX_MEMBERS:
            raise Refused("max_zip_members exceeded")
        total = 0
        names: set[str] = set()
        for member in members:
            name = member.filename
            path = PurePosixPath(name)
            if (
                name in names
                or not name
                or path.is_absolute()
                or ".." in path.parts
                or "\\" in name
                or ":" in name
                or "\x00" in name
                or member.orig_filename != name
                or (member.external_attr >> 16) & 0o170000 == 0o120000
            ):
                raise Refused("unsafe ZIP member name")
            names.add(name)
            if member.file_size > MAX_MEMBER_BYTES:
                raise Refused("max_zip_member_bytes exceeded")
            total += member.file_size
            if total > MAX_TOTAL_BYTES:
                raise Refused("max_zip_total_bytes exceeded")
            if member.file_size > max(1, member.compress_size) * MAX_RATIO:
                raise Refused("max_zip_ratio exceeded")
            if member.flag_bits & 1 or member.compress_type not in (0, 8):
                raise Refused("unsupported ZIP compression or encryption")
            if path.suffix.lower() in {".zip", ".docx", ".pptx", ".xlsx", ".jar", ".7z", ".gz"}:
                raise Refused("nested archive members are refused")
        actual_total = 0
        for member in members:
            with archive.open(member) as source:
                value = source.read(MAX_MEMBER_BYTES + 1)
                if len(value) > MAX_MEMBER_BYTES:
                    raise Refused("max_zip_member_bytes exceeded")
                if len(value) != member.file_size or source.read(1):
                    raise Refused("ZIP member size mismatch")
            actual_total += len(value)
            if actual_total > MAX_TOTAL_BYTES:
                raise Refused("max_zip_total_bytes exceeded")
            if value.startswith((b"PK\x03\x04", b"PK\x05\x06", b"7z\xbc\xaf", b"\x1f\x8b")):
                raise Refused("nested archive members are refused")
            image = value.startswith((b"\x89PNG\r\n", b"\xff\xd8\xff", b"GIF8", b"BM"))
            if value and not image and not member.is_dir():
                try:
                    _xml_guard(value)
                except xml.parsers.expat.ExpatError:
                    if member.filename.endswith((".xml", ".rels")):
                        raise
            output.writestr(member.filename, value)
        expected = "word/document.xml" if kind == "docx" else "ppt/presentation.xml"
        if expected not in names or "[Content_Types].xml" not in names:
            raise Refused("document type does not match OOXML content")
        if ("ppt/presentation.xml" if kind == "docx" else "word/document.xml") in names:
            raise Refused("ambiguous OOXML content")
        # Both XML members above passed the DTD/entity guard before these structural reads.
        document = ET.fromstring(archive.read(expected))  # nosec B314
        namespace, local_name = (
            ("wordprocessingml/2006/main", "document")
            if kind == "docx"
            else ("presentationml/2006/main", "presentation")
        )
        if document.tag != f"{{http://schemas.openxmlformats.org/{namespace}}}{local_name}":
            raise Refused("document type does not match OOXML root")
        types = ET.fromstring(archive.read("[Content_Types].xml"))  # nosec B314
        expected_type = MEDIA_TYPES[kind].removesuffix(
            ".document" if kind == "docx" else ".presentation"
        )
        expected_type += ".document.main+xml" if kind == "docx" else ".presentation.main+xml"
        declared_types = [
            item.get("ContentType") for item in types if item.get("PartName") == "/" + expected
        ]
        if declared_types != [expected_type]:
            raise Refused("document type does not match OOXML content type")
    rebuilt.seek(0)
    return rebuilt


def _bounded_text(text: str, maximum: int, *, page: bool = False) -> str:
    if page and len(text) > MAX_PAGE_CHARS:
        raise Refused("max_page_chars exceeded")
    if len(text) > maximum:
        raise Refused("max_chars exceeded")
    return text


def _pdf(data: bytes, config: dict[str, Any]) -> tuple[int, list[int], str]:
    # -I excludes the call directory; load only the separately staged fixed guard.
    import runpy

    guard = runpy.run_path(str(Path(__file__).with_name("_pdf_guard.py")))
    try:
        guard["check_pdf"](data)
    except ValueError as exc:
        raise Refused(str(exc)) from exc
    pdfium = importlib.import_module("pypdfium2")
    with pdfium.PdfDocument(data) as document:
        count = len(document)
        if count > config["max_pages"]:
            raise Refused("max_pages exceeded")
        chosen = selected_pages(count, config["pages"])
        if not chosen:
            raise Refused("document has no pages")
        if config["format"] == "images" and len(chosen) > config["max_image_pages"]:
            raise Refused("max_image_pages exceeded")
        chunks: list[str] = []
        total = 0
        image_bytes = 0
        for number in chosen:
            with closing(document[number - 1]) as page:
                with closing(page.get_textpage()) as textpage:
                    if textpage.count_chars() > MAX_PAGE_CHARS:
                        raise Refused("max_page_chars exceeded")
                    if total + textpage.count_chars() > config["max_chars"]:
                        raise Refused("max_chars exceeded")
                    text = textpage.get_text_range()
                    total += len(text)
                    _bounded_text(text, config["max_chars"], page=True)
                    chunks.append(text)
                if config["format"] == "images":
                    width, height = page.get_size()
                    scale = config["image_dpi"] / 72
                    if (
                        not all(math.isfinite(v) and v > 0 for v in (width, height))
                        or math.ceil(width * scale) * math.ceil(height * scale)
                        > config["max_image_pixels"]
                    ):
                        raise Refused("max_image_pixels exceeded")
                    with closing(page.render(scale=scale)) as bitmap:
                        picture = bitmap.to_pil()
                        try:
                            picture.save(f"page-{number}.png", format="PNG")
                        finally:
                            picture.close()
                    size = Path(f"page-{number}.png").stat().st_size
                    if size > MAX_OUTPUT_BYTES:
                        raise Refused("max_output_file_bytes exceeded")
                    image_bytes += size
                    if image_bytes > MAX_OUTPUT_TOTAL - MAX_METADATA_BYTES:
                        raise Refused("max_output_total_bytes exceeded")
        return count, chosen, _bounded_text("\n\n".join(chunks), config["max_chars"])


def _pptx(archive: io.BytesIO, config: dict[str, Any]) -> tuple[int, list[int], str]:
    pptx = importlib.import_module("pptx")
    oxml = importlib.import_module("pptx.oxml")
    etree = importlib.import_module("lxml.etree")
    parser = etree.XMLParser(
        remove_blank_text=True, resolve_entities=False, load_dtd=False, no_network=True
    )
    parser.set_element_class_lookup(oxml.element_class_lookup)
    setattr(oxml, "oxml_parser", parser)
    presentation = pptx.Presentation(archive)
    count = len(presentation.slides)
    if count > config["max_pages"]:
        raise Refused("max_pages exceeded")
    chosen = selected_pages(count, config["pages"])
    chunks: list[str] = []
    for number in chosen:
        slide = presentation.slides[number - 1]
        parts: list[str] = []
        for shape in slide.shapes:
            if shape.has_text_frame:
                parts.append(shape.text)
            if shape.has_table:
                for row in shape.table.rows:
                    parts.append("\t".join(cell.text for cell in row.cells))
        if slide.has_notes_slide:
            parts.append(slide.notes_slide.notes_text_frame.text)
        text = _bounded_text("\n".join(parts), config["max_chars"], page=True)
        chunks.append(f"## Slide {number}\n\n{text}" if config["format"] == "markdown" else text)
        _bounded_text("\n\n".join(chunks), config["max_chars"])
    return count, chosen, "\n\n".join(chunks)


def _omit_image(image: Any) -> list[Any]:
    return []


def convert(data: bytes, config: dict[str, Any]) -> dict[str, Any]:
    """Write every requested output or refuse without reporting extraction success."""
    if len(data) > MAX_INPUT_BYTES:
        raise Refused("max_input_bytes exceeded")
    kind = config["kind"]
    if kind not in MEDIA_TYPES:
        raise Refused("unsupported document format")
    supported = ["text", "markdown"] + (["images"] if kind == "pdf" else [])
    metadata: dict[str, Any] = {
        "schema_version": 1,
        "media_type": MEDIA_TYPES[kind],
        "format": config["format"],
        "supported_formats": supported,
        "pages": None,
        "selected_pages": [],
        "characters": 0,
        "truncated": False,
        "range_clamped": False,
        "verdict": "refused",
        "reason": None,
        "files": [],
    }
    try:
        if config["format"] not in supported:
            raise Refused("requested format is not supported for this document")
        count, chosen = None, []
        if kind == "pdf":
            count, chosen, text = _pdf(data, config)
        elif kind in ("docx", "pptx"):
            archive = guarded_zip(data, kind)
            if kind == "pptx":
                count, chosen, text = _pptx(archive, config)
            else:
                if config["pages"] is not None:
                    raise Refused("DOCX page ranges require a pagination engine")
                mammoth = importlib.import_module("mammoth")
                if config["format"] == "markdown":
                    result = mammoth.convert_to_markdown(
                        archive,
                        external_file_access=False,
                        include_embedded_style_map=False,
                        convert_image=_omit_image,
                    )
                else:
                    result = mammoth.extract_raw_text(archive)
                text = _bounded_text(result.value, config["max_chars"])
        else:
            if not re.match(rb"\{\\rtf[1-9]", data):
                raise Refused("document type does not match RTF content")
            rtf = importlib.import_module("striprtf.striprtf")
            text = _bounded_text(rtf.rtf_to_text(data.decode("latin-1")), config["max_chars"])
        names = [f"page-{number}.png" for number in chosen]
        if config["format"] != "images":
            names = ["document.md" if config["format"] == "markdown" else "document.txt"]
            Path(names[0]).write_text(text, encoding="utf-8")
        sizes = [Path(name).stat().st_size for name in names]
        if any(size > MAX_OUTPUT_BYTES for size in sizes):
            raise Refused("max_output_file_bytes exceeded")
        if sum(sizes) > MAX_OUTPUT_TOTAL - MAX_METADATA_BYTES:
            raise Refused("max_output_total_bytes exceeded")
        metadata.update(
            pages=count,
            selected_pages=chosen,
            characters=len(text),
            verdict="extracted",
            range_clamped=bool(
                count is not None and config["pages"] and config["pages"][1] > count
            ),
            files=names,
        )
    except Refused as exc:
        metadata["reason"] = str(exc)[:MAX_DIAGNOSTIC]
    return metadata


def main() -> int:
    """Keep parser errors guest-derived and reserve exit 2 for a definitive refusal."""
    config = json.loads(Path("request.json").read_text("utf-8"))
    metadata: dict[str, Any]
    try:
        data = Path("input.bin").read_bytes()
        metadata = convert(data, config)
    except ImportError:
        print("document converter dependencies are unavailable", file=sys.stderr)
        return 3
    except (
        ValueError,
        RuntimeError,
        KeyError,
        IndexError,
        zipfile.BadZipFile,
        xml.parsers.expat.ExpatError,
    ) as exc:
        metadata = {
            "schema_version": 1,
            "verdict": "refused",
            "reason": f"invalid document: {exc}"[:2000],
            "pages": None,
            "selected_pages": [],
            "characters": 0,
            "truncated": False,
            "range_clamped": False,
            "supported_formats": [],
            "format": config["format"],
            "media_type": MEDIA_TYPES.get(config["kind"]),
            "files": [],
        }
    Path("document.json").write_text(json.dumps(metadata, ensure_ascii=True), encoding="utf-8")
    if metadata["verdict"] == "refused":
        print(metadata["reason"], file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
