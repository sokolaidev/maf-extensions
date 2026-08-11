"""One turn of an agent that renders a diagram instead of describing one.

The first sample that reads a file **back out** of the sandbox.  Samples 05 and 06
write into a container and read its stdout; this one writes a DOT source in, runs a
renderer, and pulls the resulting PNG out through ``FILES_OUT`` — the pull surface
the Docker backend added.  The image never enters the transcript: the model gets a
reference to where it landed, and the bytes go to host state through a sink::

    app  ->  maf_sandbox (router)  ->  maf_sandbox_docker  ->  the container
                  ^ this file's `render_diagram` calls the router,
                    then `collect_outputs(...)` lands the PNG in `out/`.

**The workload is defined here, in the sample, not in a package** — and that is the
point of it.  Everything it needs is public in ``maf_sandbox``: the spec, the sink,
``sandboxed_tool`` and ``collect_outputs``.  So this file is what a third party
writing their own sandbox kind against the published protocol would write, with
nothing reached from inside the library.  Samples 05 and 06 lean on a packaged kind
(``maf_sandbox_bicep``, ``maf_sandbox_codeact``); this one shows the layer beneath
them.

Nothing here needs Azure.  Any machine with a Docker-compatible engine runs the
sandbox, and the model is any OpenAI-compatible endpoint, a local server included.
The boundary is a container, the egress is closed, and the guest image carries a
renderer and nothing else — this directory's README says what each of those costs.
Read it, along with the prerequisites and the environment variables, first.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

from agent_framework import Agent
from agent_framework.openai import OpenAIChatCompletionClient
from maf_sandbox import (
    Artifact,
    Capability,
    DeclaredOutput,
    Isolation,
    LandedArtifact,
    OutputSink,
    SandboxRouter,
    SandboxSpec,
    TransferLimits,
    WorkspaceContext,
    collect_outputs,
    error_detail,
)
from maf_sandbox.maf import SandboxToolSession, make_workspace_context, sandboxed_tool
from maf_sandbox_docker import DockerSandboxBackend, DockerSandboxConfig

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)

# A sandbox is keyed by (scope, thread_id, agent_dir). A host reads the first two from its own
# request context; this program serves one request, so they are constants — named, not inlined,
# because they belong to the request rather than to the agent.
SCOPE = "samples"
THREAD_ID = "07-docker-diagram"
AGENT_DIR = "diagram-designer"

TASK = (
    "Draw a directed graph of a three-stage data pipeline — an 'ingest' node that "
    "flows to a 'transform' node that flows to a 'load' node — and render it to an "
    "image. Then tell me where the image was saved."
)

# Where the landed PNG goes. Under the sample dir so it is easy to find; git-ignored so a run
# does not leave a tracked file behind.
OUTPUT_DIR = Path(__file__).parent / "out"

#: The image is a local reference (for example `diagram-sandbox:local`); there is no registry
#: to qualify it, because the backend runs what is already on this machine.
SANDBOX_VARS = ("DIAGRAM_SANDBOX_IMAGE",)

#: Everything the chat model needs. `OPENAI_BASE_URL` is optional, so it is read separately:
#: unset, the client talks to OpenAI.
MODEL_VARS = ("OPENAI_API_KEY", "OPENAI_CHAT_MODEL")


# --- The diagram-generator kind, written against the published protocol alone ----------------
#
# Nothing below imports a packaged workload. It is the whole of what a new kind is: a spec that
# says what sandbox it needs, a tool body that drives `write_file`/`exec`/`collect_outputs`, and
# a factory that hands both to `sandboxed_tool`.

#: The sandbox kind this workload asks for.
DIAGRAM_KIND = "diagram-generator"

RENDER_DIAGRAM_TOOL_NAME = "render_diagram"

#: Where the source is written and the image is produced — a dedicated root, not the image's own
#: tree.
_WORK_DIR = "/work"

_RENDERER = "dot"
#: The output format, single-sourced: it is `dot`'s `-T` value and the image's file extension.
_OUTPUT_FORMAT = "png"
_FORMAT_FLAG = f"-T{_OUTPUT_FORMAT}"
_OUTPUT_FLAG = "-o"
#: One fixed name each, rewritten on every call, since the sandbox is reused across calls.
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
        requires=frozenset(
            {Capability.EXEC, Capability.FILES_IN, Capability.FILES_OUT}
        ),
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
    context: WorkspaceContext,
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
        # `source_integrity` stays at its "trusted" default: this tool's result is a
        # host-authored reference string (the sink's `display`), never guest-produced content.
        output_sink=sink,
        logger=logger,
    )


def _render_diagram_tool(
    session: SandboxToolSession,
    sink: OutputSink,
    timeout: int,
) -> "Callable[..., Awaitable[str]]":
    """Build the ``render_diagram`` body for one attached tool.

    Defined at module level rather than nested inside :func:`make_diagram_tools`, and that is
    not a style choice: the function below's **docstring is the tool's description** — MAF passes
    ``__doc__`` through verbatim, indentation and all — so nesting this one level deeper would
    re-indent every line of what the model reads.
    """

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
        try:
            await sandbox.write_file(source_path, dot)
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
            # dot rejects malformed DOT with a diagnostic on stderr. That is the model's to fix,
            # not a transport failure — hand it back so the next attempt can correct the source.
            # The declared output is required=False, so its absence here is not a transfer error.
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
    return (
        f"dot could not render the diagram (exit {exit_code}) and wrote no diagnostic."
    )


def make_png_sink(output_dir: Path) -> OutputSink:
    """A sink that lands each rendered image as a file under ``output_dir``.

    ``deliver`` writes the bytes and reports back: ``display`` is the one line the model is
    allowed to see — where the image landed, and nothing guest-derived — and ``handle`` is the
    host's own path, which nothing renders into the transcript.  ``artifact.name`` is validated
    relative before it reaches here (no traversal, not absolute), so joining it under
    ``output_dir`` stays inside ``output_dir``.
    """

    async def deliver(artifact: Artifact) -> LandedArtifact:
        output_dir.mkdir(parents=True, exist_ok=True)
        destination = output_dir / artifact.name
        destination.write_bytes(artifact.content)
        return LandedArtifact(
            name=artifact.name,
            display=f"Rendered {artifact.name} ({artifact.media_type}); saved under "
            f"{output_dir.name}/.",
            handle=str(destination),
        )

    return OutputSink(deliver)


# --- Host wiring -----------------------------------------------------------------------------


def require_environment(names: tuple[str, ...]) -> dict[str, str] | None:
    """Read `names` from the environment, or report every one that is missing.

    Worth doing before anything else, and worth failing on.  `make_diagram_tools` returns an
    empty list when the router has no backend, so a half-configured run does not crash — it
    quietly produces an agent with no tools, which answers from the model alone.  That is the T0
    behaviour this sample exists to contrast with, and it is indistinguishable from success
    unless someone says so out loud.
    """
    missing = [name for name in names if not os.environ.get(name)]
    if missing:
        print("Not configured. These environment variables are unset:", file=sys.stderr)
        for name in missing:
            print(f"  {name}", file=sys.stderr)
        print("\nSee this directory's README.md.", file=sys.stderr)
        return None
    return {name: os.environ[name] for name in names}


async def no_workspace_files(_store: object) -> list[str]:
    """This kind shares no workspace files — the model supplies the DOT source directly."""
    return []


async def run() -> int:
    """Wire the stack, run one turn, and take the container down again."""
    env = require_environment(SANDBOX_VARS + MODEL_VARS)
    if env is None:
        return 2

    backend = DockerSandboxBackend(DockerSandboxConfig())

    # Below the router's default `microvm` floor; opted down explicitly.
    router = SandboxRouter([backend], min_isolation=Isolation.CONTAINER)

    context = make_workspace_context(
        no_workspace_files,
        lambda: SCOPE,
        lambda: THREAD_ID,
    )
    sink = make_png_sink(OUTPUT_DIR)

    tools = make_diagram_tools(
        router,
        AGENT_DIR,
        context,
        sink,
        image=env["DIAGRAM_SANDBOX_IMAGE"],
    )
    if not tools:
        print("No sandbox backend: render_diagram was not attached.", file=sys.stderr)
        return 2

    try:
        agent = Agent(
            client=OpenAIChatCompletionClient(
                model=env["OPENAI_CHAT_MODEL"],
                api_key=env["OPENAI_API_KEY"],
                base_url=os.environ.get("OPENAI_BASE_URL"),
            ),
            name=AGENT_DIR,
            instructions=(
                "You draw diagrams by writing Graphviz DOT and calling the "
                "render_diagram tool — never by describing the picture in prose. "
                "Call the tool with complete DOT source, then report exactly where "
                "it saved the image. Do not claim to have seen the image itself."
            ),
            tools=tools,
        )
        response = await agent.run(TASK)
        print(response.text)
    finally:
        deleted = await router.dispose_scope(SCOPE, THREAD_ID)
        print(f"\nDisposed {deleted} sandbox(es).")

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
