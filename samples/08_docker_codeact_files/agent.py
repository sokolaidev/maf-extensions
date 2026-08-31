"""One turn of an agent that reads a file it was given and writes one back.

Sample 06 with both of CodeAct's file channels wired::

    app  ->  maf_sandbox (router)  ->  maf_sandbox_docker  ->  the container
                  ^ maf_sandbox_codeact calls the router

Samples 03, 04 and 06 give `execute_code` nothing to read and take only stdout
back.  This one passes a `file_store`, which grows the tool a `files`
parameter, and an `output_sink` with `CodeactOutputs.DECLARED`, which grows it an
`outputs` parameter.  The task needs both, so a run that skips either is visibly
wrong rather than quietly thinner.

This directory's README is the walkthrough — what to watch for, and where the
sink should point, which is the security-relevant decision here.  Read it first.
"""

# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "agent-framework-openai",
#     "azure-core[aio]",
#     "azure-identity",
#     "maf-sandbox-codeact",
#     "maf-sandbox-docker",
#     "maf-sandbox>=0.28",
# ]
# ///

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from _scaffold import MEASURED, installed_versions, quoted, require_env_vars
from agent_framework import Agent, InMemoryAgentFileStore
from agent_framework.openai import OpenAIChatClient
from azure.identity.aio import DefaultAzureCredential
from maf_sandbox import (
    Artifact,
    Isolation,
    LandedArtifact,
    OutputSink,
    SandboxRouter,
    make_file_system_sink,
)
from maf_sandbox.maf import list_all_files, make_caller_context
from maf_sandbox_codeact import CodeactOutputs, make_codeact_tools
from maf_sandbox_docker import DockerSandboxBackend, DockerSandboxConfig

# Keyed by (scope, thread_id, agent_dir); constants here since this program serves one request.
SCOPE = "samples"
THREAD_ID = "08-docker-codeact-files"
AGENT_DIR = "data-analyst"

#: A standard MCR devcontainer image at Python 3.13 — sample 06's, so nothing is built here.
CODEACT_IMAGE = "mcr.microsoft.com/devcontainers/python:3.13-bookworm"

#: Ships beside this file and is seeded into the agent's file store under the same name.
STORE_FILE = "sales.csv"

#: Where landed artifacts go. Beside the sample, and deliberately *not* the file store.
OUTPUT_DIR = Path(__file__).parent / "out"

TASK = (
    "The file sales.csv is in your file store. Using a Python program, compute each "
    "row's revenue as units * unit_price, total it by region, and also compute the "
    "grand total across all regions. Print the grand total as a single integer on "
    "its own line, and save the per-region totals as summary.md — a Markdown table "
    "with the region in the first column and its revenue in the second. Tell me the "
    "grand total and where the summary was saved."
)

#: The Docker backend reads no environment — it drives the local `docker` client — so the only
#: variables are the model's. Auth is `DefaultAzureCredential`, so there is no key.
MODEL_VARS = ("AZURE_OPENAI_ENDPOINT", "AZURE_OPENAI_CHAT_MODEL")


def make_recording_sink(output_dir: Path, delivered: list[str]) -> OutputSink:
    """`make_file_system_sink`, with this turn's names recorded as they land.

    The writing and the confinement are the library's — a sink that joins a validated name
    onto a directory is still not safe, because the name says nothing about what is already
    at the path it resolves to.  What is left here is the only part that belongs to the
    application: `delivered` is *this turn's* record, and the directory cannot stand in for
    it because it also holds whatever an earlier run left behind.
    """
    landing = make_file_system_sink(
        output_dir,
        # No leading verb: the kind introduces this list with "Saved:" of its own.
        display=lambda artifact, _destination: (
            f"{artifact.name} ({len(artifact.content)} bytes), in {output_dir.name}/"
        ),
    )

    async def deliver(artifact: Artifact) -> LandedArtifact:
        landed = await landing.deliver(artifact)
        delivered.append(landed.name)
        return landed

    return OutputSink(deliver)


async def run() -> int:
    """Wire the stack, run one turn, and take the container down again."""
    env = require_env_vars(MODEL_VARS)
    if env is None:
        return 2

    backend = DockerSandboxBackend(DockerSandboxConfig())

    # Below the router's default `microvm` floor; opted down explicitly, as sample 06 does.
    # Worth re-reading that decision here rather than copying it: with a store wired, the
    # program's input is no longer only source the model wrote — it is also whatever those
    # files contain. The floor should be chosen against the provenance of the file store.
    # This sample's file store holds one CSV that ships in this repository.
    router = SandboxRouter([backend], min_isolation=Isolation.CONTAINER)

    # The agent's file store. A real host's is backed by a disk or a blob container and
    # already holds what the agent wrote earlier; here the sample seeds the one file.
    store = InMemoryAgentFileStore()
    # Explicit encoding: the guest side of this channel is UTF-8 whatever the host's locale is,
    # and on Windows the default here is not. `sales.csv` is ASCII, so this only bites the
    # reader who edits it — on one platform, which is the worst way to find out.
    seed = (Path(__file__).parent / STORE_FILE).read_text(encoding="utf-8")
    await store.write(STORE_FILE, seed)

    context = make_caller_context(list_all_files, lambda: SCOPE, lambda: THREAD_ID)

    # This turn's deliveries, recorded by the sink as they arrive. Not the same as the
    # contents of `out/`, which also holds whatever an earlier run left there.
    delivered: list[str] = []

    tools = make_codeact_tools(
        router,
        AGENT_DIR,
        context,
        # Files in: the tool grows a `files` parameter, bounded by `list_all_files` above.
        file_store=store,
        # Files out: a sink and a naming road. `DECLARED` makes the model say what its
        # program will write before it runs, which is the road that can report a name
        # declared and never written — the diagnostic `MANIFEST` cannot have.
        output_sink=make_recording_sink(OUTPUT_DIR, delivered),
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
        # Quoted, because the reply and the two measured lines below share one stream and the
        # live check trusts the `[measured]` tag completely (#314).
        print(quoted(response.text))
    finally:
        # Deletes rather than relying on the container living on — see sample 01's README.
        purge = await router.dispose_scope(SCOPE, THREAD_ID)
        print(f"\n{MEASURED}Disposed {purge.disposed} sandbox(es).")
        if purge.undisposed is not None:
            print(f"{MEASURED}Not fully disposed: {purge.undisposed}")
        await credential.close()

    # JSON, not a comma-joined sentence: an artifact name may legally contain a comma, so
    # one delivery called `notes, summary.md` would read back as two — one of them the name
    # this run is checked for.
    print(f"{MEASURED}Delivered this turn into {OUTPUT_DIR.name}/: {json.dumps(delivered)}")

    return 0


if __name__ == "__main__":
    print(installed_versions())
    raise SystemExit(asyncio.run(run()))
