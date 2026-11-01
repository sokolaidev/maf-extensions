"""Create a small Word inventory with supported and unresolved resource entries."""

from __future__ import annotations

import argparse
import io
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

PARAGRAPHS = (
    "Azure resource inventory — example, not a deployment request",
    "Region: westeurope. Purchase model: pay as you go. Currency: USD.",
    "Web servers: 2 Linux Standard_B2s virtual machines in westeurope, 730 hours each per month, pay as you go.",
    "Worker: 1 Linux Standard_D2s_v5 virtual machine in westeurope, 160 hours per month, pay as you go.",
    "Data disks: 2 Premium SSD managed disks, P10 LRS, in westeurope, provisioned for a full month.",
    "Archive: 1 Azure Blob Storage account in westeurope. Capacity, access tier and request counts are not specified.",
)


def inventory_data() -> dict[str, object]:
    """Return the known fixture inventory for model-free integration checks only."""
    items = []
    for name, key, quantity, hours, paragraph in (
        ("Web servers", "linux-b2s", 2, 730.0, 2),
        ("Worker", "linux-d2s-v5", 1, 160.0, 3),
        ("Data disks", "premium-ssd-p10", 2, None, 4),
        ("Archive", "unknown", 1, None, 5),
    ):
        items.append(
            dict(
                name=name,
                product_key=key,
                quantity=quantity,
                hours_per_month=hours,
                region="westeurope",
                evidence=PARAGRAPHS[paragraph],
                notes="Usage and access tier unspecified" if key == "unknown" else "",
            )
        )
    return {"resources": items}


def document_bytes() -> bytes:
    """Build the fixture without requiring a Word installation or third-party writer."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        files = {
            "[Content_Types].xml": (
                '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                '<Default Extension="xml" ContentType="application/xml"/>'
                '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
                "</Types>"
            ),
            "_rels/.rels": (
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
                "</Relationships>"
            ),
            "word/document.xml": (
                '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>'
                + "".join(f"<w:p><w:r><w:t>{escape(text)}</w:t></w:r></w:p>" for text in PARAGRAPHS)
                + "</w:body></w:document>"
            ),
        }
        for name, content in files.items():
            info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            info.create_system = 0  # Keep fixture bytes independent of the host OS.
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, content)
    return buffer.getvalue()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path, nargs="?", default=Path("resources.docx"))
    with parser.parse_args().output.open("xb") as destination:
        destination.write(document_bytes())
