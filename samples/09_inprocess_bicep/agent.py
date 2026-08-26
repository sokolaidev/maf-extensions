"""One turn of an agent that validates Bicep against a backend that is not isolated.

Sample 02's ``bicep_validate`` workload unchanged, behind ``NoIsolationBackend`` (in
``no_isolation_backend.py``): a host work directory and a real ``bicep`` subprocess — the
floor of the isolation ladder (:data:`~maf_sandbox.Isolation.NONE`, no boundary at all).

The egress is **honest**: a no-boundary backend cannot confine egress, so it declares the one
mode it enforces — ``UNRESTRICTED`` — and the Bicep tool is wired to run in that mode. The
router serves the pairing because the mode asked and the mode enforced agree; why running
unconfined is acceptable for this dev workload is argued in ``README.md``. One
``OpenAIChatCompletionClient`` serves Azure OpenAI in CI and a local Ollama server by default,
branched on ``AZURE_OPENAI_ENDPOINT``.

The walkthrough and environment variables are in ``README.md``; read it first.
"""

# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "agent-framework-openai",
#     "azure-core[aio]",
#     "azure-identity",
#     "maf-sandbox-bicep",
#     "maf-sandbox>=0.25",
# ]
# ///

from __future__ import annotations

import asyncio
import os
import re
import sys
from pathlib import Path

from _scaffold import MEASURED, evidence, installed_versions, quoted, require_env_vars, tool_results
from agent_framework import Agent, InMemoryAgentFileStore
from agent_framework.openai import OpenAIChatCompletionClient
from maf_sandbox import Egress, Isolation, SandboxRouter
from maf_sandbox.maf import list_all_files, make_caller_context
from maf_sandbox_bicep import make_bicep_tools
from no_isolation_backend import NoIsolationBackend

# Keyed by (scope, thread_id, agent_dir); constants here since this program serves one request.
SCOPE = "samples"
THREAD_ID = "09-inprocess-bicep"
AGENT_DIR = "devops-engineer"

BICEP_FILE = "main.bicep"

#: The tool this sample counts results from. What it returned is what the live check reads: the
#: model writes the prose around it, the framework writes this.
BICEP_TOOL = "bicep_validate"

#: What a result that reached the sandbox looks like. `bicep_validate` answers with an error
#: string *before* it acquires anything when no conversation is bound, when a name has the wrong
#: suffix, and when a name is not in its file listing — so counting calls would credit the run
#: with a compile that never happened. Each phase renders one line at the start of a line, which
#: a refusal has no way to produce.
_PHASES = re.compile(r"^build\(.*^lint\(", re.MULTILINE | re.DOTALL)
#: Bicep finds its config only by walking up from the source file, so it must sit at the
#: work-directory root — the place a container image bakes it. The host backend has no image,
#: so the sample ships this file and seeds it into the work directory on acquire, the way
#: samples 01/02/05 bake it into theirs.
BICEPCONFIG_FILE = "bicepconfig.json"

#: Local-Ollama defaults. The model defaults so a running `ollama serve` is the whole of
#: configuration; the base URL is Ollama's OpenAI-compatible endpoint; the key is a non-empty
#: placeholder the server ignores — the client requires *something* here even for a keyless
#: server, and a local one never reads it.
DEFAULT_LOCAL_MODEL = "minimax-m3:cloud"
DEFAULT_LOCAL_BASE_URL = "http://localhost:11434/v1"
LOCAL_API_KEY_PLACEHOLDER = "ollama"


async def run() -> int:
    """Wire the stack, run one turn, and dispose the (no-isolation) sandbox."""
    # The backend: a host work directory per sandbox, the bicepconfig.json seeded at its root,
    # and the real bicep compiler a shell-out away. No container, no VM, no image.
    backend = NoIsolationBackend(
        seed_files={
            BICEPCONFIG_FILE: (Path(__file__).parent / BICEPCONFIG_FILE).read_text(encoding="utf-8")
        }
    )

    # Below the router's default `microvm` floor; opted down to NONE, the no-boundary rung.
    router = SandboxRouter([backend], min_isolation=Isolation.NONE)

    store = InMemoryAgentFileStore()
    await store.write(BICEP_FILE, (Path(__file__).parent / BICEP_FILE).read_text())

    context = make_caller_context(list_all_files, lambda: SCOPE, lambda: THREAD_ID)

    # No `image=`: there is no image — the backend runs the bicep binary already on the host.
    # `egress=UNRESTRICTED` because that is the one mode this no-boundary backend enforces;
    # asking for anything tighter would be refused, since it cannot deliver a boundary it does
    # not have. The module restore then runs against the live registry, unconfined.
    tools = make_bicep_tools(router, store, AGENT_DIR, context, egress=Egress.UNRESTRICTED)
    if not tools:
        print("No sandbox backend: bicep_validate was not attached.", file=sys.stderr)
        return 2

    # One client class, two endpoints, branched on a single variable. CI sets
    # AZURE_OPENAI_ENDPOINT and reaches Azure OpenAI with a federated credential (no key); a
    # developer's machine leaves it unset and the client talks to a local Ollama server with
    # defaults — zero configuration. The two paths are mutually exclusive.
    azure_endpoint = os.environ.get("AZURE_OPENAI_ENDPOINT")
    credential = None
    if azure_endpoint:
        env = require_env_vars(("AZURE_OPENAI_CHAT_MODEL",))
        if env is None:
            return 2
        from azure.identity.aio import DefaultAzureCredential

        credential = DefaultAzureCredential()
        client = OpenAIChatCompletionClient(
            model=env["AZURE_OPENAI_CHAT_MODEL"],
            azure_endpoint=azure_endpoint,
            credential=credential,
        )
    else:
        client = OpenAIChatCompletionClient(
            model=os.environ.get("OPENAI_CHAT_MODEL") or DEFAULT_LOCAL_MODEL,
            base_url=os.environ.get("OPENAI_BASE_URL") or DEFAULT_LOCAL_BASE_URL,
            api_key=LOCAL_API_KEY_PLACEHOLDER,
        )

    try:
        agent = Agent(
            client=client,
            name=AGENT_DIR,
            instructions=(
                "You validate Azure Bicep. Always call the bicep_validate tool "
                "and report exactly the diagnostics it returns — rule id, "
                "severity, file, line and message. Never judge the file from "
                "reading it, and never invent, reword or omit a diagnostic."
            ),
            tools=tools,
        )
        response = await agent.run(f"Validate {BICEP_FILE} and list every diagnostic you get back.")
        # Quoted first, because the reply and the block below share one stream and the live
        # check trusts the `[measured]` tag completely.
        print(quoted(response.text))

        # The compiler's own words, printed from the tool result rather than from the reply. The
        # reply is the model's account of this, which is the thing the sample exists to be
        # sceptical of: a model that never called the tool can still write a convincing summary
        # of what it would have said (#314).
        compiles = [
            result for result in tool_results(response, BICEP_TOOL) if _PHASES.search(result)
        ]
        print()
        print(
            evidence(
                "Diagnostics as bicep_validate returned them",
                compiles,
                "compiles that reached the sandbox",
            )
        )
    finally:
        purge = await router.dispose_scope(SCOPE, THREAD_ID)
        print(f"\n{MEASURED}Disposed {purge.disposed} sandbox(es).")
        if purge.undisposed is not None:
            print(f"{MEASURED}Not fully disposed: {purge.undisposed}")
        if credential is not None:
            await credential.close()

    return 0


if __name__ == "__main__":
    print(installed_versions())
    raise SystemExit(asyncio.run(run()))
