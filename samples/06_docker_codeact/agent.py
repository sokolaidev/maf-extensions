"""One deterministic turn of an agent that computes with code instead of with itself.

Sample 03 with its CodeAct workload, task, and model wiring unchanged, running on Docker::

    app  ->  maf_sandbox (router)  ->  maf_sandbox_docker  ->  the container
                  ^ maf_sandbox_codeact calls the router

The workload, the task and the model wiring are identical to sample 03's; what
differs is the configuration around them — the backend and its isolation floor,
and the image it runs in (a registry pull, not the service-provided
`python-3.13`).  That is the tightest "one line lower" in the set: sample 04 also
swapped sample 03's Azure model for a local one, and this keeps it, because
keeping it is what lets this sample be verified in CI with no stored secret and
— unlike sample 03 — no billable sandbox.

The sandbox is a plain Docker container on the runner (or your own machine), which
costs nothing; the model is the same Azure OpenAI deployment sample 03 uses,
reached with `DefaultAzureCredential`, so there is no API key here either.  A
developer without Azure runs this against a local endpoint by making sample 04's
one-line client swap; the wiring below is the one CI needs.

The boundary is a container, not a VM — an honest downgrade from sample 03, and
this directory's README says what it costs and, for this workload, what it does
not.  Read it, along with the prerequisites and the environment variables, first.
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
import re
import sys

from _scaffold import MEASURED, evidence, installed_versions, quoted, require_env_vars, tool_results
from agent_framework import Agent
from agent_framework.openai import OpenAIChatClient
from azure.identity.aio import DefaultAzureCredential
from maf_sandbox import Isolation, SandboxRouter
from maf_sandbox.maf import list_no_files, make_caller_context
from maf_sandbox_codeact import make_codeact_tools
from maf_sandbox_docker import DockerSandboxBackend, DockerSandboxConfig

# Keyed by the caller's scope, thread and agent directory; constants here since this
# program serves one request.
SCOPE = "samples"
THREAD_ID = "06-docker-codeact"
AGENT_DIR = "data-analyst"

#: A standard MCR devcontainer image at Python 3.13; see this directory's README for why.
CODEACT_IMAGE = "mcr.microsoft.com/devcontainers/python:3.13-bookworm"

TASK = (
    "Write a Python program that computes the 100th Fibonacci number, with "
    "F(0) = 0 and F(1) = 1, and prints just the integer. Run it and tell me "
    "exactly what it printed."
)

#: The tool this sample counts results from. What it returned is what the live check reads: the
#: model writes the prose around it, the interpreter writes this.
CODEACT_TOOL = "execute_code"

#: How the tool renders a run: a `stdout:`, `stderr:` or `exit code:` section, each at the start
#: of a line. A call it refuses — a file it cannot read, a request over a transfer cap — comes
#: back as an `Error:` string carrying none of them, so this is what separates a program the
#: interpreter ran from a request that never reached it. A program that both printed nothing and
#: exited 0 carries none of them either, and this task is not answered by one.
_RAN = re.compile(r"^(stdout|stderr|exit code):", re.MULTILINE)

#: The Docker backend reads no environment — it drives the local `docker` client —
#: so the only variables here are the model's. Auth is `DefaultAzureCredential`,
#: so there is no key: `az login` (or a federated CI credential) is enough.
MODEL_VARS = ("AZURE_OPENAI_ENDPOINT", "AZURE_OPENAI_CHAT_MODEL")


async def run() -> int:
    """Wire the stack, run one turn, and take the container down again."""
    env = require_env_vars(MODEL_VARS)
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

    tools = make_codeact_tools(
        router,
        AGENT_DIR,
        context,
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
                "You answer computational questions by writing and running Python "
                "with the execute_code tool, never by computing them yourself. "
                "Always call the tool, and report exactly what it returned — do "
                "not paraphrase, round, or recompute the number it printed."
            ),
            tools=tools,
        )
        response = await agent.run(TASK)
        # Quoted first, because the reply and the block below share one stream and the live
        # check trusts the `[measured]` tag completely.
        print(quoted(response.text))

        # What the interpreter printed, taken from the tool result rather than from the reply.
        # The 100th Fibonacci number is a constant any model can recite, so the reply alone is
        # not evidence a program ran — this block is (#314).
        runs = [result for result in tool_results(response, CODEACT_TOOL) if _RAN.search(result)]
        print()
        print(
            evidence(
                "Program output as execute_code returned it",
                runs,
                "programs whose output came back from the sandbox",
            )
        )
    finally:
        # Deletes rather than relying on the container living on — see sample 01's README.
        purge = await router.dispose_scope(SCOPE, THREAD_ID)
        print(f"\n{MEASURED}Disposed {purge.disposed} sandbox(es).")
        if purge.undisposed is not None:
            print(f"{MEASURED}Not fully disposed: {purge.undisposed}")
        await credential.close()

    return 0


if __name__ == "__main__":
    print(installed_versions())
    raise SystemExit(asyncio.run(run()))
