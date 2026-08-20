"""The `diagram-generator` kind, written against the published protocol alone.

This is the file to read if the question is *how do I write a kind*. Nothing here imports a
packaged workload, and nothing here is specific to how `agent.py` happens to wire it up: a kind
is a spec saying what sandbox it needs, a tool body driving `write_file` / `exec` /
`collect_outputs`, and a factory handing both to `sandboxed_tool`.

It lives beside `agent.py` rather than inside it because the two answer different questions, and
a reader who came for either had to read past the other. `agent.py` is the same host wiring as
every other sample; this is the part none of the others have. The seam between them is one
name — `make_diagram_tools` — which is a fair measure of how separable they were.

Imported, never run, so it carries no PEP 723 block of its own; `agent.py` declares what both
files need. `sys.path[0]` is the script's directory, which is what lets `agent.py` import it.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from maf_sandbox import (
    CallerContext,
    Capability,
    DeclaredOutput,
    OutputSink,
    SandboxRouter,
    SandboxSpec,
    TransferLimits,
    collect_outputs,
    error_detail,
)
from maf_sandbox.maf import SandboxToolSession, sandboxed_tool

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)


#: The sandbox kind this workload asks for.
DIAGRAM_KIND = "diagram-generator"

RENDER_DIAGRAM_TOOL_NAME = "render_diagram"

#: Where the source is written and the image is produced — a dedicated root, not the image's own
#: tree.
_WORK_DIR = "/maf-sandbox/work"

_RENDERER = "dot"
#: The output format, single-sourced: it is `dot`'s `-T` value and the image's file extension.
_OUTPUT_FORMAT = "png"
_FORMAT_FLAG = f"-T{_OUTPUT_FORMAT}"
_OUTPUT_FLAG = "-o"
#: One fixed name each. The sandbox is reused across calls, so concurrent calls would share
#: these paths — the render is serialised per attached tool (see `_render_diagram_tool`) so one
#: call's source and image are never overwritten by another's mid-render.
_SOURCE_FILENAME = "diagram.dot"
_OUTPUT_FILENAME = f"diagram.{_OUTPUT_FORMAT}"
_OUTPUT_MEDIA_TYPE = "image/png"

#: A rendered PNG of a hand-authored graph is kilobytes; the cap is generous and the collection
#: is a single file.
_MAX_IMAGE_BYTES = 4 * 1024 * 1024
_FILES_OUT_LIMITS = TransferLimits(
    max_bytes_per_file=_MAX_IMAGE_BYTES,
    max_total_bytes=_MAX_IMAGE_BYTES,
    max_files=1,
)

_DEFAULT_TIMEOUT_SECONDS = 60


def diagram_sandbox_spec(image: str | None = None) -> SandboxSpec:
    """The sandbox a diagram render needs, in backend-neutral terms.

    ``egress_allow=()`` because rendering is pure computation — ``dot`` reads the source it was
    given and writes an image, and reaches nothing.  The one declared output lands (``FILES_OUT``)
    and is ``required=False``: a ``dot`` that rejects malformed source produces no file, and that
    absence is a diagnostic the model should fix rather than a transfer error.
    """
    return SandboxSpec(
        kind=DIAGRAM_KIND,
        image=image,
        egress_allow=(),
        work_dir=_WORK_DIR,
        requires=frozenset({Capability.EXEC, Capability.FILES_IN, Capability.FILES_OUT}),
        declared_outputs=(
            DeclaredOutput(
                path=_OUTPUT_FILENAME,
                media_type=_OUTPUT_MEDIA_TYPE,
                required=False,
            ),
        ),
        files_out=_FILES_OUT_LIMITS,
    )


def make_diagram_tools(
    router: SandboxRouter | None,
    agent_dir: str,
    context: CallerContext,
    sink: OutputSink,
    *,
    image: str | None = None,
    exec_timeout_seconds: int = _DEFAULT_TIMEOUT_SECONDS,
) -> list[Any]:
    """Return the ``[render_diagram]`` tool list, or ``[]`` when no sandbox is available.

    Args:
        router: The sandbox router, or ``None`` when sandboxing is not configured.
        agent_dir: The agent's directory name. Baked into the sandbox key at factory time
            rather than taken from the model at call time.
        context: How to read the caller's scope and thread.
        sink: Where the rendered image lands. Passed to ``sandboxed_tool`` so the tool's
            declarations know it lands something, and threaded into the body for
            ``collect_outputs``.
        image: OCI reference of a sandbox image with ``dot`` on its path.
        exec_timeout_seconds: Per-render bound. A sandbox that stops answering must not hold the
            caller's turn open.
    """
    spec = diagram_sandbox_spec(image)
    return sandboxed_tool(
        lambda session: _render_diagram_tool(session, sink, exec_timeout_seconds),
        router=router,
        context=context,
        agent_dir=agent_dir,
        spec=spec,
        name=RENDER_DIAGRAM_TOOL_NAME,
        approval_mode="never_require",
        # `source_integrity` stays at its "trusted" default: the result is deterministic
        # first-party output from a no-identity, closed-egress sandbox — a host-authored
        # reference on success, `dot`'s own diagnostic on failure — the same basis on which the
        # Bicep workload trusts a compiler's output. It is not model-authored content.
        output_sink=sink,
        logger=logger,
    )


def _render_diagram_tool(
    session: SandboxToolSession,
    sink: OutputSink,
    timeout: int,
) -> Callable[..., Awaitable[str]]:
    """Build the ``render_diagram`` body for one attached tool.

    Defined at module level rather than nested inside :func:`make_diagram_tools`, and that is
    not a style choice: the function below's **docstring is the tool's description** — MAF passes
    ``__doc__`` through verbatim, indentation and all — so nesting this one level deeper would
    re-indent every line of what the model reads.
    """

    # The function calls in one assistant message run concurrently, so two `render_diagram`
    # calls can drive the same sandbox at once — `maf_sandbox._protocol.Sandbox.acquire`
    # documents this. Both write `diagram.dot` and read `diagram.png` at the fixed paths below,
    # so without guarding, one call could collect the other's image. This lock serialises the
    # write -> exec -> collect sequence per attached tool.
    #
    # A FILES_OUT kind could instead build its `DeclaredOutput` per call under
    # `guest_call_path()`, using `name` to keep the landed artifact name stable while the
    # framework reclaims the call path. This sample keeps the output in the spec so a
    # first-custom-kind reader sees the contract at attach time; the lock is that choice's price.
    render_lock = asyncio.Lock()

    async def render_diagram(dot: str) -> str:
        """Render a Graphviz diagram from DOT source and save it as a PNG image.

        Use this to turn a graph you describe in Graphviz DOT into an actual image: write the
        complete DOT source and this renders it with ``dot`` inside a sandbox with **no network
        access**, then saves the resulting PNG to host storage.

        **The image itself does not come back — a reference to it does.**  The result names
        where the PNG was saved; it does not contain the image, so do not claim to have seen the
        picture or describe its pixels.  Say where it was saved and stop.

        Write valid, self-contained DOT every time — a full ``digraph { ... }`` or
        ``graph { ... }``.  If the source has a syntax error the renderer rejects it and you get
        its diagnostic back instead of an image; fix the DOT and call again.

        Args:
            dot: The Graphviz DOT source to render.

        Returns:
            A one-line reference to where the PNG was saved; or the renderer's diagnostic when
            the DOT could not be parsed; or an error message when the sandbox was unavailable,
            so the run degrades rather than blocking.
        """
        # Scope and thread come from the host's request context, never from model input.
        key = session.key()
        if isinstance(key, str):
            return key

        sandbox = await session.acquire(key)
        if isinstance(sandbox, str):
            return sandbox

        source_path = f"{session.spec.work_dir}/{_SOURCE_FILENAME}"
        output_path = f"{session.spec.work_dir}/{_OUTPUT_FILENAME}"

        # One call's write -> exec -> collect must complete before the next touches the shared
        # paths — see the note where `render_lock` is created.
        async with render_lock:
            try:
                await sandbox.write_file(source_path, dot, working_directory=session.spec.work_dir)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "render_diagram: could not write the DOT source into the sandbox: %s",
                    error_detail(exc),
                )
                return "Error: could not write the diagram source into the sandbox"

            try:
                # An argv sequence, never a command line: the source is a written file and the
                # renderer's arguments are fixed, so nothing the model wrote reaches a shell.
                result = await sandbox.exec(
                    [_RENDERER, _FORMAT_FLAG, source_path, _OUTPUT_FLAG, output_path],
                    working_directory=session.spec.work_dir,
                    timeout=timeout,
                )
            except TimeoutError:
                logger.warning("render_diagram: dot timed out after %ss", timeout)
                return f"Error: rendering timed out after {timeout}s"
            except Exception as exc:  # noqa: BLE001
                # Provider/transport detail can carry account ids — must not reach the transcript.
                logger.warning("render_diagram: exec failed: %s", error_detail(exc))
                return "Error: could not run the renderer in the sandbox"

            if result.exit_code != 0:
                # dot rejects malformed DOT with a diagnostic on stderr. That is the model's to
                # fix, not a transport failure — hand it back so the next attempt can correct the
                # source. The declared output is required=False, so an absent file here is not a
                # transfer error.
                logger.info("render_diagram: dot exited %d", result.exit_code)
                return _render_failed(result.exit_code, (result.stderr or "").rstrip("\n"))

            try:
                landed = await collect_outputs(sandbox, session.spec, sink=sink)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "render_diagram: could not land the rendered image: %s",
                    error_detail(exc),
                )
                return "Error: the diagram rendered but could not be saved"

        if not landed:
            # dot exited 0 but produced no file — required=False, so collect_outputs returned
            # empty rather than raising. Nothing to report but the anomaly itself.
            return "The renderer exited cleanly but produced no image."
        return landed[0].display

    return render_diagram


def _render_failed(exit_code: int, stderr: str) -> str:
    """Render a ``dot`` failure for a model that has to fix its own DOT.

    The diagnostic is ``dot``'s own — first-party, and a syntax error names the line — so it goes
    back verbatim.  When ``dot`` failed but wrote nothing, the exit code is all there is.
    """
    if stderr:
        return f"dot could not render the diagram (exit {exit_code}):\n{stderr}"
    return f"dot could not render the diagram (exit {exit_code}) and wrote no diagnostic."
