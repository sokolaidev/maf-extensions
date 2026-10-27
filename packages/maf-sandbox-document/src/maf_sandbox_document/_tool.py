"""Document reads through core's labelled store, call isolation and bounded output delivery."""

from __future__ import annotations

import base64
import json
import logging
import math
import re
from collections.abc import Awaitable, Callable
from importlib.resources import files
from pathlib import PurePosixPath
from typing import Any, cast

from maf_sandbox import (
    CallerContext,
    Capability,
    Cleanup,
    DeclaredOutput,
    Egress,
    FileStoreProvenance,
    Isolation,
    IsolationScope,
    OsFamily,
    OutputSink,
    SandboxRouter,
    SandboxSpec,
    SourceIntegrity,
    TransferLimits,
    collect_outputs,
)
from maf_sandbox.maf import SandboxResult, SandboxToolSession, sandboxed_tool

from ._guest import (
    MAX_DIAGNOSTIC,
    MAX_INPUT_BYTES,
    MAX_METADATA_BYTES,
    MAX_OUTPUT_BYTES,
    MAX_OUTPUT_TOTAL,
    selected_pages,
)

DOCUMENT_KIND = "document"
DOCUMENT_READ_TOOL_NAME = "document_read"
_LOGGER = logging.getLogger(__name__)


def document_sandbox_spec(image: str | None = None) -> SandboxSpec:
    """Require a disposable, isolated POSIX guest with closed egress for every document."""
    return SandboxSpec(
        kind=DOCUMENT_KIND,
        image=image,
        work_dir="/maf-sandbox/work",
        requires=frozenset({Capability.EXEC, Capability.FILES_IN, Capability.FILES_OUT}),
        requires_os_family=OsFamily.POSIX,
        egress=Egress.CLOSED,
        min_isolation=Isolation.CONTAINER,
        isolation_scope=IsolationScope.CALL,
        min_cleanup=Cleanup.DISPOSE,
        confined_to_guest_call_path=False,
        outputs_named_at_call_time=True,
        files_in=TransferLimits(
            max_bytes_per_file=MAX_INPUT_BYTES,
            max_total_bytes=MAX_INPUT_BYTES + 256 * 1024,
            max_files=4,
        ),
        files_out=TransferLimits(
            max_bytes_per_file=MAX_OUTPUT_BYTES,
            max_total_bytes=MAX_OUTPUT_TOTAL,
            max_files=101,
        ),
    )


def make_document_tools(
    router: SandboxRouter | None,
    file_store: Any,
    agent_id: str,
    context: CallerContext,
    sink: OutputSink,
    *,
    image: str | None = None,
    exec_timeout_seconds: float = 60,
    max_pages: int = 200,
    max_chars: int = 1_000_000,
    image_dpi: int = 96,
    max_image_pixels: int = 16_000_000,
    max_image_pages: int = 20,
    formats: tuple[str, ...] = ("text", "markdown", "images"),
    file_store_provenance: FileStoreProvenance | None = None,
    requires_file_integrity: SourceIntegrity | None = None,
) -> list[Any]:
    """Attach document_read(file, format, pages) to a store whose read returns bytes.

    The context supplies the authorized listing. The host selects all limits and the output
    sink; the model selects one listed document and a 1-based inclusive page/slide range.
    """
    if (
        type(exec_timeout_seconds) not in (int, float)
        or not math.isfinite(exec_timeout_seconds)
        or not 0 < exec_timeout_seconds <= 300
    ):
        raise ValueError("exec_timeout_seconds must be finite and in (0, 300]")
    settings = {
        "max_pages": max_pages,
        "max_chars": max_chars,
        "image_dpi": image_dpi,
        "max_image_pixels": max_image_pixels,
        "max_image_pages": max_image_pages,
    }
    ceilings = {
        "max_pages": 10_000,
        "max_chars": 8_000_000,
        "image_dpi": 600,
        "max_image_pixels": 32_000_000,
        "max_image_pages": 100,
    }
    for name, value in settings.items():
        if type(value) is not int or not 1 <= value <= ceilings[name]:
            raise ValueError(f"{name} must be an integer in [1, {ceilings[name]}]")
    if (
        not isinstance(cast(object, formats), tuple)
        or not formats
        or any(value not in ("text", "markdown", "images") for value in formats)
        or len(set(formats)) != len(formats)
    ):
        raise ValueError("formats must be a nonempty tuple of distinct text, markdown or images")
    return sandboxed_tool(
        lambda session: _build_tool(
            session, file_store, sink, settings, formats, exec_timeout_seconds
        ),
        router=router,
        agent_id=agent_id,
        context=context,
        spec=document_sandbox_spec(image),
        name=DOCUMENT_READ_TOOL_NAME,
        approval_mode="never_require",
        source_integrity=SourceIntegrity.UNTRUSTED,
        result_contract=True,
        verdicts=("extracted", "refused"),
        output_sink=sink,
        file_store_provenance=file_store_provenance,
        requires_file_integrity=requires_file_integrity,
        admission_timeout=max(30, exec_timeout_seconds),
        logger=_LOGGER,
    )


def _stopped(sentence: str) -> SandboxResult:
    return SandboxResult(completed=False, trusted_output=(sentence,))


def _outputs(
    data: bytes, request: dict[str, Any], call_id: str, refused: bool
) -> tuple[DeclaredOutput, ...]:
    manifest = json.loads(data)
    if not isinstance(manifest, dict):
        raise ValueError("invalid document metadata")
    manifest = cast(dict[str, Any], manifest)
    if (
        set(manifest)
        != {
            "schema_version",
            "media_type",
            "format",
            "supported_formats",
            "pages",
            "selected_pages",
            "characters",
            "truncated",
            "range_clamped",
            "verdict",
            "reason",
            "files",
        }
        or type(manifest.get("schema_version")) is not int
        or manifest.get("schema_version") != 1
        or manifest.get("verdict") != ("refused" if refused else "extracted")
        or manifest.get("truncated") is not False
        or manifest.get("format") != request["format"]
        or type(manifest.get("characters")) is not int
        or not 0 <= manifest["characters"] <= request["max_chars"]
        or type(manifest.get("range_clamped")) is not bool
        or (not refused and manifest.get("reason") is not None)
        or (refused and not isinstance(manifest.get("reason"), str))
    ):
        raise ValueError("invalid document metadata")
    names: list[str] = []
    if not refused:
        count = manifest.get("pages")
        if request["kind"] in ("pdf", "pptx"):
            if type(count) is not int or not 1 <= count <= request["max_pages"]:
                raise ValueError("invalid page count")
            chosen = selected_pages(count, request["pages"])
        else:
            if count is not None:
                raise ValueError("invalid pagination")
            chosen = []
        if manifest.get("selected_pages") != chosen:
            raise ValueError("invalid selected pages")
        if request["format"] == "images":
            if request["kind"] != "pdf" or len(chosen) > request["max_image_pages"]:
                raise ValueError("invalid image output")
            names = [f"page-{number}.png" for number in chosen]
        else:
            names = ["document.md" if request["format"] == "markdown" else "document.txt"]
    if manifest.get("files") != names:
        raise ValueError("invalid output manifest")
    media = {
        ".png": "image/png",
        ".txt": "text/plain",
        ".md": "text/markdown",
        ".json": "application/json",
    }
    return tuple(
        DeclaredOutput(
            path=f"{call_id}/{name}", name=name, media_type=media[PurePosixPath(name).suffix]
        )
        for name in [*names, "document.json"]
    )


def _build_tool(
    session: SandboxToolSession,
    store: Any,
    sink: OutputSink,
    settings: dict[str, int],
    formats: tuple[str, ...],
    timeout: float,
) -> Callable[..., Awaitable[SandboxResult]]:
    programs = {
        name: files("maf_sandbox_document").joinpath(name).read_text("utf-8")
        for name in ("_guest.py", "_pdf_guard.py")
    }

    async def document_read(
        file: str, format: str = "text", pages: str | None = None
    ) -> SandboxResult:
        """Read one listed PDF, DOCX, PPTX or RTF as text, Markdown or PDF page PNGs.

        pages is a 1-based inclusive range such as "2-5" or "3", clamped to the available
        pages or slides. DOCX ranges are refused; RTF ignores pages. Unsupported formats
        and documents exceeding a cap are refused. Output includes document.json metadata.
        """
        if not isinstance(cast(object, file), str) or not file or format not in formats:
            return _stopped("Error: choose one listed file and an enabled output format")
        page_range = None
        if pages is not None:
            if not isinstance(cast(object, pages), str) or not re.fullmatch(
                r"[1-9][0-9]{0,8}(-[1-9][0-9]{0,8})?", pages
            ):
                return _stopped("Error: pages must be a positive 1-based inclusive range")
            ends = [int(part) for part in pages.split("-")]
            page_range = [ends[0], ends[-1]]
            if page_range[0] > page_range[1]:
                return _stopped("Error: pages must be an ascending range")
        listing = await session.list_files(store)
        if isinstance(listing, str):
            return _stopped(listing)
        matches = [item for item in listing if item.name == file]
        if len(matches) != 1:
            return _stopped("Error: file must name exactly one visible stored document")
        item = await session.read_file(store, matches[0], at="file", hidden=True)
        if isinstance(item, str):
            return _stopped(item)
        if item is None or item.uri is None or not item.uri.startswith("data:"):
            return _stopped("Error: the document store must return binary bytes")
        try:
            encoded = item.uri.split(",", 1)[1]
            if len(encoded) > (MAX_INPUT_BYTES + 2) // 3 * 4:
                return _stopped("Error: max_input_bytes exceeded")
            data = base64.b64decode(encoded, validate=True)
        except (ValueError, IndexError):
            return _stopped("Error: the document store returned invalid binary content")
        if len(data) > MAX_INPUT_BYTES:
            return _stopped("Error: max_input_bytes exceeded")
        key = session.key()
        if isinstance(key, str):
            return _stopped(key)
        sandbox = await session.acquire(key)
        if isinstance(sandbox, str):
            return _stopped(sandbox)
        guest_directory = session.guest_call_path()
        call_id = guest_directory.rsplit("/", 1)[-1]
        request = dict(
            settings,
            kind=PurePosixPath(file).suffix.lower().removeprefix("."),
            format=format,
            pages=page_range,
        )
        try:
            await sandbox.write_file("input.bin", data, working_directory=guest_directory)
            await sandbox.write_file(
                "request.json", json.dumps(request), working_directory=guest_directory
            )
            for name, program in programs.items():
                await sandbox.write_file(name, program, working_directory=guest_directory)
            result = await sandbox.exec(
                ["python3", "-I", "_guest.py"], working_directory=guest_directory, timeout=timeout
            )
        except TimeoutError:
            return _stopped(f"Error: document conversion timed out after {timeout:g}s")
        except Exception:
            _LOGGER.warning("document_read: staging or execution failed", exc_info=True)
            return _stopped("Error: could not run the document converter")
        if result.exit_code not in (0, 2):
            diagnostic = result.stdout if result.producer_owns_stderr else result.stderr
            return SandboxResult(
                completed=False,
                trusted_output=("Error: document conversion did not complete",),
                output=(diagnostic[:MAX_DIAGNOSTIC],),
            )
        refused = result.exit_code == 2
        try:
            metadata = await sandbox.read_file(
                "document.json", working_directory=guest_directory, max_bytes=MAX_METADATA_BYTES
            )
            outputs = _outputs(metadata, request, call_id, refused)
            landed = await collect_outputs(
                sandbox,
                session.spec,
                sink=sink,
                outputs=outputs,
                call_id=call_id,
                observer=session.observer,
                key=key,
            )
            if len(landed) != len(outputs):
                return _stopped("Error: not every document output was delivered")
        except Exception:
            _LOGGER.warning("document_read: output delivery failed", exc_info=True)
            return _stopped("Error: document output verification or delivery failed")
        diagnostic = result.stdout if result.producer_owns_stderr else result.stderr
        return SandboxResult(
            completed=True,
            verdict="refused" if refused else "extracted",
            output=tuple(item.display for item in landed)
            + ((diagnostic[:MAX_DIAGNOSTIC],) if refused else ()),
        )

    document_read.__doc__ = (
        f"{document_read.__doc__}\nEnabled output formats: {', '.join(formats)}."
    )
    return document_read
