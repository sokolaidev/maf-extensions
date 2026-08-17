"""One turn of an agent that reads a file it was given and writes one back — remotely.

Sample 08's task, its data and its wiring, with one line changed::

    app  ->  maf_sandbox (router)  ->  maf_sandbox_acas  ->  the sandbox
                  ^ maf_sandbox_codeact calls the router

Everything that pulls a file back out of a sandbox has been Docker until now — samples
07 and 08 — and Docker is the local backend, where a file leaving the guest is a
`docker cp` on the same machine.  Here the same file crosses a preview control plane:
`FILES_OUT` on this backend is a `stat_file` and a `read_file` over HTTPS, against a
service whose SDK has twice reported something the payload did not carry (#139, #142).
That is the whole reason this sample exists (#300) — the pull surface is portable in the
way the design says, or it is not, and only a second backend can say which.

Read `samples/08_docker_codeact_files/README.md` first: it explains both channels, what
the sink is for and where it must not point.  This directory's README covers only what
is different here, which is the backend, the isolation floor and the cost.

Running this needs a real Azure subscription and **creates a billable sandbox** — see
this directory's README for the prerequisites and the environment variables.
"""

# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "agent-framework-openai",
#     # Imported directly for `DefaultAzureCredential`, so it is named here rather than taken
#     # on loan from maf-sandbox-acas, which happens to depend on it today.
#     "azure-identity",
#     "maf-sandbox-acas",
#     "maf-sandbox-codeact",
#     "maf-sandbox>=0.12",
# ]
# ///

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from _scaffold import MEASURED, quoted, require_env_vars
from agent_framework import Agent, InMemoryAgentFileStore
from agent_framework.openai import OpenAIChatClient
from azure.identity.aio import DefaultAzureCredential
from maf_sandbox import (
    Artifact,
    LandedArtifact,
    OutputSink,
    SandboxRouter,
    make_file_system_sink,
)
from maf_sandbox.maf import list_all_files, make_caller_context
from maf_sandbox_acas import AcasSandboxBackend, AcasSandboxConfig
from maf_sandbox_codeact import CodeactOutputs, make_codeact_tools

# Keyed by (scope, thread_id, agent_dir); constants here since this program serves one request.
SCOPE = "samples"
THREAD_ID = "14-acas-codeact-files"
AGENT_DIR = "data-analyst"

#: A standard MCR devcontainer image at Python 3.13 — sample 03's, imported into the sandbox
#: group as a disk image. Fully qualified, so no registry variable accompanies it.
CODEACT_IMAGE = "mcr.microsoft.com/devcontainers/python:3.13-bookworm"

#: Ships beside this file and is seeded into the agent's file store under the same name. A copy
#: of sample 08's, byte for byte: the two samples answer the same question over the same rows,
#: so a red on one side while the other is green names the backend rather than the workload.
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

#: Everything the sandbox backend needs. No `ACAS_SANDBOX_REGISTRY`: `CODEACT_IMAGE` is
#: already fully-qualified.
SANDBOX_VARS = (
    "ACAS_SANDBOX_ENDPOINT",
    "ACAS_SANDBOX_SUBSCRIPTION_ID",
    "ACAS_SANDBOX_RESOURCE_GROUP",
    "ACAS_SANDBOX_GROUP",
)

#: Everything the chat model needs. Auth is `DefaultAzureCredential`, so there is
#: no key here — `az login` is enough.
MODEL_VARS = ("AZURE_OPENAI_ENDPOINT", "AZURE_OPENAI_CHAT_MODEL")


def make_recording_sink(output_dir: Path, delivered: list[str]) -> OutputSink:
    """`make_file_system_sink`, with this turn's names recorded as they land.

    Sample 08's, unchanged, and it is unchanged on purpose: a sink is host-side code that
    never learns which backend produced the bytes it is handed.  If this function had to know,
    the pull surface would not be portable and the sample would be making the opposite point.
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
    """Wire the stack, run one turn, and take the sandbox down again."""
    env = require_env_vars(SANDBOX_VARS + MODEL_VARS)
    if env is None:
        return 2

    backend = AcasSandboxBackend(
        AcasSandboxConfig(
            endpoint=env["ACAS_SANDBOX_ENDPOINT"],
            subscription_id=env["ACAS_SANDBOX_SUBSCRIPTION_ID"],
            resource_group=env["ACAS_SANDBOX_RESOURCE_GROUP"],
            sandbox_group=env["ACAS_SANDBOX_GROUP"],
        )
    )

    # No `min_isolation`, where sample 08 opts down to `Isolation.CONTAINER`. That is the one
    # line worth pausing on: 08's own comment says the floor should be chosen against the
    # provenance of the file store, because a store turns the program's input into something
    # other than source the model wrote. Here the answer costs nothing to give — the default
    # floor is `Isolation.MICROVM` and this backend meets it, so the same workload runs a rung
    # higher without the application asking.
    router = SandboxRouter([backend])

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
        # Unreachable given the checks above; printed because the `[]` contract is worth
        # stating. `[]` means one thing only — no backend was registered at all. A backend that
        # *is* registered and cannot serve this spec never reaches here: a floor breach raises
        # at `SandboxRouter(...)`, and a missing capability raises `SandboxCapabilityNotSupported`
        # out of `make_codeact_tools` itself. So the whole kind is refused rather than the output
        # channel quietly dropped — loudly, and before this line. Sample 11 shows that on purpose.
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
        # Deletes rather than relying on the lifecycle timers — see sample 01's README.
        deleted = await router.dispose_scope(SCOPE, THREAD_ID)
        print(f"\n{MEASURED}Disposed {deleted} sandbox(es).")
        await backend.aclose()
        await credential.close()

    # JSON, not a comma-joined sentence: an artifact name may legally contain a comma, so
    # one delivery called `notes, summary.md` would read back as two — one of them the name
    # this run is checked for.
    print(f"{MEASURED}Delivered this turn into {OUTPUT_DIR.name}/: {json.dumps(delivered)}")

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
