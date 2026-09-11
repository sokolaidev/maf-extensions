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
"""

# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "deepagents",
#     "langchain-core",
#     "langchain-openai",
#     "maf-sandbox-deepagents",
#     "maf-sandbox-docker",
#     "maf-sandbox>=0.37",
# ]
# ///

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

from _scaffold import MEASURED, evidence, installed_versions, quoted, require_env_vars
from deepagents import create_deep_agent
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_openai import ChatOpenAI
from maf_sandbox import Isolation, SandboxKey, SandboxRouter
from maf_sandbox_deepagents import MafSandbox, deepagents_spec
from maf_sandbox_docker import DockerSandboxBackend, DockerSandboxConfig

# A sandbox is keyed by the caller's scope, thread and agent directory. A host reads the first
# two from its own request context — a user/tenant and a conversation. This program serves
# exactly one request, so they are constants here, but they are still named rather than
# inlined: they belong to the request, not to the agent.
SCOPE = "samples"
THREAD_ID = "17-docker-deepagents-bicep"
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

#: Everything the chat model needs. `OPENAI_BASE_URL` is optional, so it is read separately:
#: any OpenAI-compatible endpoint serves, a local server included.
MODEL_VARS = ("OPENAI_API_KEY", "OPENAI_CHAT_MODEL")

#: The compiler's SARIF output, as `bicep_validate` reads it: the plain format prints only the
#: errors once there is one, and this file has one, so the two warnings beside it would go unseen.
BUILD = f"bicep build {BICEP_FILE} --no-restore --diagnostics-format sarif"
LINT = f"bicep lint {BICEP_FILE} --diagnostics-format sarif"

INSTRUCTIONS = (
    "You validate Azure Bicep with the compiler, never by reading the file yourself. "
    f"The file to validate is {BICEP_FILE}, already in your working directory. "
    f"Use the execute tool only: run `{BUILD}` and then `{LINT}`, and report exactly the "
    "diagnostics in the SARIF they print — ruleId, level (a missing level means warning), "
    "line and message. This sandbox image has no Python, so ls, read_file, write_file, "
    "edit_file, glob and grep do not work here; do not call them. Never invent, reword or "
    "omit a diagnostic."
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
    env = require_env_vars(SANDBOX_VARS + MODEL_VARS)
    if env is None:
        return 2

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
        # The host puts the file in the sandbox. Deep Agents' `write_file` would run Python in
        # the guest to do it, and this image has none; the adapter's own upload does not.
        (uploaded,) = await sandbox.aupload_files(
            [(BICEP_FILE, (Path(__file__).parent / BICEP_FILE).read_bytes())]
        )
        if uploaded.error is not None:
            print(f"Upload of {BICEP_FILE} refused: {uploaded.error}", file=sys.stderr)
            return 2

        agent = create_deep_agent(
            model=ChatOpenAI(
                model=env["OPENAI_CHAT_MODEL"],
                api_key=env["OPENAI_API_KEY"],  # pyright: ignore[reportArgumentType]
                base_url=os.environ.get("OPENAI_BASE_URL"),
            ),
            system_prompt=INSTRUCTIONS,
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

    return 0


if __name__ == "__main__":
    print(installed_versions())
    raise SystemExit(asyncio.run(run()))
