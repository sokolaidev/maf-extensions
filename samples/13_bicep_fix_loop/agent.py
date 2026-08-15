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
#     # The async HTTP transport `azure.identity.aio.DefaultAzureCredential` needs, which
#     # `azure-identity` alone does not pull in. Samples 05 and 09 declare it for the same
#     # reason: without it the Azure path fails on import, before the model is ever reached.
#     "azure-core[aio]",
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

#: The two structural faults in `main.bicep`, by the rule id the compiler reports for each. The
#: file is sample 05's, unedited, so these are the faults that sample already reports.
TRACKED_FAULTS = ("no-unused-params", "BCP035")

#: The tool the model is expected to reach for, and the one this sample counts calls to.
BICEP_TOOL = "bicep_validate"

#: What `main.bicep` is *for*, and the one thing no other signal here protects. Deleting the
#: template and leaving an empty file satisfies every other check at once: it changed, it
#: reports no tracked fault, and it compiles clean. "Repaired" would then be the verdict on a
#: file with nothing left in it. Checked as text because the compiler's job is to say the file
#: is valid, never that it still does what it was written to do.
WORK_PRODUCT = ("Microsoft.Storage/storageAccounts", "output storageAccountId")

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


def faults_left(diagnostics: str) -> list[str]:
    """Which of the two tracked faults the **compiler** still reports in ``diagnostics``.

    Asked of the compiler rather than of the source text, and that is the whole point. A fault
    is fixed when the rule stops firing, not when some string disappears: `no-unused-params` is
    satisfied either by deleting `environmentName` *or* by using it, and a substring test for
    `param environmentName` calls the second one unfixed while the compiler calls the file
    clean. That is a real repair failed by its own harness.

    `main.bicep` reports a **third** diagnostic, `use-recent-api-versions`, which is not tracked
    here. It fires on how old the API version is rather than on the shape of the file, so what
    counts as fixed would move with the calendar. The model sees it and may address it; neither
    answer changes this tally. Anything *else* the compiler reports is a new fault the model
    introduced, and `scripts/check_live_fix_loop_sample.py` fails the run for it.

    Both tracked rules fire on the file as it ships, so a rule absent here was fixed.
    """
    return [rule for rule in TRACKED_FAULTS if rule.lower() in diagnostics.lower()]


def work_missing(source: str) -> list[str]:
    """Which pieces of the template the model did not leave behind — empty is the good answer."""
    return [piece for piece in WORK_PRODUCT if piece not in source]


def tool_calls(reply: object, name: str) -> int:
    """How many times ``reply`` actually called the tool ``name``.

    Read off the returned messages, because the container count cannot answer this. A turn that
    never validates leaves turn 1's container standing, so the count still reads 1 and the run
    looks like reuse while the second `acquire` never happened. This is the number that makes
    "the fix turn reached the same warm sandbox" a measurement rather than an inference.
    """
    return sum(
        1
        for message in getattr(reply, "messages", [])
        for content in message.contents
        if getattr(content, "type", None) == "function_call"
        and getattr(content, "name", None) == name
    )


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
        print(f"\n  bicep_validate calls in turn 1: {tool_calls(first, BICEP_TOOL)}")
        print(f"  containers after turn 1: {containers()}\n")

        print("== Turn 2: fix, then validate again ==\n")
        second = await agent.run(
            "Fix the faults those diagnostics point at, then validate again and say what changed.",
            session=session,
        )
        print(second.text)
        # Printed before the container count, because it is what gives that count its meaning: a
        # turn that never validated would leave turn 1's container standing and still read 1.
        print(f"\n  bicep_validate calls in turn 2: {tool_calls(second, BICEP_TOOL)}")
        print(f"  containers after turn 2: {containers()}\n")

        # A model that says "it validates clean now" is still narrating. Compile the file it
        # left behind, from here rather than from the conversation, and let that be the verdict
        # everything below is read off. This is also a **third** `acquire` on the same key,
        # which is why the container count is printed again: turn 2 finding the sandbox warm
        # was not a one-off.
        print("== What the compiler says about the file the model left ==\n")
        verdict = str(
            await bicep_validate.invoke(arguments={"files": [BICEP_FILE]}, skip_parsing=True)
        )
        print("\n".join(f"  {line}" for line in verdict.splitlines()))
        print(f"\n  containers after the check: {containers()}\n")

        # The work product. `changed` compares the file store with what went in — the one thing
        # the compiler cannot answer, since a model that edited nothing still compiles. The
        # tally comes from the diagnostics above rather than from the source text, so a fault
        # fixed by *using* the parameter counts as fixed, which is what the compiler thinks too.
        edited = await store.read(BICEP_FILE)
        source = edited if isinstance(edited, str) else edited.decode("utf-8")
        remaining = faults_left(verdict)
        fixed = [rule for rule in TRACKED_FAULTS if rule not in remaining]

        missing = work_missing(source)
        print("== The work product ==\n")
        print(f"  main.bicep changed: {source != original}")
        print(
            f"  storage account and output intact: {not missing}"
            + (f" — missing {'; '.join(missing)}" if missing else "")
        )
        print(f"  faults fixed:       {len(fixed)} — {'; '.join(fixed) or 'none'}")
        print(f"  faults remaining:   {len(remaining)} — {'; '.join(remaining) or 'none'}\n")
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
