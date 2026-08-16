"""Author, validate, fix — two turns against one warm sandbox.

Every other sample runs one turn against a file that was already there. Here the store starts
**empty**: turn 1 writes `main.bicep` from a written brief and validates what it wrote, turn 2
repairs what the compiler reported, and the program compiles the file itself at both ends.
`acquire` is get-or-create precisely so a model can iterate like this, and until now nothing
showed the second turn arriving to find its sandbox still there.

The brief is what makes the diagnostics predictable without scripting them. It asks for a
parameter that a later change will use, and for no `sku` yet because the tier is undecided —
both ordinary things to write, and between them they produce the two faults turn 2 has to
repair. The model is never told they are faults; the compiler is what says so.

What the sample checks is the model's **work product**, not its narration: `main.bicep` is read
back out of the file store after each turn, and the container count comes from `docker ps -a`. A
model can describe a fix it did not make; it cannot leave the file changed without making one.

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
import re
import subprocess
import sys
from typing import TYPE_CHECKING

from _scaffold import MEASURED, quoted, require_env_vars, tool_results
from agent_framework import Agent, FileAccessProvider, InMemoryAgentFileStore
from agent_framework.openai import OpenAIChatCompletionClient
from maf_sandbox import Isolation, SandboxRouter
from maf_sandbox.maf import list_all_files, make_caller_context
from maf_sandbox_bicep import make_bicep_tools
from maf_sandbox_docker import DockerSandboxBackend, DockerSandboxConfig

if TYPE_CHECKING:
    # The runtime import stays inside `build_client`, so a local run loads no Azure SDK at all.
    from azure.identity.aio import DefaultAzureCredential

SCOPE = "samples"
THREAD_ID = "13-fix-loop"
AGENT_DIR = "devops-engineer"
BICEP_FILE = "main.bicep"

#: What turn 1 is asked to write. Every clause is a plain requirement rather than a planted
#: fault, and two of them have consequences the compiler objects to: `environmentName` is
#: declared for a later change and so goes unused, and the omitted `sku` is required. Those are
#: `TRACKED_FAULTS` below. Naming the faults here instead would script the repair, which #304
#: rules out — the point is a model reacting to real diagnostics.
SPEC = (
    "three parameters — `location` defaulting to the resource group's location, "
    "`storageAccountName`, and `environmentName` which a later change will use; "
    "one `Microsoft.Storage/storageAccounts` resource at apiVersion 2023-01-01, "
    "kind StorageV2, named from the parameter, with no `sku` yet because the tier is "
    "still being decided; and an output `storageAccountId` giving the resource's id"
)

#: The two faults the brief implies, by the rule id the compiler reports for each. Samples 01,
#: 02, 05 and 09 report the same pair from the `main.bicep` they check in, which is why the
#: brief describes that file: the diagnostics stay comparable across all five.
TRACKED_FAULTS = ("no-unused-params", "BCP035")

#: The tool the model is expected to reach for, and the one this sample counts calls to.
BICEP_TOOL = "bicep_validate"

#: How `_run_phase` renders a compile: one line per phase, at the start of the line. Both
#: must be present for a result to be one the sandbox produced.
_PHASES = re.compile(r"^build\(.*^lint\(", re.MULTILINE | re.DOTALL)

#: What the brief asks for by name, and the one thing no other signal here protects:
#: emptying the file changes it, reports no tracked fault, and compiles clean. Patterns rather
#: than substrings, because the model writes this file and its spacing is the model's to
#: choose. `environmentName` is deliberately absent: deleting it is a valid repair of
#: `no-unused-params`, while hardcoding what `location` and `storageAccountName` supply is
#: not a repair of anything.
WORK_PRODUCT = (
    ("Microsoft.Storage/storageAccounts", re.compile(r"Microsoft\.Storage/storageAccounts")),
    ("output storageAccountId", re.compile(r"output\s+storageAccountId")),
    ("param location", re.compile(r"param\s+location\b")),
    ("param storageAccountName", re.compile(r"param\s+storageAccountName\b")),
)

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


def containers() -> list[str]:
    """Ids Docker reports for this thread, **stopped ones included**, in a stable order."""
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
    return sorted(result.stdout.split())


def counted(ids: list[str]) -> str:
    """A count and the ids behind it, because the count alone is the weaker claim.

    "One container exists at this instant" is not "the same container served both turns". A
    backend that force-removes a sandbox — an exec timeout does exactly that — leaves the next
    `acquire` to create a fresh one, and every checkpoint still reads 1. Printing the id makes
    the sample measure what #304 asked to see.
    """
    return f"{len(ids)} ({', '.join(ids) or 'none'})"


def faults_left(diagnostics: str) -> list[str]:
    """Which of the two tracked faults the **compiler** still reports in ``diagnostics``.

    Called twice: once on what the compiler told the model about the file it just wrote, and
    once on the program's own compile of the file turn 2 left. The difference between the two is
    what was fixed. Read from the diagnostics and not the source, because `no-unused-params` is
    satisfied by *using* `environmentName` as well as by deleting it.

    `use-recent-api-versions` is deliberately untracked: it fires on the age of the API version,
    so what counts as fixed would move with the calendar. Anything else the compiler reports is
    a fault the model introduced, which `scripts/check_live_fix_loop_sample.py` rejects.
    """
    return [rule for rule in TRACKED_FAULTS if rule.lower() in diagnostics.lower()]


def work_missing(source: str) -> list[str]:
    """Which pieces of the template the model did not leave behind — empty is the good answer."""
    return [label for label, pattern in WORK_PRODUCT if not pattern.search(source)]


def suppressed(source: str) -> list[str]:
    """Tracked rules the file silences with a `#disable-next-line` directive.

    `faults_left` asks the compiler, which is the right question — and a directive makes the
    compiler stop asking. Two comment lines otherwise satisfy every signal here at once: the
    file changed, the template is intact, and both phases come back clean. Suppressing a
    diagnostic is a legitimate thing to do in real Bicep; it is not a repair, and turn 2's
    prompt asks for one.
    """
    directives = re.findall(r"^[^\S\n]*#disable-next-line[^\S\n]+(.+)$", source, re.MULTILINE)
    named = {token for line in directives for token in line.split()}
    return [rule for rule in TRACKED_FAULTS if rule in named]


def validations(reply: object, name: str) -> int:
    """How many times ``reply`` called ``name`` and got a compile back.

    The container count cannot answer this: a turn that never validates leaves the previous
    turn's container standing, so the count still reads 1 while no second `acquire` happened.

    Counting *requests* would not answer it either. `bicep_validate` returns an error string
    without touching the sandbox when no conversation is bound, when a name has the wrong
    suffix, and when a name is not in its file listing — so a turn whose only validator call
    was rejected would score 1 having acquired nothing. A result whose *lines* start with both
    compiler phases is one that reached the sandbox — anchored, because a rejection echoes the
    caller's own filename back, and a name carrying those markers would otherwise count.
    """
    return sum(1 for result in tool_results(reply, name) if _PHASES.search(result))


async def read_or_empty(store: InMemoryAgentFileStore, name: str) -> str:
    """``name``'s contents, or ``""`` when the model never created it.

    The store starts empty, so "no such file" is a real outcome here and not an error: a turn 1
    that wrote nothing is exactly what this sample has to be able to report. `read` answers
    `None` for it; the `except` is for a store that raises instead.
    """
    try:
        content = await store.read(name)
    except Exception:  # noqa: BLE001 - any failure to read means nothing was authored
        return ""
    return content or ""


def build_client() -> tuple[OpenAIChatCompletionClient, DefaultAzureCredential | None] | None:
    """One client class, two endpoints. CI sets `AZURE_OPENAI_ENDPOINT`; a laptop does not.

    Sample 09 makes the same split inline. Factored out here, so the credential handed back is
    named rather than inferred. Returns the client and that credential to close, or ``None`` when
    the environment names an endpoint and then does not say which model to reach on it.
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
    # Empty on purpose: `main.bicep` does not exist until turn 1 writes it. Nothing here can
    # smuggle in the file the sample is about, which is what "the model authored it" has to mean.
    store = InMemoryAgentFileStore()

    backend = DockerSandboxBackend(DockerSandboxConfig())
    router = SandboxRouter([backend], min_isolation=Isolation.CONTAINER)
    context = make_caller_context(list_all_files, lambda: SCOPE, lambda: THREAD_ID)
    tools = make_bicep_tools(router, store, AGENT_DIR, context, image=IMAGE)
    if not tools:
        print("No sandbox backend: bicep_validate was not attached.", file=sys.stderr)
        return 2

    # Every count below is "containers for this thread", so one left by a run that was killed
    # before disposing makes all four read 2 and the footer read 1 left behind — honest, and
    # unreadable. This is also the program's first call to Docker: the guard above attaches a
    # tool whenever a backend is *registered*, which probes nothing, so an unreachable engine
    # surfaces here and is answered here.
    try:
        stale = containers()
    except (OSError, subprocess.CalledProcessError) as exc:
        # `containers()` captures output, so the engine's own explanation is on the exception
        # rather than in the message — and `str(CalledProcessError)` prints only the exit code.
        # It is also the part worth reading: a refused socket and a permission denied both fail
        # here and only Docker can tell them apart.
        detail = (getattr(exc, "stderr", None) or "").strip() or str(exc)
        print(
            "`docker ps` failed, and this sample cannot run without it:",
            *(f"  {line}" for line in detail.splitlines()),
            sep="\n",
            file=sys.stderr,
        )
        return 2
    if stale:
        print(
            f"{len(stale)} container(s) already exist for {SCOPE}/{THREAD_ID}, left by a run that "
            f"did not dispose. Remove them first:\n"
            f"  docker rm -f $(docker ps -aq --filter label={_LABEL_SCOPE}={SCOPE} "
            f"--filter label={_LABEL_THREAD}={THREAD_ID})",
            file=sys.stderr,
        )
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
                "You write, validate and repair Azure Bicep. Create a file with "
                "file_access_write and edit an existing one with file_access_replace — "
                "file_access_write refuses to overwrite a file that already exists. Always call "
                "bicep_validate rather than judging a file by reading it, and report exactly "
                "the diagnostics it returns. Write or edit the file first, then validate what "
                "is on disk. When you are asked to repair a file, anything validation then "
                "reports that your repair introduced is part of that repair: fix it and "
                "validate again before you answer. Keep every answer short."
            ),
            tools=tools,
            # Gives the model `file_access_read`/`_write`/`_replace` over the same store
            # `bicep_validate` reads from — without them it can describe a fix and not make one.
            # Both approval gates are off because there is no human here to answer either one;
            # the README says what leaving the read gate on does to a fix turn.
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

        print("== Turn 1: author main.bicep, then validate it ==\n")
        first = await agent.run(
            f"Write {BICEP_FILE} with {SPEC}. Then validate it and list each diagnostic as one "
            "line: rule id, severity, line. Leave the file as you wrote it for now — the next "
            "message decides what to change.",
            session=session,
        )
        print(quoted(first.text))
        print(
            f"\n{MEASURED}validations that reached the sandbox in turn 1: "
            f"{validations(first, BICEP_TOOL)}"
        )
        print(f"{MEASURED}containers after turn 1: {counted(containers())}\n")

        # The baseline turn 2's work is measured against, compiled here rather than lifted out
        # of turn 1's own tool call. That distinction is the whole point: the model may validate
        # a draft, edit it and validate again, and then its *first* result describes a file that
        # no longer exists — crediting turn 2 with faults turn 1 had already fixed. Compiling
        # the snapshot makes the diagnostics correspond to `authored` by construction.
        #
        # It costs one more `acquire`, on the same key, which is why the count is printed again.
        authored = await read_or_empty(store, BICEP_FILE)
        baseline = str(
            await bicep_validate.invoke(arguments={"files": [BICEP_FILE]}, skip_parsing=True)
        )
        as_authored = faults_left(baseline)

        print("== What the compiler says about the file turn 1 wrote ==\n")
        print(quoted("\n".join(f"  {line}" for line in baseline.splitlines())))
        print(
            f"\n{MEASURED}tracked faults in the authored file: {len(as_authored)} — "
            f"{'; '.join(as_authored) or 'none'}"
        )
        print(f"{MEASURED}containers after the baseline compile: {counted(containers())}\n")

        print("== Turn 2: fix, then validate again ==\n")
        second = await agent.run(
            "Fix the faults those diagnostics point at, then validate again and say what "
            "changed. Leave the file reporting nothing it did not report before — a repair "
            "that trades one diagnostic for another has not finished.",
            session=session,
        )
        print(quoted(second.text))
        # Printed before the container count, because it is what gives that count its meaning: a
        # turn that never reached the sandbox would leave turn 1's container standing and
        # still read 1.
        print(
            f"\n{MEASURED}validations that reached the sandbox in turn 2: "
            f"{validations(second, BICEP_TOOL)}"
        )
        print(f"{MEASURED}containers after turn 2: {counted(containers())}\n")

        # A model that says "it validates clean now" is still narrating. Compile the file it
        # left behind, from here rather than from the conversation, and let that be the verdict
        # everything below is read off. This is the **fourth** `acquire` on the same key, which
        # is why the container count is printed again: turn 2 finding the sandbox warm was not
        # a one-off.
        print("== What the compiler says about the file the model left ==\n")
        verdict = str(
            await bicep_validate.invoke(arguments={"files": [BICEP_FILE]}, skip_parsing=True)
        )
        print(quoted("\n".join(f"  {line}" for line in verdict.splitlines())))
        print(f"\n{MEASURED}containers after the check: {counted(containers())}\n")

        # The work product, and the two questions the compiler cannot answer: did turn 1 write a
        # file at all, and did turn 2 change it. A model that edited nothing still compiles.
        # `fixed` is the difference between the two compiles, so it counts what *this run* did
        # rather than assuming the file arrived with both faults in it.
        source = await read_or_empty(store, BICEP_FILE)
        remaining = faults_left(verdict)
        fixed = [rule for rule in as_authored if rule not in remaining]

        missing = work_missing(source)
        silenced = suppressed(source)
        print("== The work product ==\n")
        print(f"{MEASURED}main.bicep authored in turn 1: {bool(authored.strip())}")
        print(f"{MEASURED}main.bicep changed by turn 2:  {source != authored}")
        print(
            f"{MEASURED}storage account and output intact: {not missing}"
            + (f" — missing {'; '.join(missing)}" if missing else "")
        )
        print(
            f"{MEASURED}tracked rules suppressed: {len(silenced)} — {'; '.join(silenced) or 'none'}"
        )
        print(f"{MEASURED}faults fixed:       {len(fixed)} — {'; '.join(fixed) or 'none'}")
        print(
            f"{MEASURED}faults remaining:   {len(remaining)} — {'; '.join(remaining) or 'none'}\n"
        )
    finally:
        disposed = await router.dispose_scope(SCOPE, THREAD_ID)
        if credential is not None:
            await credential.close()

    print(
        f"{MEASURED}Disposed {disposed} sandbox(es) after 2 turns and a check. "
        f"Containers left: {len(containers())}."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
