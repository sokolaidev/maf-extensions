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
#: One name each, both inside the call's own directory. A fixed path under `work_dir` is
#: shared by every call in the conversation, which costs a collision and a leak — see the
#: README.
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
    given and writes an image, and reaches nothing.

    ``outputs_named_at_call_time`` because the image is produced inside the call's own
    directory, whose name is allocated per call.  It still declares that this workload lands
    *something*, which is what keeps the attach-time refusals — no sink, or no ``FILES_OUT`` in
    ``requires`` — doing their job.  The declaration itself is built in the tool body.
    """
    return SandboxSpec(
        kind=DIAGRAM_KIND,
        image=image,
        egress_allow=(),
        work_dir=_WORK_DIR,
        requires=frozenset({Capability.EXEC, Capability.FILES_IN, Capability.FILES_OUT}),
        outputs_named_at_call_time=True,
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
        # No integrity declaration. `dot`'s failure diagnostic quotes the model's own DOT
        # source, and which of the reference and the diagnostic comes back is decided by
        # that source — a presence bit — so nothing about the result is first-party. `dot`
        # is an unlabelled argument, so the framework's input-label join answers untrusted.
        source_integrity=None,
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

    # No lock, and that is where the files go rather than luck: the calls in one assistant
    # message run concurrently (`Sandbox.acquire` documents it), and under `guest_call_path()`
    # two renders share no path to collide on.

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

        # This call's own directory. Asking for it is what puts it on the framework's list:
        # the path, and everything under it, is removed when the body returns.
        guest_call_directory = session.guest_call_path()
        # What `collect_outputs` resolves is relative to `work_dir`, and the call directory sits
        # directly under it, so its last component is the whole of the prefix.
        call_id = guest_call_directory.rsplit("/", 1)[-1]
        guest_source_path = f"{guest_call_directory}/{_SOURCE_FILENAME}"
        guest_output_path = f"{guest_call_directory}/{_OUTPUT_FILENAME}"

        try:
            await sandbox.write_file(
                guest_source_path, dot, working_directory=session.spec.work_dir
            )
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
                [_RENDERER, _FORMAT_FLAG, guest_source_path, _OUTPUT_FLAG, guest_output_path],
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
            landed = await collect_outputs(
                sandbox,
                session.spec,
                sink=sink,
                # Declared here because the path carries this call's own id. `name` keeps
                # that id out of host storage and out of what the model is shown, at the cost
                # of a landed name every call shares — see the README.
                outputs=(
                    DeclaredOutput(
                        path=f"{call_id}/{_OUTPUT_FILENAME}",
                        media_type=_OUTPUT_MEDIA_TYPE,
                        required=False,
                        name=_OUTPUT_FILENAME,
                    ),
                ),
            )
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
