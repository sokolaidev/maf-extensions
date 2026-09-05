"""One turn of an agent that cannot read its program's output, and reads its file instead.

Sample 08 with the guest's text withheld and the outputs landed where the model can go
and get them::

    app  ->  maf_sandbox (router)  ->  maf_sandbox_docker  ->  the container
                  ^ maf_sandbox_codeact calls the router

`withhold_guest_output=True` keeps everything the program printed out of the result,
so `stdout` is a byte count and nothing else.  What replaces it is a **second store**:
`make_file_store_sink` lands each call's declared outputs under a folder named for that
call, `sandbox_outputs_read_tools` gives the model a read-only pair of tools over that
store, and the withheld result names the folder.  The model writes a file, is told
where it went, and goes and reads it.

The answer in the reply is therefore evidence of the whole path.  Nothing the program
printed comes back, so a grand total in the reply came out of a file the program wrote,
landed in the sink, and read back through a host tool.

This directory's README is the walkthrough — above all why there are two stores and why
the read-back tools are not a second `FileAccessProvider`.  Read it first.
"""

# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "agent-framework-openai",
#     "azure-core[aio]",
#     "azure-identity",
#     "maf-sandbox-codeact",
#     "maf-sandbox-docker",
#     "maf-sandbox>=0.33",
# ]
# ///

from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import replace
from pathlib import Path

from _scaffold import MEASURED, evidence, installed_versions, quoted, require_env_vars, tool_results
from agent_framework import Agent, InMemoryAgentFileStore
from agent_framework.openai import OpenAIChatClient
from azure.identity.aio import DefaultAzureCredential
from maf_sandbox import Artifact, FileStoreProvenance, Isolation, LandedArtifact, SandboxRouter
from maf_sandbox.maf import (
    file_store_provenance_middleware,
    list_all_files,
    make_caller_context,
    make_file_store_sink,
    sandbox_outputs_read_tools,
)
from maf_sandbox_codeact import CodeactOutputs, make_codeact_tools
from maf_sandbox_docker import DockerSandboxBackend, DockerSandboxConfig

SCOPE = "samples"
THREAD_ID = "16-docker-codeact-outputs-store"
AGENT_DIR = "data-analyst"

#: A standard MCR devcontainer image at Python 3.13 — sample 06's, so nothing is built here.
CODEACT_IMAGE = "mcr.microsoft.com/devcontainers/python:3.13-bookworm"

#: Ships beside this file and is seeded into the *working* store under the same name.
STORE_FILE = "sales.csv"

#: What the model is told to write, and the only name it should declare.
SUMMARY_FILE = "summary.md"

#: The tool names the read-back pair carries here. The default prefix, spelled out because the
#: README argues about it and a reader should be able to grep for what the model actually saw.
OUTPUTS_TOOL_PREFIX = "sandbox_outputs"

TASK = (
    "The file sales.csv is in your file store. Using a Python program, compute each "
    "row's revenue as units * unit_price, total it by region, and also compute the "
    "grand total across all regions. Write both into summary.md — a Markdown table "
    "with the region in the first column and its revenue in the second, and a final "
    "line reading 'grand total: N'. Then tell me the grand total."
)

MODEL_VARS = ("AZURE_OPENAI_ENDPOINT", "AZURE_OPENAI_CHAT_MODEL")


def make_recording_sink(store: object, record: FileStoreProvenance, landed: list[str]):
    """`make_file_store_sink`, with this turn's destinations recorded as they land.

    The folder, the refusal of an existing destination and the provenance entry are the
    library's.  What is left here is the application's: `landed` is *this turn's* record, and
    listing the store afterwards cannot stand in for it, because the store also holds whatever
    an earlier turn put there.
    """
    landing = make_file_store_sink(store, provenance=record)

    async def deliver(artifact: Artifact) -> LandedArtifact:
        delivered = await landing.deliver(artifact)
        landed.append(delivered.handle or delivered.name)
        return delivered

    # `replace` rather than a fresh `OutputSink`: `per_call` is the sink's claim about its
    # layout and the kind reads it, so a wrapper that rebuilt the object by hand would take the
    # folder back out of the result the first time a field was forgotten.
    return replace(landing, deliver=deliver)


async def run() -> int:
    """Wire the two stores, run one turn, and take the container down again."""
    env = require_env_vars(MODEL_VARS)
    if env is None:
        return 2

    backend = DockerSandboxBackend(DockerSandboxConfig())
    router = SandboxRouter([backend], min_isolation=Isolation.CONTAINER)

    # The **working** store: what the program is given. Nothing model-facing is wired over it,
    # so the only road into the sandbox is `execute_code`'s own `files` parameter.
    working = InMemoryAgentFileStore()
    seed = (Path(__file__).parent / STORE_FILE).read_text(encoding="utf-8")
    await working.write(STORE_FILE, seed)

    # The **outputs** store: what the program produced. The model reads it and cannot write it.
    outputs = InMemoryAgentFileStore()

    # One record per store is the rule — a path is the whole key, so a single record shared
    # between the two would answer about a file it never saw. This one is the outputs store's,
    # and every landing is entered into it before the bytes are written, so nothing a guest
    # produced can ever read back as host-placed.
    landed_provenance = FileStoreProvenance()

    context = make_caller_context(list_all_files, lambda: SCOPE, lambda: THREAD_ID)

    landed: list[str] = []
    tools = list(
        make_codeact_tools(
            router,
            AGENT_DIR,
            context,
            file_store=working,
            output_sink=make_recording_sink(outputs, landed_provenance, landed),
            outputs=CodeactOutputs.DECLARED,
            # The whole point: nothing the program prints comes back, so the answer in the
            # reply cannot have come from `stdout`.
            withhold_guest_output=True,
            image=CODEACT_IMAGE,
        )
    )
    if not tools:
        print("No sandbox backend: execute_code was not attached.", file=sys.stderr)
        return 2

    # The other half. Read-only by construction — there is no write here to disable — and named
    # apart from the framework's `file_access_*` tools, which is not a style choice: see README.
    tools += sandbox_outputs_read_tools(outputs, name_prefix=OUTPUTS_TOOL_PREFIX)

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
                "every file it writes in the outputs parameter. The program's printed output "
                "does not come back to you: to see what it produced, read the file back with "
                f"the {OUTPUTS_TOOL_PREFIX}_read tool."
            ),
            tools=tools,
            # Records the model's own file-store writes. Nothing here wires a write tool, so it
            # observes nothing today — it is what keeps that true rather than incidental.
            middleware=[file_store_provenance_middleware(landed_provenance)],
        )
        response = await agent.run(TASK)
        print(quoted(response.text))
        # The read-backs themselves, fenced. A model can claim it read the file; it cannot put
        # the file's own text here without the tool having returned it.
        print()
        print(
            evidence(
                "read back out of the outputs store",
                tool_results(response, f"{OUTPUTS_TOOL_PREFIX}_read"),
                "Read-backs the model made",
            )
        )
    finally:
        purge = await router.dispose_scope(SCOPE, THREAD_ID)
        print(f"\n{MEASURED}Disposed {purge.disposed} sandbox(es).")
        if purge.undisposed is not None:
            print(f"{MEASURED}Not fully disposed: {purge.undisposed}")
        await credential.close()

    # JSON for the reason sample 08 gives: an artifact name may legally contain a comma.
    print(f"{MEASURED}Landed this turn in the outputs store: {json.dumps(landed)}")

    return 0


if __name__ == "__main__":
    print(installed_versions())
    raise SystemExit(asyncio.run(run()))
