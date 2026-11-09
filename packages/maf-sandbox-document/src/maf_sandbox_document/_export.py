"""Host-owned Markdown export with disposable rendering and bounded byte delivery."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass, replace
from importlib.resources import files
from typing import Any, Literal, cast
from uuid import uuid4

from maf_sandbox import SandboxKey, SandboxRouter, SandboxSpec, TransferLimits

from ._export_contract import MAXIMA, validate_request
from ._tool import DOCUMENT_KIND, document_sandbox_spec


@dataclass(frozen=True)
class ExportOptions:
    """Print metadata and host-selected refusal bounds, capped by the kind's maxima."""

    title: str = ""
    date: str = ""
    page_size: str = "A4"
    max_markdown_bytes: int = 1_000_000
    max_asset_bytes: int = 5_000_000
    max_assets: int = 50
    max_pages: int = 100
    max_pdf_bytes: int = 16_000_000
    deadline_seconds: float = 60
    unsupplied_images: Literal["refuse", "placeholder"] = "refuse"

    def __post_init__(self) -> None:
        validate_request(asdict(self) | {"markdown": "", "assets": {}})


@dataclass(frozen=True)
class ExportResult:
    """PDF bytes and the guest-reported image replacement count, returned after disposal."""

    pdf: bytes
    images_replaced: int


def document_export_sandbox_spec(image: str) -> SandboxSpec:
    """Require the separate export image and the document kind's isolation contract."""
    if not isinstance(cast(object, image), str) or not image.strip():
        raise ValueError("an explicit document-export image is required")
    return replace(
        document_sandbox_spec(image),
        files_in=TransferLimits(
            max_bytes_per_file=26_000_000, max_total_bytes=26_100_000, max_files=2
        ),
        files_out=TransferLimits(
            max_bytes_per_file=MAXIMA["max_pdf_bytes"],
            max_total_bytes=MAXIMA["max_pdf_bytes"] + 4096,
            max_files=2,
        ),
    )


async def export_pdf(
    markdown: str,
    assets: Mapping[str, bytes],
    options: ExportOptions = ExportOptions(),
    *,
    router: SandboxRouter,
    key: SandboxKey,
    image: str,
) -> bytes:
    """Return a PDF after successful disposal; refuse invalid inputs with ValueError.

    The host supplies routing identity and a document-export image. Each export gets a fresh
    call identity; no store is read or written. Output bytes remain untrusted document content.
    TimeoutError covers staging, rendering and output reads; cleanup has its own 30-second bound.
    """
    result = await export_pdf_result(markdown, assets, options, router=router, key=key, image=image)
    return result.pdf


async def export_pdf_result(
    markdown: str,
    assets: Mapping[str, bytes],
    options: ExportOptions = ExportOptions(),
    *,
    router: SandboxRouter,
    key: SandboxKey,
    image: str,
) -> ExportResult:
    """Export with the same guards as export_pdf, also returning the replacement count.

    Placeholder mode requires an image whose renderer reports images_replaced. Both the PDF
    and count are guest-derived; no result is delivered before successful disposal.
    """
    if not isinstance(cast(object, options), ExportOptions):
        raise TypeError("options must be ExportOptions")
    if not isinstance(cast(object, assets), Mapping) or len(assets) > options.max_assets:
        raise ValueError("max_assets exceeded or assets is not a mapping")
    if any(not isinstance(cast(object, data), bytes) for data in assets.values()):
        raise ValueError("assets must contain bytes")
    if sum(len(data) for data in assets.values()) > options.max_asset_bytes:
        raise ValueError("max_asset_bytes exceeded")
    request = asdict(options) | {
        "markdown": markdown,
        "assets": {name: base64.b64encode(data).decode("ascii") for name, data in assets.items()},
    }
    validate_request(request)
    spec = document_export_sandbox_spec(image)
    call_key = replace(key, call_id=uuid4().hex)
    guest_directory = f"{spec.work_dir}/{call_key.call_id}"
    sandbox = None
    try:
        async with asyncio.timeout(options.deadline_seconds):
            sandbox = await router.acquire(call_key, spec)
            await sandbox.write_file(
                "request.json",
                json.dumps(request, ensure_ascii=False),
                working_directory=guest_directory,
            )
            await sandbox.write_file(
                "_export_contract.py",
                files("maf_sandbox_document").joinpath("_export_contract.py").read_text("utf-8"),
                working_directory=guest_directory,
            )
            result = await sandbox.exec(
                ["python3", "-I", "/opt/maf-document-export/render.py"],
                working_directory=guest_directory,
                timeout=options.deadline_seconds,
            )
            if result.exit_code == 2:
                raise ValueError("document export refused: " + result.stderr[:1024])
            if result.exit_code != 0:
                raise RuntimeError("document export did not complete")
            metadata = json.loads(
                await sandbox.read_file(
                    "export.json", working_directory=guest_directory, max_bytes=4096
                )
            )
            if not isinstance(metadata, dict):
                raise ValueError("invalid export metadata")
            metadata = cast(dict[str, Any], metadata)
            images_replaced = metadata.get(
                "images_replaced", 0 if options.unsupplied_images == "refuse" else None
            )
            if (
                type(metadata.get("schema_version")) is not int
                or metadata.get("schema_version") != 1
                or type(metadata.get("pages")) is not int
                or not 1 <= metadata["pages"] <= options.max_pages
                or metadata.get("network_requests") != []
                or type(images_replaced) is not int
                or not 0 <= images_replaced <= len(markdown)
                or (options.unsupplied_images == "refuse" and images_replaced != 0)
            ):
                raise ValueError("invalid export metadata")
            data = await sandbox.read_file(
                "document.pdf",
                working_directory=guest_directory,
                max_bytes=options.max_pdf_bytes,
            )
            if (
                not data.startswith(b"%PDF-")
                or len(data) > options.max_pdf_bytes
                or metadata.get("bytes") != len(data)
                or metadata.get("sha256") != hashlib.sha256(data).hexdigest()
            ):
                raise ValueError("invalid or oversized PDF output")
            return ExportResult(data, images_replaced)
    finally:
        if sandbox is None:
            cleaned = await router.dispose_call(call_key, spec=spec, timeout=30)
        else:
            cleaned = await router.dispose_kind(
                call_key, DOCUMENT_KIND, instance_id=sandbox.instance_id, timeout=30
            )
        if not cleaned:
            raise RuntimeError("document export sandbox cleanup failed")
