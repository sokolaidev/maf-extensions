"""One turn of an agent that validates Bicep with a compiler instead of with itself.

This is the `app` box of the layering the whole repository is organised around::

    app  ->  maf_sandbox (router)  ->  maf_sandbox_acas  ->  the sandbox
                  ^ maf_sandbox_bicep calls the router

Deliberately the smallest thing that is still real: no chat loop, no multi-turn
fix cycle, no framework of its own.  It puts `main.bicep` — which has a mistake
in it on purpose — into a file store, hands the agent the `bicep_validate`
tool, runs exactly one turn, prints what came back, and deletes the sandbox.

What the printed diagnostics prove is the point.  They come from the Bicep
compiler running inside a microVM-isolated sandbox (T2), not from the model reading
its own output and agreeing with itself (T0).  Running this against a *valid*
file would prove much less.

Running it needs a real Azure subscription and **creates a billable sandbox** — see
this directory's README for the prerequisites and the environment variables.
"""

# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "agent-framework-openai",
#     "maf-sandbox-acas",
#     "maf-sandbox-bicep",
#     "maf-sandbox>=0.12",
# ]
# ///

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from _scaffold import require_env_vars
from agent_framework import Agent, InMemoryAgentFileStore
from agent_framework.openai import OpenAIChatClient
from azure.identity.aio import DefaultAzureCredential
from maf_sandbox import SandboxRouter
from maf_sandbox.maf import list_all_files, make_caller_context
from maf_sandbox_acas import AcasSandboxBackend, AcasSandboxConfig
from maf_sandbox_bicep import make_bicep_tools

# A sandbox is keyed by (scope, thread_id, agent_dir).  A host reads the first two
# from its own request context — a user/tenant and a conversation.  This program
# serves exactly one request, so they are constants here, but they are still named
# rather than inlined: the whole point of `make_caller_context` below is that
# they belong to the request, not to the agent.
SCOPE = "samples"
THREAD_ID = "01-acas-bicep"
AGENT_DIR = "devops-engineer"

BICEP_FILE = "main.bicep"

#: Everything the sandbox backend needs. `BICEP_SANDBOX_IMAGE` is a bare
#: `repository:tag` (for example `bicep-sandbox:0.46.1`); the registry above
#: qualifies it into a full reference.
SANDBOX_VARS = (
    "ACAS_SANDBOX_ENDPOINT",
    "ACAS_SANDBOX_SUBSCRIPTION_ID",
    "ACAS_SANDBOX_RESOURCE_GROUP",
    "ACAS_SANDBOX_GROUP",
    "ACAS_SANDBOX_REGISTRY",
    "BICEP_SANDBOX_IMAGE",
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
            registry=env["ACAS_SANDBOX_REGISTRY"],
        )
    )

    # A backend below `Isolation.MICROVM` here raises
    # `SandboxBackendNotPermitted` — at construction, not at first tool call, so
    # a misconfigured deployment cannot start with the feature apparently
    # enabled and quietly unsafe.
    #
    # A swapped backend has a second way to be refused, one call further down:
    # `make_bicep_tools` checks it can confine egress to the hosts the workload
    # names.  Separate rules because they have separate owners — the boundary is
    # this host's policy, what the sandbox may reach is the workload's.
    router = SandboxRouter([backend])

    # The agent's file store.  A real host's store is usually backed by a disk or
    # a blob container and already holds what the agent wrote earlier in the
    # conversation; here the sample seeds it with the one file to validate.
    store = InMemoryAgentFileStore()
    await store.write(BICEP_FILE, (Path(__file__).parent / BICEP_FILE).read_text())

    # All three arguments are **callables, read per call** — not values.  That is
    # load-bearing rather than a convenience.  A sandbox is keyed by
    # (scope, thread_id, agent_dir); a host that builds one agent and serves many
    # conversations with it would, if scope and thread were captured here, let
    # one conversation address another conversation's sandbox.  Reading them per
    # call keeps the key a property of the request.  It is also why nothing in
    # this stack accepts a scope or a thread id from the model.
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
        # Unreachable with the checks above, and printed rather than asserted
        # because the contract is worth stating: an unconfigured host gets `[]`
        # back — no tool at all — rather than a tool that fails when called.
        print("No sandbox backend: bicep_validate was not attached.", file=sys.stderr)
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
        # Delete the sandbox rather than leaving it to the lifecycle timers.
        # `auto_suspend_seconds` and `auto_delete_seconds` are the backstop, not
        # the plan: a sandbox per agent per conversation, billable for ten minutes
        # after the last call, adds up fast.  `dispose_scope` deletes by
        # service-side label, so it reclaims sandboxes this process never saw.
        deleted = await router.dispose_scope(SCOPE, THREAD_ID)
        print(f"\nDisposed {deleted} sandbox(es).")
        await backend.aclose()
        await credential.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
