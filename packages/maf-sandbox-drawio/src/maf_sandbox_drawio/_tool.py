"""The create_drawio workload and its host-selected layout policy."""

from __future__ import annotations

import json
import logging
import math
from collections.abc import Awaitable, Callable
from dataclasses import asdict
from importlib.resources import files
from typing import Any, Literal, cast

from maf_sandbox import (
    CallerContext,
    Capability,
    DeclaredOutput,
    FileStoreProvenance,
    OsFamily,
    OutputSink,
    SandboxRouter,
    SandboxSpec,
    SourceIntegrity,
    TransferLimits,
    collect_outputs,
    error_detail,
)
from maf_sandbox.maf import SandboxResult, SandboxToolSession, sandboxed_tool

from ._export import (
    EXPORT_FILE_BYTES,
    EXPORT_FILES,
    EXPORT_MANIFEST_BYTES,
    EXPORT_TOTAL_BYTES,
    DrawioExport,
)
from ._renderer import MAX_DIAGNOSTIC, MAX_INPUT_BYTES, MAX_OUTPUT_BYTES

DRAWIO_KIND = "drawio"
CREATE_DRAWIO_TOOL_NAME = "create_drawio"
_LOGGER = logging.getLogger(__name__)


#: Every answer `create_drawio` may reach about the source it was given.
#:
#: Calls without a definitive conversion result carry no verdict.
DRAWIO_VERDICTS = ("created", "refused")


def drawio_sandbox_spec(
    image: str | None = None, *, export: DrawioExport | None = None
) -> SandboxSpec:
    """Declare a POSIX workload with closed egress and bounded editable/image outputs.

    Confinement is undeclared, so the default cleanup policy disposes the sandbox.
    """
    return SandboxSpec(
        kind=DRAWIO_KIND,
        image=image,
        work_dir="/maf-sandbox/work",
        egress_allow=(),
        requires=frozenset({Capability.EXEC, Capability.FILES_IN, Capability.FILES_OUT}),
        requires_os_family=OsFamily.POSIX,
        outputs_named_at_call_time=True,
        files_out=TransferLimits(
            max_bytes_per_file=MAX_OUTPUT_BYTES if export is None else EXPORT_FILE_BYTES,
            max_total_bytes=MAX_OUTPUT_BYTES if export is None else EXPORT_TOTAL_BYTES,
            max_files=1 if export is None else EXPORT_FILES,
        ),
    )


def make_drawio_tools(
    router: SandboxRouter | None,
    agent_id: str,
    context: CallerContext,
    sink: OutputSink,
    *,
    image: str | None = None,
    preserve_layout: bool = True,
    direction: Literal["TB", "LR"] = "TB",
    exec_timeout_seconds: float = 60,
    export: DrawioExport | None = None,
) -> list[Any]:
    """Attach create_drawio(xml), preserving supplied layout unless configured otherwise.

    Missing vertex geometry always triggers automatic layout on that page. The POSIX image must
    provide python3 and Graphviz dot. Choose a sink policy suitable for repeated diagram.drawio
    names; neither the output destination nor layout settings are model arguments.
    """
    if export is not None and not isinstance(cast(object, export), DrawioExport):
        raise TypeError("export must be DrawioExport or None")
    if not isinstance(cast(object, preserve_layout), bool):
        raise TypeError("preserve_layout must be a bool")
    if direction not in {"TB", "LR"}:
        raise ValueError("direction must be TB or LR")
    if (
        isinstance(cast(object, exec_timeout_seconds), bool)
        or not isinstance(cast(object, exec_timeout_seconds), (int, float))
        or not math.isfinite(exec_timeout_seconds)
        or not 0 < exec_timeout_seconds <= 300
    ):
        raise ValueError("exec_timeout_seconds must be a finite number in (0, 300]")
    return sandboxed_tool(
        lambda session: _create_tool(
            session, sink, preserve_layout, direction, exec_timeout_seconds, export
        ),
        router=router,
        agent_id=agent_id,
        context=context,
        spec=drawio_sandbox_spec(image, export=export),
        name=CREATE_DRAWIO_TOOL_NAME,
        approval_mode="never_require",
        source_integrity=SourceIntegrity.UNTRUSTED,
        result_contract=True,
        verdicts=DRAWIO_VERDICTS,
        output_sink=sink,
        logger=_LOGGER,
    )


def _create_tool(
    session: SandboxToolSession,
    sink: OutputSink,
    preserve_layout: bool,
    direction: str,
    timeout: float,
    export: DrawioExport | None = None,
    require_layout: bool = False,
) -> Callable[..., Awaitable[SandboxResult]]:
    program = files("maf_sandbox_drawio").joinpath("_renderer.py").read_text(encoding="utf-8")

    def _stopped(sentence: str) -> SandboxResult:
        """A call that reached no answer, and this module's own sentence saying why."""
        return SandboxResult(completed=False, trusted_output=(sentence,))

    async def create_drawio(xml: str) -> SandboxResult:
        """Create an editable diagram.drawio file from native, uncompressed draw.io XML.

        Supply an mxfile containing diagram/mxGraphModel/root, or a bare mxGraphModel.
        Each page needs structural mxCell IDs 0 and 1 (parent=0), unique vertex IDs with
        parent=1 and vertex=1, and edge cells with edge=1 and source/target vertex IDs.
        Labels use value or object wrappers, with IDs on the wrapper. XML-escape values.
        DTDs and entity declarations are refused. The input limit is 1 MiB, 8 pages, and
        1000 cells per page.

        Missing geometry always gets automatic layout, using 160x80 default vertex sizes.
        Automatic layout supports flat graphs up to 200 vertices and 600 edges per page.
        Groups, relative ports and edge labels need complete supplied geometry and layout
        preservation. Supplied mxGeometry dimensions must be positive; use finite ASCII
        decimal or scientific numbers. Put custom metadata on object wrappers, not geometry.
        Correct malformed XML or invalid references using the returned diagnostic and retry.

        The result is a saved-file reference, not a preview of the diagram.

        Args:
            xml: Complete native draw.io XML; geometry is optional for flat graphs.
        """
        try:
            if not isinstance(cast(object, xml), str):
                return _stopped("Error: xml must be a string")
            if len(xml) > MAX_INPUT_BYTES or len(xml.encode("utf-8")) > MAX_INPUT_BYTES:
                return _stopped("Error: XML exceeds the 1 MiB input limit")
        except UnicodeError:
            return _stopped("Error: XML must be valid UTF-8 text")
        key = session.key()
        if isinstance(key, str):
            return _stopped(key)
        sandbox = await session.acquire(key)
        if isinstance(sandbox, str):
            return _stopped(sandbox)
        guest_call_directory = session.guest_call_path()
        call_id = guest_call_directory.rsplit("/", 1)[-1]
        try:
            await sandbox.write_file("input.xml", xml, working_directory=guest_call_directory)
            await sandbox.write_file("renderer.py", program, working_directory=guest_call_directory)
            export_args: list[str] = []
            if export is not None:
                await sandbox.write_file(
                    "export.json",
                    json.dumps(asdict(export)),
                    working_directory=guest_call_directory,
                )
                export_args = ["--export-config", "export.json"]
            if require_layout:
                export_args.append("--require-layout")
            result = await sandbox.exec(
                [
                    "python3",
                    "-I",
                    "renderer.py",
                    "--preserve-layout",
                    str(preserve_layout).lower(),
                    "--direction",
                    direction,
                    "--timeout",
                    str(timeout * 0.9),
                    *export_args,
                ],
                working_directory=guest_call_directory,
                timeout=timeout,
            )
        except TimeoutError:
            return _stopped(f"Error: draw.io conversion timed out after {timeout:g}s")
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning("create_drawio: sandbox execution failed: %s", error_detail(exc))
            return _stopped("Error: could not run the draw.io converter in the sandbox")
        if result.exit_code != 0:
            guest_diagnostic = result.stdout if result.producer_owns_stderr else result.stderr
            diagnostic = (guest_diagnostic or "The converter returned no diagnostic")[
                :MAX_DIAGNOSTIC
            ]
            refused = result.exit_code == 2
            return SandboxResult(
                completed=refused,
                verdict="refused" if refused else None,
                trusted_output=(
                    "The converter rejected the diagram. Its diagnostic follows."
                    if refused
                    else "The converter could not complete the diagram.",
                ),
                # The converter's text, quoting whatever the supplied source made it say.
                output=(f"draw.io conversion failed (exit {result.exit_code}): {diagnostic}",),
            )
        try:
            outputs = (
                DeclaredOutput(
                    path=f"{call_id}/diagram.drawio",
                    name="diagram.drawio",
                    media_type="application/xml",
                ),
            )
            if export is not None:
                manifest = await sandbox.read_file(
                    "exports.json",
                    working_directory=guest_call_directory,
                    max_bytes=EXPORT_MANIFEST_BYTES,
                )
                outputs += _export_outputs(manifest, export, call_id)
            landed = await collect_outputs(
                sandbox,
                session.spec,
                sink=sink,
                outputs=outputs,
                call_id=call_id,
                observer=session.observer,
                key=key,
            )
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning("create_drawio: output delivery failed: %s", error_detail(exc))
            return _stopped("Error: delivery of diagram.drawio failed")
        if not landed:
            return _stopped("Error: the converter produced no diagram.drawio file")
        return SandboxResult(
            completed=True, verdict="created", output=tuple(item.display for item in landed)
        )

    policy = (
        "Preserve supplied page geometry; automatically lay out pages with missing geometry."
        if preserve_layout
        else "Replace every page's layout automatically; only flat graphs are supported."
    )
    create_drawio.__doc__ = (
        f"{create_drawio.__doc__}\nConfigured layout: {policy} Direction: {direction}."
    )
    if export is not None:
        create_drawio.__doc__ += (
            f" Also export {', '.join(export.formats)} using offline assets. "
            "External resources and unsupported export content are refused."
        )
    return create_drawio


def _export_outputs(data: bytes, export: DrawioExport, call_id: str) -> tuple[DeclaredOutput, ...]:
    parsed = json.loads(data)
    if not isinstance(parsed, dict):
        raise ValueError("Invalid export manifest")
    manifest = cast(dict[str, Any], parsed)
    if (
        set(manifest) != {"pages", "files"}
        or type(manifest["pages"]) is not int
        or not 1 <= manifest["pages"] <= 8
    ):
        raise ValueError("Invalid export manifest")
    pages = export.pages or tuple(range(1, manifest["pages"] + 1))
    if any(page > manifest["pages"] for page in pages):
        raise ValueError("Export page is absent")
    names = [f"diagram-{page}.{format}" for page in pages for format in export.formats]
    if manifest["files"] != names:
        raise ValueError("Export manifest does not match requested outputs")
    media = {"png": "image/png", "jpg": "image/jpeg", "svg": "image/svg+xml"}
    return tuple(
        DeclaredOutput(
            path=f"{call_id}/{name}", name=name, media_type=media[name.rsplit(".", 1)[1]]
        )
        for name in names
    )


def make_drawio_export_tools(
    router: SandboxRouter | None,
    agent_id: str,
    context: CallerContext,
    sink: OutputSink,
    store: Any,
    *,
    export: DrawioExport,
    image: str | None = None,
    exec_timeout_seconds: float = 60,
    file_store_provenance: FileStoreProvenance | None = None,
    requires_file_integrity: SourceIntegrity | None = None,
) -> list[Any]:
    """Attach export_drawio(file), reading an exact authorized store reference with geometry.

    The store supplies uncompressed XML text. Resources must be embedded or present in the
    offline runtime; filenames never become guest paths or commands.
    """
    if not isinstance(cast(object, export), DrawioExport):
        raise TypeError("export must be DrawioExport")
    if (
        type(exec_timeout_seconds) not in (int, float)
        or not math.isfinite(exec_timeout_seconds)
        or not 0 < exec_timeout_seconds <= 300
    ):
        raise ValueError("exec_timeout_seconds must be a finite number in (0, 300]")

    def factory(session: SandboxToolSession) -> Callable[..., Awaitable[SandboxResult]]:
        create = _create_tool(session, sink, True, "TB", exec_timeout_seconds, export, True)

        async def export_drawio(file: str) -> SandboxResult:
            """Export an existing editable diagram by its exact visible file-store reference.

            Supply uncompressed draw.io XML with complete geometry. The configured formats
            and pages are exported offline; the diagram is not laid out again.
            """
            if not isinstance(cast(object, file), str):
                return SandboxResult(completed=False, trusted_output=("Error: file must be text",))
            listing = await session.list_files(store)
            if isinstance(listing, str):
                return SandboxResult(completed=False, trusted_output=(listing,))
            matches = [item for item in listing if item.name == file]
            if len(matches) != 1:
                return SandboxResult(
                    completed=False,
                    trusted_output=("Error: file must name exactly one visible stored diagram",),
                )
            item = await session.read_file(store, matches[0], at="file", hidden=True)
            if isinstance(item, str):
                return SandboxResult(completed=False, trusted_output=(item,))
            if item is None or item.text is None:
                return SandboxResult(
                    completed=False, trusted_output=("Error: the stored diagram has no XML text",)
                )
            return await create(item.text)

        return export_drawio

    return sandboxed_tool(
        factory,
        router=router,
        agent_id=agent_id,
        context=context,
        spec=drawio_sandbox_spec(image, export=export),
        name="export_drawio",
        file_store_provenance=file_store_provenance,
        requires_file_integrity=requires_file_integrity,
        approval_mode="never_require",
        source_integrity=SourceIntegrity.UNTRUSTED,
        result_contract=True,
        verdicts=DRAWIO_VERDICTS,
        output_sink=sink,
        logger=_LOGGER,
    )
