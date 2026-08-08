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

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Any

from agent_framework import Agent, InMemoryAgentFileStore
from agent_framework.openai import OpenAIChatCompletionClient
from maf_sandbox import SandboxRouter
from maf_sandbox.maf import make_workspace_context
from maf_sandbox_bicep import make_bicep_tools
from maf_sandbox_wslc import WslcSandboxBackend, WslcSandboxConfig

# A sandbox is keyed by (scope, thread_id, agent_dir).  A host reads the first two
# from its own request context — a user/tenant and a conversation.  This program
# serves exactly one request, so they are constants here, but they are still named
# rather than inlined: the whole point of `make_workspace_context` below is that
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


def require_environment(names: tuple[str, ...]) -> dict[str, str] | None:
    """Read `names` from the environment, or report every one that is missing.

    Worth doing before anything else, and worth failing on.  `make_bicep_tools`
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


async def list_workspace(store: Any) -> list[str]:
    """Return every file in the workspace store, as workspace-relative paths.

    This listing is the workload's **injection-pinning boundary**: only a name
    that appears in it is ever substituted into a sandbox command, so a path the
    model invented reaches no shell.  `list_children` returns bare names one
    level at a time, so walking it is the host's job rather than the store's.
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


async def run() -> int:
    """Wire the stack, run one turn, and take the container down again."""
    env = require_environment(SANDBOX_VARS + MODEL_VARS)
    if env is None:
        return 2

    backend = WslcSandboxBackend(WslcSandboxConfig())

    # `deployed=True` is absent by necessity, not oversight: `Isolation.CONTAINER`
    # is not in `DEPLOYED_ISOLATION`, so adding it raises
    # `SandboxBackendNotPermitted` here at construction.
    router = SandboxRouter([backend])

    store = InMemoryAgentFileStore()
    await store.write(BICEP_FILE, (Path(__file__).parent / BICEP_FILE).read_text())

    context = make_workspace_context(
        list_workspace,
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
