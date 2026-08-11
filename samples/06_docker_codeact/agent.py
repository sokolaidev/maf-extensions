"""One deterministic turn of an agent that computes with code instead of with itself.

Sample 03 with exactly one thing changed — the backend::

    app  ->  maf_sandbox (router)  ->  maf_sandbox_docker  ->  the container
                  ^ maf_sandbox_codeact calls the router

The workload, the task and the model wiring are identical to sample 03's; only the
backend and the isolation floor differ.  That is the tightest "one line lower" in
the set: sample 04 also swapped sample 03's Azure model for a local one, and this
keeps it, because keeping it is what lets this sample be verified in CI with no
stored secret and — unlike sample 03 — no billable sandbox.

The sandbox is a plain Docker container on the runner (or your own machine), which
costs nothing; the model is the same Azure OpenAI deployment sample 03 uses,
reached with `DefaultAzureCredential`, so there is no API key here either.  A
developer without Azure runs this against a local endpoint by making sample 04's
one-line client swap; the wiring below is the one CI needs.

The boundary is a container, not a VM — an honest downgrade from sample 03, and
this directory's README says what it costs and, for this workload, what it does
not.  Read it, along with the prerequisites and the environment variables, first.
"""

from __future__ import annotations

import asyncio
import os
import sys

from agent_framework import Agent
from agent_framework.openai import OpenAIChatClient
from azure.identity.aio import DefaultAzureCredential
from maf_sandbox import Isolation, SandboxRouter
from maf_sandbox.maf import make_workspace_context
from maf_sandbox_codeact import make_codeact_tools
from maf_sandbox_docker import DockerSandboxBackend, DockerSandboxConfig

# Keyed by (scope, thread_id, agent_dir); constants here since this program serves one request.
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

#: The Docker backend reads no environment — it drives the local `docker` client —
#: so the only variables here are the model's. Auth is `DefaultAzureCredential`,
#: so there is no key: `az login` (or a federated CI credential) is enough.
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
    """Wire the stack, run one turn, and take the container down again."""
    env = require_environment(MODEL_VARS)
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
        print(response.text)
    finally:
        # Deletes rather than relying on the container living on — see sample 01's README.
        deleted = await router.dispose_scope(SCOPE, THREAD_ID)
        print(f"\nDisposed {deleted} sandbox(es).")
        await credential.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
