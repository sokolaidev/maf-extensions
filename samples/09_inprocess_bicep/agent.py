"""One turn of an agent that validates Bicep against a backend that is not real.

Sample 02 with the backend swapped for the in-process fake from ``maf_sandbox.testing``::

    app  ->  maf_sandbox (router)  ->  maf_sandbox.testing  ->  this process
                  ^ maf_sandbox_bicep calls the router

The workload is sample 02's unchanged — ``make_bicep_tools``, the fixed ``bicep build`` and
``bicep lint`` command templates, the SARIF parser — and the file is the same kind of
deliberately broken ``main.bicep``.  What sits behind the router is not a container or a VM
but an :class:`~maf_sandbox.testing.InProcessSandbox` scripted to answer those two commands:
``bicep build`` returns an empty SARIF document, ``bicep lint`` returns one with a single
``no-hardcoded-location`` finding, the finding a real compiler would flag against the
hardcoded ``location: 'eastus'`` below.  No Bicep binary, no image, no billable anything.

That makes this the floor of the set — below sample 02's local container, below every
backend — and the learning ground for the protocol seam itself: the router acquires, the
workload writes the file and runs the commands, the SARIF comes back and is rendered, all in
one process a reader can step through with no infrastructure at all.  The fake is an honest
stand-in for the compiler here because the command templates carry no model text — a marker
match on a fixed string is exactly what a real ``exec`` would answer, just scripted.

One client class, two endpoints.  CI sets ``AZURE_OPENAI_ENDPOINT`` and the client reaches
Azure OpenAI with a federated :class:`DefaultAzureCredential` — no key, the same wiring
sample 06 uses.  A developer's machine leaves it unset and the client talks to a local Ollama
server on its default port, with a placeholder key the server ignores — zero configuration,
not even a model name, because the model defaults when it is not provided.  The two paths are
mutually exclusive and the branch is one environment variable.

This directory's README is the walkthrough — what the fake scripts, what to watch for, and
the environment variables.  Read it first.
"""

# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "agent-framework-openai",
#     "azure-core[aio]",
#     "azure-identity",
#     "maf-sandbox-bicep",
#     "maf-sandbox>=0.12",
# ]
# ///

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

from agent_framework import Agent, InMemoryAgentFileStore
from agent_framework.openai import OpenAIChatCompletionClient
from maf_sandbox import Isolation, SandboxRouter
from maf_sandbox.maf import list_all_files, make_caller_context
from maf_sandbox.testing import InProcessSandbox, InProcessSandboxBackend
from maf_sandbox_bicep import make_bicep_tools
from _scaffold import require_env_vars

# Keyed by (scope, thread_id, agent_dir); constants here since this program serves one request.
SCOPE = "samples"
THREAD_ID = "09-inprocess-bicep"
AGENT_DIR = "devops-engineer"

BICEP_FILE = "main.bicep"

#: Local-Ollama defaults. The model defaults so a running `ollama serve` is the whole of
#: configuration; the base URL is Ollama's OpenAI-compatible endpoint; the key is a non-empty
#: placeholder the server ignores — the client requires *something* here even for a keyless
#: server, and a local one never reads it.
DEFAULT_LOCAL_MODEL = "minimax-m3:cloud"
DEFAULT_LOCAL_BASE_URL = "http://localhost:11434/v1"
LOCAL_API_KEY_PLACEHOLDER = "ollama"

#: What the fake compiler returns. `bicep build` is clean; `bicep lint` flags the hardcoded
#: location in main.bicep. The markers ("bicep build", "bicep lint") are distinct substrings
#: of the fixed command templates, so the fake answers each phase from a different script.
_BUILD_SARIF = json.dumps({"version": "2.1.0", "runs": []})

_LINT_SARIF = json.dumps(
    {
        "version": "2.1.0",
        "runs": [
            {
                "tool": {
                    "driver": {
                        "name": "bicep",
                        "rules": [
                            {
                                "id": "no-hardcoded-location",
                                "helpUri": "https://aka.ms/bicep/no-hardcoded-location",
                            }
                        ],
                    }
                },
                "results": [
                    {
                        "ruleId": "no-hardcoded-location",
                        "level": "warning",
                        "message": {
                            "text": (
                                "Resource location should not be a hard-coded string; use a "
                                "parameter, a variable, or an expression like "
                                "resourceGroup().location."
                            )
                        },
                        "locations": [
                            {
                                "physicalLocation": {
                                    "artifactLocation": {"uri": "main.bicep"},
                                    "region": {"startLine": 6},
                                }
                            }
                        ],
                    }
                ],
            }
        ],
    }
)


async def run() -> int:
    """Wire the stack, run one turn, and dispose the (in-process) sandbox."""
    # The backend: an in-process sandbox scripted to answer the two fixed bicep commands. No
    # container, no VM, no image — the fake is the whole backend, and `default_stdout` is a
    # clean SARIF document so any unmatched command reads as "no diagnostics" rather than a
    # parse error.
    sandbox = InProcessSandbox(
        outputs={"bicep build": _BUILD_SARIF, "bicep lint": _LINT_SARIF},
        default_stdout=_BUILD_SARIF,
    )
    backend = InProcessSandboxBackend(sandbox)

    # Below the router's default `microvm` floor; opted down to PROCESS, the fake's rung.
    router = SandboxRouter([backend], min_isolation=Isolation.PROCESS)

    store = InMemoryAgentFileStore()
    await store.write(BICEP_FILE, (Path(__file__).parent / BICEP_FILE).read_text())

    context = make_caller_context(list_all_files, lambda: SCOPE, lambda: THREAD_ID)

    # No `image=`: the fake never pulls, and the spec's image field is the one thing a real
    # backend would supply that this one genuinely does not need.
    tools = make_bicep_tools(router, store, AGENT_DIR, context)
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
        response = await agent.run(
            f"Validate {BICEP_FILE} and list every diagnostic you get back."
        )
        print(response.text)
    finally:
        deleted = await router.dispose_scope(SCOPE, THREAD_ID)
        print(f"\nDisposed {deleted} sandbox(es).")
        if credential is not None:
            await credential.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
