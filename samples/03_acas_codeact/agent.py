"""One deterministic turn of an agent that computes with code instead of with itself.

This is the `app` box of the layering the whole repository is organised around::

    app  ->  maf_sandbox (router)  ->  maf_sandbox_acas  ->  the sandbox
                  ^ maf_sandbox_codeact calls the router

Deliberately the smallest thing that is still real: no chat loop, no file the agent
authored beforehand, no fix cycle.  It hands the agent the `execute_code` tool, asks
for one Python program with exactly one right answer, prints what came back, and
deletes the sandbox.

What the printed number proves is the point.  It was computed by a Python
interpreter running inside a microVM-isolated sandbox (T2), not predicted by the
model reading its own training data (T0).  A question with a range of acceptable
answers would prove much less.

Running this needs a real Azure subscription and **creates a billable sandbox** —
see this directory's README for the prerequisites, the environment variables, and
how to import the sandbox image into your sandbox group.
"""

# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "agent-framework-openai",
#     "maf-sandbox-acas",
#     "maf-sandbox-codeact",
#     "maf-sandbox>=0.15",
# ]
# ///

from __future__ import annotations

import asyncio
import re
import sys

from _scaffold import MEASURED, evidence, quoted, require_env_vars, tool_results
from agent_framework import Agent
from agent_framework.openai import OpenAIChatClient
from azure.identity.aio import DefaultAzureCredential
from maf_sandbox import SandboxRouter
from maf_sandbox.maf import list_no_files, make_caller_context
from maf_sandbox_acas import AcasSandboxBackend, AcasSandboxConfig
from maf_sandbox_codeact import make_codeact_tools

# Keyed by (scope, thread_id, agent_dir); constants here since this program serves one request.
SCOPE = "samples"
THREAD_ID = "03-acas-codeact"
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

    # No `min_isolation`: the default floor is `Isolation.MICROVM`, which this backend meets.
    router = SandboxRouter([backend])

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
        # Deletes rather than relying on the lifecycle timers — see sample 01's README.
        deleted = await router.dispose_scope(SCOPE, THREAD_ID)
        print(f"\n{MEASURED}Disposed {deleted} sandbox(es).")
        await backend.aclose()
        await credential.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
