"""One turn of an agent that validates Bicep with a compiler instead of with itself.

Sample 01 with exactly one thing changed — the backend::

    app  ->  maf_sandbox (router)  ->  maf_sandbox_wslc  ->  the container
                  ^ maf_sandbox_bicep calls the router

The workload, the wiring and the file being validated are the same; `main.bicep`
here is a byte-identical copy of sample 01's.  That is what makes the pair worth
having: it is the protocol's central claim — a workload written against
`maf_sandbox` runs unchanged on another backend — shown rather than asserted.

Nothing here needs Azure.  `wslc`, the container CLI that ships with WSL 2.9.3 and
later, runs the sandbox on the developer's own machine, and the model is any
OpenAI-compatible endpoint, a local server included.

The boundary is a container, not a VM, and the egress is closed rather than
allowlisted.  Both are honest downgrades from sample 01, and this directory's
README says what each of them costs — read it, along with the prerequisites and
the environment variables, before running this.
"""

# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "agent-framework-openai",
#     "maf-sandbox-bicep",
#     "maf-sandbox-wslc",
#     "maf-sandbox>=0.12",
# ]
# ///

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

from _scaffold import require_env_vars
from agent_framework import Agent, InMemoryAgentFileStore
from agent_framework.openai import OpenAIChatCompletionClient
from maf_sandbox import Isolation, SandboxRouter
from maf_sandbox.maf import list_all_files, make_caller_context
from maf_sandbox_bicep import make_bicep_tools
from maf_sandbox_wslc import WslcSandboxBackend, WslcSandboxConfig

# A sandbox is keyed by (scope, thread_id, agent_dir).  A host reads the first two
# from its own request context — a user/tenant and a conversation.  This program
# serves exactly one request, so they are constants here, but they are still named
# rather than inlined: the whole point of `make_caller_context` below is that
# they belong to the request, not to the agent.
SCOPE = "samples"
THREAD_ID = "02-wslc-bicep"
AGENT_DIR = "devops-engineer"

BICEP_FILE = "main.bicep"

#: Everything the sandbox backend needs. `BICEP_SANDBOX_IMAGE` is a local image
#: reference (for example `bicep-sandbox:local`); there is no registry to qualify
#: it, because `wslc` runs what is already on this machine.
SANDBOX_VARS = ("BICEP_SANDBOX_IMAGE",)

#: Everything the chat model needs. `OPENAI_BASE_URL` is optional, so it is read
#: separately: unset, the client talks to OpenAI.
MODEL_VARS = ("OPENAI_API_KEY", "OPENAI_CHAT_MODEL")


async def run() -> int:
    """Wire the stack, run one turn, and take the container down again."""
    env = require_env_vars(SANDBOX_VARS + MODEL_VARS)
    if env is None:
        return 2

    backend = WslcSandboxBackend(WslcSandboxConfig())

    # Below the router's default `microvm` floor; opted down explicitly.
    router = SandboxRouter([backend], min_isolation=Isolation.CONTAINER)

    store = InMemoryAgentFileStore()
    await store.write(BICEP_FILE, (Path(__file__).parent / BICEP_FILE).read_text())

    context = make_caller_context(
        list_all_files,
        lambda: SCOPE,
        lambda: THREAD_ID,
    )

    tools = make_bicep_tools(
        router,
        store,
        AGENT_DIR,
        context,
        image=env["BICEP_SANDBOX_IMAGE"],
    )
    if not tools:
        print("No sandbox backend: bicep_validate was not attached.", file=sys.stderr)
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
                "You validate Azure Bicep. Always call the bicep_validate tool "
                "and report exactly the diagnostics it returns — rule id, "
                "severity, file, line and message. Never judge the file from "
                "reading it, and never invent, reword or omit a diagnostic."
            ),
            tools=tools,
        )
        response = await agent.run(
            f"Validate {BICEP_FILE} and list every diagnostic you get back."
        )
        print(response.text)
    finally:
        deleted = await router.dispose_scope(SCOPE, THREAD_ID)
        print(f"\nDisposed {deleted} sandbox(es).")

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
