"""One deterministic turn of an agent that computes with code instead of with itself.

Sample 03 with exactly one thing changed — the backend::

    app  ->  maf_sandbox (router)  ->  maf_sandbox_wslc  ->  the container
                  ^ maf_sandbox_codeact calls the router

The workload and the task are identical to sample 03's; only the backend and the
isolation floor differ.  That is what makes the pair worth having: it is the
protocol's central claim — a workload written against `maf_sandbox` runs unchanged
on another backend — shown rather than asserted.

Nothing here needs Azure.  `wslc`, the container CLI that ships with WSL 2.9.3 and
later, runs the sandbox on the developer's own machine, and the model is any
OpenAI-compatible endpoint, a local server included.

The boundary is a container, not a VM, and that is an honest downgrade from sample
03 — this directory's README says what it costs and what, for this workload, it
does not.  Read it, along with the prerequisites and the environment variables,
before running this.
"""

from __future__ import annotations

import asyncio
import os
import sys

from agent_framework import Agent
from agent_framework.openai import OpenAIChatCompletionClient
from maf_sandbox import Isolation, SandboxRouter
from maf_sandbox.maf import list_no_files, make_caller_context
from maf_sandbox_codeact import make_codeact_tools
from maf_sandbox_wslc import WslcSandboxBackend, WslcSandboxConfig
from _scaffold import require_env_vars

# Keyed by (scope, thread_id, agent_dir) — see sample 03's agent.py.
SCOPE = "samples"
THREAD_ID = "04-wslc-codeact"
AGENT_DIR = "data-analyst"

#: The same reference sample 03 uses; see this directory's README for why.
CODEACT_IMAGE = "mcr.microsoft.com/devcontainers/python:3.13-bookworm"

TASK = (
    "Write a Python program that computes the 100th Fibonacci number, with "
    "F(0) = 0 and F(1) = 1, and prints just the integer. Run it and tell me "
    "exactly what it printed."
)

#: Everything the chat model needs. `OPENAI_BASE_URL` is optional, so it is read
#: separately: unset, the client talks to OpenAI.
MODEL_VARS = ("OPENAI_API_KEY", "OPENAI_CHAT_MODEL")


async def run() -> int:
    """Wire the stack, run one turn, and take the container down again."""
    env = require_env_vars(MODEL_VARS)
    if env is None:
        return 2

    backend = WslcSandboxBackend(WslcSandboxConfig())

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
        print("No sandbox backend: execute_code was not attached.", file=sys.stderr)
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
        deleted = await router.dispose_scope(SCOPE, THREAD_ID)
        print(f"\nDisposed {deleted} sandbox(es).")

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
