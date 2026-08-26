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
sandbox, and the model is Azure OpenAI reached with `DefaultAzureCredential` — no
API key in this program, the same wiring samples 01, 03, 05, 06 and 08 use.
The boundary is a container, the egress is closed, and the guest image carries a
renderer and nothing else — this directory's README says what each of those costs.
Read it, along with the prerequisites and the environment variables, first.
"""

# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "agent-framework-openai",
#     "azure-core[aio]",
#     "azure-identity",
#     "maf-sandbox-docker",
#     "maf-sandbox>=0.25",
# ]
# ///

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from _scaffold import MEASURED, installed_versions, quoted, require_env_vars
from agent_framework import Agent
from agent_framework.openai import OpenAIChatClient
from azure.identity.aio import DefaultAzureCredential
from diagram_kind import diagram_sandbox_spec, make_diagram_tools
from maf_sandbox import (
    Isolation,
    SandboxKey,
    SandboxRouter,
    error_detail,
    make_file_system_sink,
)
from maf_sandbox.maf import (
    list_no_files,
    make_caller_context,
)
from maf_sandbox_docker import DockerSandboxBackend, DockerSandboxConfig

if TYPE_CHECKING:
    from maf_sandbox import SandboxSpec

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

#: The image is a local reference (for example `diagram-sandbox:local`); the sample builds it and
#: the backend runs what is on this machine. See the README on why an unqualified tag is safe here.
SANDBOX_VARS = ("DIAGRAM_SANDBOX_IMAGE",)

#: Everything the chat model needs. No key: auth is `DefaultAzureCredential`, which an
#: `az login` session or a federated CI credential satisfies.
MODEL_VARS = ("AZURE_OPENAI_ENDPOINT", "AZURE_OPENAI_CHAT_MODEL")


#: Bounds the one command this program runs itself. Nothing to do with the render, which the
#: kind bounds with its own.
PROBE_TIMEOUT_SECONDS = 30


# --- Host wiring -----------------------------------------------------------------------------


async def left_in_the_sandbox(router: SandboxRouter, spec: SandboxSpec, key: SandboxKey) -> str:
    """Whatever is still in the sandbox's working directory now the turn is over.

    Host-side instrumentation rather than part of the kind: it reads the guest to grade a claim
    the library makes about it.  ``render_diagram`` writes its DOT source and its PNG inside the
    call's own directory, so a healthy turn leaves the working directory **empty** — the
    framework removed that directory, and everything under it, when the tool call returned.

    Read over ``exec``, because the docker backend serves no ``FILES_LIST``; sample 15's act 5
    asks the same question of a backend that does.  ``acquire`` is get-or-create, so this reuses
    the sandbox the turn left warm rather than creating one — which is why ``run`` asks only
    once something has landed, and never on a turn that called no tool.
    """
    sandbox = await router.acquire(key, spec)
    listing = await sandbox.exec(
        ["ls", "-A", spec.work_dir],
        working_directory=spec.work_dir,
        timeout=PROBE_TIMEOUT_SECONDS,
    )
    return ", ".join(listing.stdout.split())


async def run() -> int:
    """Wire the stack, run one turn, and take the container down again."""
    env = require_env_vars(SANDBOX_VARS + MODEL_VARS)
    if env is None:
        return 2

    backend = DockerSandboxBackend(DockerSandboxConfig())

    # Below the router's default `microvm` floor; opted down explicitly.
    router = SandboxRouter([backend], min_isolation=Isolation.CONTAINER)

    context = make_caller_context(
        list_no_files,
        lambda: SCOPE,
        lambda: THREAD_ID,
    )
    # The library writes and confines; the sample supplies only the sentence the model sees,
    # which this kind phrases with a verb because nothing introduces the line for it.
    sink = make_file_system_sink(
        OUTPUT_DIR,
        display=lambda artifact, _destination: (
            f"Rendered {artifact.name} ({artifact.media_type}); saved under {OUTPUT_DIR.name}/."
        ),
    )

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

    credential = DefaultAzureCredential()
    try:
        agent = Agent(
            client=OpenAIChatClient(
                model=env["AZURE_OPENAI_CHAT_MODEL"],
                azure_endpoint=env["AZURE_OPENAI_ENDPOINT"],
                credential=credential,
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
        # Quoted, because the reply and the measured line below share one stream and the live
        # check trusts the `[measured]` tag completely (#314).
        print(quoted(response.text))
    finally:
        # Asked only once something has landed, because that means the tool ran and left its
        # sandbox warm. Asking unconditionally would *create* one, and the disposal count below
        # is this sample's evidence that a tool call — not this line — is what made a sandbox
        # exist. Contained, because the disposal after it has to happen either way.
        if OUTPUT_DIR.is_dir() and any(OUTPUT_DIR.iterdir()):
            try:
                surviving = await left_in_the_sandbox(
                    router,
                    diagram_sandbox_spec(env["DIAGRAM_SANDBOX_IMAGE"]),
                    SandboxKey(scope=SCOPE, thread_id=THREAD_ID, agent_dir=AGENT_DIR),
                )
                print(f"\n{MEASURED}Left in the sandbox work directory: {surviving or 'nothing'}")
            except Exception as exc:  # noqa: BLE001 — a probe must not cost the disposal
                print(f"\n{MEASURED}Could not read the sandbox after the call: {error_detail(exc)}")
        purge = await router.dispose_scope(SCOPE, THREAD_ID)
        print(f"\n{MEASURED}Disposed {purge.disposed} sandbox(es).")
        if purge.undisposed is not None:
            print(f"{MEASURED}Not fully disposed: {purge.undisposed}")
        await credential.close()

    return 0


if __name__ == "__main__":
    print(installed_versions())
    raise SystemExit(asyncio.run(run()))
