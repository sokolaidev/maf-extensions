"""Two turns against one warm sandbox: validate, read the diagnostics, fix, validate again.

Every other sample runs one turn. `acquire` is get-or-create precisely so a model can iterate,
and until now nothing showed the second turn arriving to find its sandbox still there.

What the sample checks is the model's **work product**, not its narration: `main.bicep` is read
back out of the file store after the fix turn and compared with what went in, and the container
count comes from `docker ps -a`. A model can describe a fix it did not make; it cannot leave the
file changed without making one.

Needs a Docker-compatible engine and a model — Azure OpenAI in CI, a local Ollama server by
default. See this directory's README.
"""

# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "agent-framework-openai",
#     "azure-identity",
#     "maf-sandbox-bicep",
#     "maf-sandbox-docker",
#     "maf-sandbox>=0.14",
# ]
# ///

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path

from _scaffold import require_env_vars
from agent_framework import Agent, FileAccessProvider, InMemoryAgentFileStore
from agent_framework.openai import OpenAIChatCompletionClient
from maf_sandbox import Isolation, SandboxRouter
from maf_sandbox.maf import list_all_files, make_caller_context
from maf_sandbox_bicep import make_bicep_tools
from maf_sandbox_docker import DockerSandboxBackend, DockerSandboxConfig

SCOPE = "samples"
THREAD_ID = "13-fix-loop"
AGENT_DIR = "devops-engineer"
BICEP_FILE = "main.bicep"

#: Built from `images/bicep-sandbox` — the same guest samples 02 and 05 use, so the compiler and
#: the lint rule set are theirs and the diagnostics below are comparable with both.
IMAGE = os.environ.get("BICEP_SANDBOX_IMAGE") or "bicep-sandbox:local"

#: Sample 09's split, unchanged: one client class, two endpoints, branched on one variable.
DEFAULT_LOCAL_MODEL = "minimax-m3:cloud"
DEFAULT_LOCAL_BASE_URL = "http://localhost:11434/v1"
LOCAL_API_KEY_PLACEHOLDER = "ollama"

#: The labels `DockerSandboxBackend` stamps, and what `dispose_scope` selects on.
_LABEL_SCOPE = "maf-sandbox.scope"
_LABEL_THREAD = "maf-sandbox.thread"


def containers() -> int:
    """Containers Docker reports for this thread, **stopped ones included**."""
    result = subprocess.run(  # noqa: S603 - fixed argv, no shell, values from this file
        [
            "docker",
            "ps",
            "-a",
            "--quiet",
            "--filter",
            f"label={_LABEL_SCOPE}={SCOPE}",
            "--filter",
            f"label={_LABEL_THREAD}={THREAD_ID}",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return len(result.stdout.split())


def faults_left(source: str) -> list[str]:
    """Which of the file's two structural faults are still in ``source``.

    Read off the text rather than from the compiler, because this runs between turns and the
    point is what the *model wrote*. `main.bicep` declares `environmentName` and never uses it,
    and its `storageAccount` has no `sku`; fixing either is a real edit and the sample says
    which happened rather than requiring both.

    The file reports a **third** diagnostic, `use-recent-api-versions`, and it is deliberately
    not tracked here. That one fires on how old the API version is rather than on the shape of
    the file, so what counts as fixed changes with the calendar. The model sees it and may
    address it; the tally stays out of it, and so does the live check.

    Each entry names a fault, so the same string reads correctly under "fixed" and "remaining".
    """
    remaining: list[str] = []
    if "param environmentName" in source:
        remaining.append("no-unused-params: unused environmentName")
    if "sku:" not in source:
        remaining.append("BCP035: storageAccount without sku")
    return remaining


def build_client() -> tuple[OpenAIChatCompletionClient, object | None] | None:
    """One client class, two endpoints. CI sets `AZURE_OPENAI_ENDPOINT`; a laptop does not.

    Sample 09's split, unchanged, down to reporting a half-configured Azure run rather than
    letting it fail later. Returns the client and the credential to close, or ``None`` when the
    environment names an endpoint and then does not say which model to reach on it.
    """
    azure_endpoint = os.environ.get("AZURE_OPENAI_ENDPOINT")
    if not azure_endpoint:
        return (
            OpenAIChatCompletionClient(
                model=os.environ.get("OPENAI_CHAT_MODEL") or DEFAULT_LOCAL_MODEL,
                base_url=os.environ.get("OPENAI_BASE_URL") or DEFAULT_LOCAL_BASE_URL,
                api_key=LOCAL_API_KEY_PLACEHOLDER,
            ),
            None,
        )
    env = require_env_vars(("AZURE_OPENAI_CHAT_MODEL",))
    if env is None:
        return None
    from azure.identity.aio import DefaultAzureCredential

    credential = DefaultAzureCredential()
    return (
        OpenAIChatCompletionClient(
            model=env["AZURE_OPENAI_CHAT_MODEL"],
            azure_endpoint=azure_endpoint,
            credential=credential,
        ),
        credential,
    )


async def run() -> int:
    """Wire the stack once, run two turns through one session, and report what moved."""
    original = (Path(__file__).parent / BICEP_FILE).read_text(encoding="utf-8")
    store = InMemoryAgentFileStore()
    await store.write(BICEP_FILE, original)

    backend = DockerSandboxBackend(DockerSandboxConfig())
    router = SandboxRouter([backend], min_isolation=Isolation.CONTAINER)
    context = make_caller_context(list_all_files, lambda: SCOPE, lambda: THREAD_ID)
    tools = make_bicep_tools(router, store, AGENT_DIR, context, image=IMAGE)
    if not tools:
        print("No sandbox backend: bicep_validate was not attached.", file=sys.stderr)
        return 2
    # `make_bicep_tools` returns exactly `[bicep_validate]`. Bound here because this sample is
    # the one that also calls it directly, from the program rather than through the model.
    bicep_validate = tools[0]

    configured = build_client()
    if configured is None:
        return 2
    client, credential = configured
    try:
        agent = Agent(
            client=client,
            name=AGENT_DIR,
            instructions=(
                "You validate and repair Azure Bicep. Always call bicep_validate rather than "
                "judging a file by reading it, and report exactly the diagnostics it returns. "
                "When asked to fix something, edit the file first with file_access_replace — "
                "file_access_write refuses to overwrite a file that already exists — then "
                "call bicep_validate again on the edited file. Keep every answer short."
            ),
            tools=tools,
            # Gives the model `file_access_read`/`_write`/`_replace` over the same store
            # `bicep_validate` reads from — without them it can describe a fix and not make one.
            # **Both** approval gates are off, and both are needed: the read tools have their
            # own. Leave `disable_readonly_tool_approval` at its default and the fix turn stops
            # dead on the model's first `file_access_read`, returning no text and no edit, since
            # this program has no human in it to approve anything.
            context_providers=[
                FileAccessProvider(
                    store,
                    disable_readonly_tool_approval=True,
                    disable_write_tool_approval=True,
                )
            ],
        )

        # One session across both turns, so the second carries the first's diagnostics. This is
        # the whole mechanism: a fresh session would make turn two a stranger to turn one.
        session = agent.create_session()

        print("== Turn 1: validate ==\n")
        first = await agent.run(
            f"Validate {BICEP_FILE}. List each diagnostic as one line: rule id, severity, line.",
            session=session,
        )
        print(first.text)
        print(f"\n  containers after turn 1: {containers()}\n")

        print("== Turn 2: fix, then validate again ==\n")
        second = await agent.run(
            "Fix the faults those diagnostics point at, then validate again and say what changed.",
            session=session,
        )
        print(second.text)
        print(f"\n  containers after turn 2: {containers()}\n")

        # The model's work product, not its account of it.
        edited = await store.read(BICEP_FILE)
        source = edited if isinstance(edited, str) else edited.decode("utf-8")
        changed = source != original
        remaining = faults_left(source)
        fixed = [f for f in faults_left(original) if f not in remaining]

        print("== What the file actually says now ==\n")
        print(f"  main.bicep changed: {changed}")
        print(f"  faults fixed:       {len(fixed)} — {'; '.join(fixed) or 'none'}")
        print(f"  faults remaining:   {len(remaining)} — {'; '.join(remaining) or 'none'}\n")

        # A model that says "it validates clean now" is still narrating. Compile the file it
        # left behind, from here rather than from the conversation, and print what the compiler
        # says. This is also a **third** `acquire` on the same key, which is why the container
        # count is printed again: turn 2 finding the sandbox warm was not a one-off.
        print("== Independent check: compile what the model left ==\n")
        verdict = await bicep_validate.invoke(arguments={"files": [BICEP_FILE]}, skip_parsing=True)
        print("\n".join(f"  {line}" for line in str(verdict).splitlines()))
        print(f"\n  containers after the check: {containers()}\n")
    finally:
        disposed = await router.dispose_scope(SCOPE, THREAD_ID)
        if credential is not None:
            await credential.close()

    print(
        f"Disposed {disposed} sandbox(es) after 2 turns and a check. "
        f"Containers left: {containers()}."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
