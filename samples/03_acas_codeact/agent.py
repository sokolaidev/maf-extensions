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

from __future__ import annotations

import asyncio
import os
import sys

from agent_framework import Agent
from agent_framework.openai import OpenAIChatClient
from azure.identity.aio import DefaultAzureCredential
from maf_sandbox import SandboxRouter
from maf_sandbox.maf import make_workspace_context
from maf_sandbox_acas import AcasSandboxBackend, AcasSandboxConfig
from maf_sandbox_codeact import make_codeact_tools

# A sandbox is keyed by (scope, thread_id, agent_dir).  A host reads the first two
# from its own request context — a user/tenant and a conversation.  This program
# serves exactly one request, so they are constants here, but they are still named
# rather than inlined: the whole point of `make_workspace_context` below is that
# they belong to the request, not to the agent.
SCOPE = "samples"
THREAD_ID = "03-acas-codeact"
AGENT_DIR = "data-analyst"

#: A standard Microsoft Container Registry devcontainer image, at Python 3.13 — N-1
#: against current stable.  It is the only standard MCR family at 3.13 usable by both
#: this backend and wslc: `azurelinux/base/python` caps at 3.12, and the
#: docker-library mirror is frozen at 3.11.  See this directory's README for why a
#: dev-container image is bulkier than this sandbox strictly needs.
CODEACT_IMAGE = "mcr.microsoft.com/devcontainers/python:3.13-bookworm"

TASK = (
    "Write a Python program that computes the 100th Fibonacci number, with "
    "F(0) = 0 and F(1) = 1, and prints just the integer. Run it and tell me "
    "exactly what it printed."
)

#: Everything the sandbox backend needs. No `ACAS_SANDBOX_REGISTRY`: `CODEACT_IMAGE`
#: above is already a fully-qualified reference, so nothing needs qualifying against
#: a registry the way `BICEP_SANDBOX_IMAGE` does in sample 01.
SANDBOX_VARS = (
    "ACAS_SANDBOX_ENDPOINT",
    "ACAS_SANDBOX_SUBSCRIPTION_ID",
    "ACAS_SANDBOX_RESOURCE_GROUP",
    "ACAS_SANDBOX_GROUP",
)

#: Everything the chat model needs. Auth is `DefaultAzureCredential`, so there is
#: no key here — `az login` is enough.
MODEL_VARS = ("AZURE_OPENAI_ENDPOINT", "AZURE_OPENAI_CHAT_MODEL")


def require_environment(names: tuple[str, ...]) -> dict[str, str] | None:
    """Read `names` from the environment, or report every one that is missing.

    Worth doing before anything else, and worth failing on.  `make_codeact_tools`
    returns an empty list when the router has no backend, so a half-configured
    run does not crash — it quietly produces an agent with no tools, which
    answers the question from the model alone.  That is the T0 behaviour this
    sample exists to contrast with, and it is indistinguishable from success
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
    """CodeAct shares no workspace files, so nothing ever enumerates one."""
    return []


async def run() -> int:
    """Wire the stack, run one turn, and take the sandbox down again."""
    env = require_environment(SANDBOX_VARS + MODEL_VARS)
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

    # No `min_isolation` here is not an oversight: the router's default floor is
    # `Isolation.MICROVM`, and `AcasSandboxBackend` declares exactly that, so this
    # sample configures nothing and still gets the production posture. Compare
    # sample 04, which opts the floor down explicitly for a container-isolated
    # backend.
    router = SandboxRouter([backend])

    # All three arguments are **callables, read per call** — not values. A sandbox
    # is keyed by (scope, thread_id, agent_dir); a host that builds one agent and
    # serves many conversations with it would, if scope and thread were captured
    # here, let one conversation address another conversation's sandbox.
    context = make_workspace_context(
        no_workspace_files,
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
        # Unreachable with the checks above, and printed rather than asserted
        # because the contract is worth stating: an unconfigured host gets `[]`
        # back — no tool at all — rather than a tool that fails when called.
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
        print(response.text)
    finally:
        # Delete the sandbox rather than leaving it to the lifecycle timers. See
        # sample 01's README for what those timers cost if a run is killed mid-turn.
        deleted = await router.dispose_scope(SCOPE, THREAD_ID)
        print(f"\nDisposed {deleted} sandbox(es).")
        await backend.aclose()
        await credential.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
