"""One turn of an agent that reads a file it was given and writes one back.

Sample 06 with both of CodeAct's file channels wired::

    app  ->  maf_sandbox (router)  ->  maf_sandbox_docker  ->  the container
                  ^ maf_sandbox_codeact calls the router

Samples 03, 04 and 06 give `execute_code` nothing to read and take only stdout
back.  This one passes a `workspace_store`, which grows the tool a `files`
parameter, and an `output_sink` with `CodeactOutputs.DECLARED`, which grows it an
`outputs` parameter.  The task cannot be answered without the first and cannot be
delivered without the second, so a run that skips either is visibly wrong rather
than quietly thinner.

Two things worth watching, because they are the parts a fake backend cannot show:

- The program opens `sales.csv` by that name.  Each call gets a fresh directory
  inside the sandbox and the shared file is written into it, so a bare relative
  name is what the program uses — no run id, no absolute path.
- The summary lands as `summary.md`, not `<run-id>/summary.md`.  The guest path
  and the delivered name are separate fields, and this is the difference showing
  up on disk.

Sample 07 also lands an artifact, and does it by defining a kind inline against
the protocol.  This one changes no workload code at all: the same packaged
`execute_code` a host already has, with two constructor arguments it did not pass
before.  Read this directory's README first — particularly on where the sink
points, which is the security-relevant decision here.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Any

from agent_framework import Agent, InMemoryAgentFileStore
from agent_framework.openai import OpenAIChatClient
from azure.identity.aio import DefaultAzureCredential
from maf_sandbox import Artifact, Isolation, LandedArtifact, OutputSink, SandboxRouter
from maf_sandbox.maf import make_workspace_context
from maf_sandbox_codeact import CodeactOutputs, make_codeact_tools
from maf_sandbox_docker import DockerSandboxBackend, DockerSandboxConfig

# Keyed by (scope, thread_id, agent_dir); constants here since this program serves one request.
SCOPE = "samples"
THREAD_ID = "08-docker-codeact-files"
AGENT_DIR = "data-analyst"

#: A standard MCR devcontainer image at Python 3.13 — sample 06's, so nothing is built here.
CODEACT_IMAGE = "mcr.microsoft.com/devcontainers/python:3.13-bookworm"

#: Ships beside this file and is seeded into the agent's workspace under the same name.
WORKSPACE_FILE = "sales.csv"

#: Where landed artifacts go. Beside the sample, and deliberately *not* the workspace store.
OUTPUT_DIR = Path(__file__).parent / "out"

TASK = (
    "The file sales.csv is in your workspace. Using a Python program, compute each "
    "row's revenue as units * unit_price, total it by region, and also compute the "
    "grand total across all regions. Print the grand total as a single integer on "
    "its own line, and save a Markdown table of the per-region totals as summary.md. "
    "Tell me the grand total and where the summary was saved."
)

#: The Docker backend reads no environment — it drives the local `docker` client — so the only
#: variables are the model's. Auth is `DefaultAzureCredential`, so there is no key.
MODEL_VARS = ("AZURE_OPENAI_ENDPOINT", "AZURE_OPENAI_CHAT_MODEL")


def require_environment(names: tuple[str, ...]) -> dict[str, str] | None:
    """Read `names` from the environment, or report every one that is missing.

    Worth failing on rather than warning about, for the reason sample 06 gives: an
    unconfigured router produces an agent with no tools, which answers from the
    model alone and looks like success.
    """
    missing = [name for name in names if not os.environ.get(name)]
    if missing:
        print("Not configured. These environment variables are unset:", file=sys.stderr)
        for name in missing:
            print(f"  {name}", file=sys.stderr)
        print("\nSee this directory's README.md.", file=sys.stderr)
        return None
    return {name: os.environ[name] for name in names}


async def list_workspace(store: Any) -> list[str]:
    """Every file in the workspace store, as workspace-relative paths.

    This listing is the **authority** for what `files` may name: the kind shares a
    file only if it appears here, so a name the model invented — or read out of a
    file it was given — resolves to nothing.  `list_children` returns entries one
    level at a time, so walking the tree is the host's job rather than the store's
    — sample 01's walker, unchanged, because the boundary is the same one.
    """
    paths: list[str] = []

    async def walk(directory: str) -> None:
        for entry in await store.list_children(directory):
            child = f"{directory}/{entry.name}" if directory else entry.name
            if entry.type == "directory":
                await walk(child)
            else:
                paths.append(child)

    await walk("")
    return paths


def make_markdown_sink(output_dir: Path) -> OutputSink:
    """A sink that lands each produced file under ``output_dir``.

    ``display`` is the one line the model is allowed to see; ``handle`` is the host's
    own path and nothing renders it into a transcript.  ``artifact.name`` is validated
    relative before it arrives — no traversal, not absolute — so joining it under
    ``output_dir`` stays under ``output_dir``.

    ``artifact.media_type`` is always ``None`` for this kind and there is nothing to fix
    about that: the bytes came from a program the model wrote, so a type read out of the
    guest would be the sandbox telling the host how to handle its own content.  A host
    that wants to decide by extension has ``artifact.name`` and its own policy.
    """

    async def deliver(artifact: Artifact) -> LandedArtifact:
        output_dir.mkdir(parents=True, exist_ok=True)
        destination = output_dir / artifact.name
        destination.write_bytes(artifact.content)
        return LandedArtifact(
            name=artifact.name,
            # No leading verb: the kind introduces this list with "Saved:" of its own, and
            # two of them read as a stutter in the transcript.
            display=f"{artifact.name} ({len(artifact.content)} bytes), in {output_dir.name}/",
            handle=str(destination),
        )

    return OutputSink(deliver)


async def run() -> int:
    """Wire the stack, run one turn, and take the container down again."""
    env = require_environment(MODEL_VARS)
    if env is None:
        return 2

    backend = DockerSandboxBackend(DockerSandboxConfig())

    # Below the router's default `microvm` floor; opted down explicitly, as sample 06 does.
    # Worth re-reading that decision here rather than copying it: with a store wired, the
    # program's input is no longer only source the model wrote — it is also whatever those
    # files contain. The floor should be chosen against the provenance of the workspace.
    # This sample's workspace holds one CSV that ships in this repository.
    router = SandboxRouter([backend], min_isolation=Isolation.CONTAINER)

    # The agent's workspace. A real host's is backed by a disk or a blob container and
    # already holds what the agent wrote earlier; here the sample seeds the one file.
    store = InMemoryAgentFileStore()
    await store.write(
        WORKSPACE_FILE, (Path(__file__).parent / WORKSPACE_FILE).read_text()
    )

    context = make_workspace_context(list_workspace, lambda: SCOPE, lambda: THREAD_ID)

    tools = make_codeact_tools(
        router,
        AGENT_DIR,
        context,
        # Files in: the tool grows a `files` parameter, bounded by `list_workspace` above.
        workspace_store=store,
        # Files out: a sink and a naming road. `DECLARED` makes the model say what its
        # program will write before it runs, which is the road that can report a name
        # declared and never written — the diagnostic `MANIFEST` cannot have.
        output_sink=make_markdown_sink(OUTPUT_DIR),
        outputs=CodeactOutputs.DECLARED,
        image=CODEACT_IMAGE,
    )
    if not tools:
        # Unreachable given the checks above; printed because the `[]` contract is worth stating.
        print("No sandbox backend: execute_code was not attached.", file=sys.stderr)
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
                "You answer questions about data by writing and running Python with the "
                "execute_code tool, never by reading numbers out of a file yourself. Pass "
                "every file your program opens in the tool's files parameter, and declare "
                "every file it writes in the outputs parameter — a file you do not declare "
                "is not saved. Report exactly what the program printed."
            ),
            tools=tools,
        )
        response = await agent.run(TASK)
        print(response.text)
    finally:
        # Deletes rather than relying on the container living on — see sample 01's README.
        deleted = await router.dispose_scope(SCOPE, THREAD_ID)
        print(f"\nDisposed {deleted} sandbox(es).")
        await credential.close()

    # What actually landed, printed from the host's side of the sink. The model was told a
    # sentence; this is the file. A turn that answers correctly and writes nothing is the
    # failure this sample exists to make visible, so it is worth looking at separately.
    landed = (
        sorted(path.name for path in OUTPUT_DIR.glob("*"))
        if OUTPUT_DIR.is_dir()
        else []
    )
    print(f"Landed in {OUTPUT_DIR.name}/: {', '.join(landed) if landed else 'nothing'}")

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
