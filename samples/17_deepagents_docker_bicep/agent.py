"""One turn of a Deep Agents agent that validates Bicep with a compiler, in a maf-sandbox container.

Sample 05 from the other side of the seam::

    deepagents  ->  maf_sandbox_deepagents  ->  maf_sandbox (router)  ->  maf_sandbox_docker  ->  the container

There is no Microsoft Agent Framework here and no `bicep_validate`.  The agent is
LangChain's Deep Agents, its sandbox is Deep Agents' own `execute` tool, and what sits
behind that tool is a `maf_sandbox` router with the same Docker backend and the same
image sample 05 runs.  The file is the same `main.bicep`, byte for byte, and the
compiler says the same three things about it.

What the router keeps is what the sample exists to show.  The floor is opted down to
`CONTAINER` explicitly, because the default would refuse Docker.  Egress is closed,
because the spec names no host.  The sandbox is keyed by scope, thread and agent
directory, and purged by that key at the end.  What is given up is said in this
directory's README: the agent writes the shell, and Deep Agents' file tools need a
`python3` this image does not carry, so the prompt tells it to use `execute`.

The model is samples 09 and 13's two roads in `langchain-openai`'s terms: an Azure OpenAI
deployment reached with `DefaultAzureCredential` when `AZURE_OPENAI_ENDPOINT` is set, and any
OpenAI-compatible endpoint otherwise.
"""

# /// script
# requires-python = ">=3.12"
# dependencies = [
#     # The async HTTP transport `azure.identity.aio.DefaultAzureCredential` needs, which
#     # `azure-identity` alone does not pull in. Samples 05, 09 and 13 declare it for the same
#     # reason: without it the Azure path fails on import, before the model is ever reached.
#     "azure-core[aio]",
#     "azure-identity",
#     "deepagents",
#     "langchain-core",
#     "langchain-openai",
#     "maf-sandbox-deepagents",
#     "maf-sandbox-docker",
#     "maf-sandbox>=0.39",
# ]
# ///

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from _scaffold import MEASURED, evidence, installed_versions, quoted, require_env_vars
from deepagents import create_deep_agent
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_openai import AzureChatOpenAI, ChatOpenAI
from maf_sandbox import Isolation, SandboxKey, SandboxRouter
from maf_sandbox_deepagents import MafSandbox, deepagents_spec
from maf_sandbox_docker import DockerSandboxBackend, DockerSandboxConfig

if TYPE_CHECKING:
    # The runtime import stays inside `build_model`, so a local run loads no Azure SDK at all.
    from azure.identity.aio import DefaultAzureCredential

# A sandbox is keyed by the caller's scope, thread and agent directory. A host reads the first
# two from its own request context — a user/tenant and a conversation. This program serves
# exactly one request, so they are constants here, but they are still named rather than
# inlined: they belong to the request, not to the agent.
SCOPE = "samples"
THREAD_ID = "17-deepagents-docker-bicep"
AGENT_DIR = "devops-engineer"

BICEP_FILE = "main.bicep"

#: Deep Agents' shell tool, the one this sample counts results from. What it returned is what the
#: live check reads: the model writes the prose around it, the sandbox writes this.
EXECUTE_TOOL = "execute"

#: What a result that reached the compiler looks like: a SARIF document names every diagnostic
#: by `ruleId`. Weaker evidence than sample 05's, and said so in the README — the model wrote
#: the command that produced it, where `bicep_validate` writes its own.
_DIAGNOSTIC = '"ruleId"'

#: Everything the sandbox backend needs. `BICEP_SANDBOX_IMAGE` is a local image reference
#: (for example `bicep-sandbox:local`); the backend runs what is already on this machine.
SANDBOX_VARS = ("BICEP_SANDBOX_IMAGE",)

#: What the Azure road needs beyond the endpoint that selects it. No key: auth is
#: `DefaultAzureCredential`, which an `az login` session or a federated CI credential satisfies.
AZURE_MODEL_VARS = ("AZURE_OPENAI_CHAT_MODEL",)

#: The Azure OpenAI API version this client speaks. `AzureChatOpenAI` requires one and has no
#: default; the value is the one `agent-framework`'s chat-completions client picks for itself,
#: so this sample and samples 09 and 13 reach one deployment over one surface.
AZURE_API_VERSION = "2024-12-01-preview"

#: What a token for an Azure OpenAI deployment is minted against.
AZURE_SCOPE = "https://cognitiveservices.azure.com/.default"

#: Local-Ollama defaults, as samples 09 and 13 carry them. The model defaults so a running
#: `ollama serve` is the whole of configuration; the base URL is Ollama's OpenAI-compatible
#: endpoint; the key is a non-empty placeholder the server ignores — the client requires
#: *something* here even for a keyless server, and a local one never reads it.
DEFAULT_LOCAL_MODEL = "minimax-m3:cloud"
DEFAULT_LOCAL_BASE_URL = "http://localhost:11434/v1"
LOCAL_API_KEY_PLACEHOLDER = "ollama"

#: The compiler's SARIF output, as `bicep_validate` reads it: the plain format prints only the
#: errors once there is one, and this file has one, so the two warnings beside it would go unseen.
BUILD = f"bicep build {BICEP_FILE} --no-restore --diagnostics-format sarif"
LINT = f"bicep lint {BICEP_FILE} --diagnostics-format sarif"

#: `{base}` is the sandbox's storage base, filled in from the spec: Deep Agents' file tools
#: take guest paths, so the model has to be told where its files are.
INSTRUCTIONS = (
    "You validate Azure Bicep with the compiler, never by reading the file yourself. "
    f"The file to validate is {{base}}/{BICEP_FILE}; your working directory is {{base}}. "
    f"Use the execute tool only: run `{BUILD}` and then `{LINT}`, and report exactly the "
    "diagnostics in the SARIF they print — ruleId, level (a missing level means warning), "
    "line and message. This sandbox image has no Python, so ls, read_file, write_file, "
    "edit_file, glob and grep do not work here; do not call them. Never invent, reword or "
    "omit a diagnostic."
)


def build_model() -> tuple[BaseChatModel, DefaultAzureCredential | None] | None:
    """One client library, two endpoints. CI sets `AZURE_OPENAI_ENDPOINT`; a laptop does not.

    Samples 09 and 13 make the same split on the framework's own client. Returns the model and
    the credential to close, or ``None`` when the environment names an endpoint and then does
    not say which deployment to reach on it.
    """
    azure_endpoint = os.environ.get("AZURE_OPENAI_ENDPOINT")
    if not azure_endpoint:
        return (
            ChatOpenAI(
                model=os.environ.get("OPENAI_CHAT_MODEL") or DEFAULT_LOCAL_MODEL,
                base_url=os.environ.get("OPENAI_BASE_URL") or DEFAULT_LOCAL_BASE_URL,
                api_key=os.environ.get("OPENAI_API_KEY") or LOCAL_API_KEY_PLACEHOLDER,  # pyright: ignore[reportArgumentType]
            ),
            None,
        )
    env = require_env_vars(AZURE_MODEL_VARS)
    if env is None:
        return None
    from azure.identity.aio import DefaultAzureCredential, get_bearer_token_provider

    credential = DefaultAzureCredential()
    return (
        AzureChatOpenAI(
            azure_endpoint=azure_endpoint,
            azure_deployment=env["AZURE_OPENAI_CHAT_MODEL"],
            api_version=AZURE_API_VERSION,
            # The async half of the pair: the agent is awaited, and a synchronous provider
            # would mint its token on the event loop's thread.
            azure_ad_async_token_provider=get_bearer_token_provider(credential, AZURE_SCOPE),
        ),
        credential,
    )


def execute_results(reply: dict[str, object]) -> list[str]:
    """Everything the `execute` tool returned during `reply`, in the order it came back.

    A `ToolMessage` is written by the framework beside the call that asked for it; a model can
    say a compiler reported something, but it cannot put that sentence here without the
    compiler.
    """
    messages = reply.get("messages", [])
    assert isinstance(messages, list)
    return [
        str(message.content)
        for message in messages
        if isinstance(message, ToolMessage) and message.name == EXECUTE_TOOL
    ]


def final_reply(reply: dict[str, object]) -> str:
    """The last thing the model said, which is its own account of the results below it."""
    messages = reply.get("messages", [])
    assert isinstance(messages, list)
    for message in reversed(messages):
        if isinstance(message, AIMessage) and message.content:
            return str(message.content)
    return ""


async def run() -> int:
    """Wire the stack, run one turn, and take the container down again."""
    env = require_env_vars(SANDBOX_VARS)
    if env is None:
        return 2
    configured = build_model()
    if configured is None:
        return 2
    model, credential = configured

    backend = DockerSandboxBackend(DockerSandboxConfig())
    # Below the router's default `microvm` floor; opted down explicitly.
    router = SandboxRouter([backend], min_isolation=Isolation.CONTAINER)

    # Closed egress: the spec names no host, so the container runs with no network. The
    # template uses no modules, so nothing needs restoring and the compile completes offline.
    spec = deepagents_spec(env["BICEP_SANDBOX_IMAGE"])
    key = SandboxKey(scope=SCOPE, thread_id=THREAD_ID, agent_dir=AGENT_DIR)
    # Refuses here, before any agent exists, if the backend cannot serve the spec.
    sandbox = MafSandbox(router, key, spec)

    try:
        # The host puts the file in the sandbox. Deep Agents' `write_file` would first run a
        # Python preflight in the guest, and this image has none; the adapter's own upload does not.
        (uploaded,) = await sandbox.aupload_files(
            [(BICEP_FILE, (Path(__file__).parent / BICEP_FILE).read_bytes())]
        )
        if uploaded.error is not None:
            print(f"Upload of {BICEP_FILE} refused: {uploaded.error}", file=sys.stderr)
            return 2

        agent = create_deep_agent(
            model=model,
            system_prompt=INSTRUCTIONS.format(base=spec.work_dir),
            backend=sandbox,
        )
        reply = await agent.ainvoke(
            {
                "messages": [
                    HumanMessage(f"Validate {BICEP_FILE} and list every diagnostic you get back.")
                ]
            },
            config={"configurable": {"thread_id": THREAD_ID}},
        )
        # Quoted first, because the reply and the block below share one stream and the live
        # check trusts the `[measured]` tag completely.
        print(quoted(final_reply(reply)))

        # The compiler's own words, printed from the tool results rather than from the reply.
        compiles = [result for result in execute_results(reply) if _DIAGNOSTIC in result]
        print()
        print(
            evidence(
                "Diagnostics as execute returned them",
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
