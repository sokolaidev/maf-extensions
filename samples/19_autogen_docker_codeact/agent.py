"""One deterministic turn of an AutoGen agent that computes with code instead of with itself.

Sample 06's task, image and router under an AutoGen agent::

    autogen  ->  the sample's CodeExecutor  ->  maf_sandbox (router)  ->  maf_sandbox_docker  ->  the container

The executor this file writes is the sample: `autogen_core.code_executor.CodeExecutor` acquiring
from a `maf_sandbox` router, with the same Docker backend and image sample 06 runs. Architecture,
give-ups and model wiring are this directory's README's to tell; the one caller-relevant constraint
here is that the router is configured at run() and the sandbox is disposed there, not by AutoGen.
"""

# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "autogen-agentchat>=0.7.5,<0.8",
#     "autogen-core>=0.7.5,<0.8",
#     "autogen-ext[openai]>=0.7.5,<0.8",
#     # The async HTTP transport `azure.identity.aio.DefaultAzureCredential` needs, which
#     # `azure-identity` alone does not pull in. Samples 05, 09 and 13 declare it for the same
#     # reason: without it the Azure path fails on import, before the model is ever reached.
#     "azure-core[aio]",
#     "azure-identity",
#     "maf-sandbox-docker",
#     "maf-sandbox>=0.43",
# ]
# ///

from __future__ import annotations

import asyncio
import os
import re
from typing import TYPE_CHECKING

from _scaffold import MEASURED, evidence, installed_versions, quoted, require_env_vars
from autogen_agentchat.agents import AssistantAgent
from autogen_agentchat.base import TaskResult
from autogen_agentchat.messages import TextMessage, ToolCallExecutionEvent
from autogen_core import CancellationToken
from autogen_core.code_executor import CodeBlock, CodeExecutor, CodeResult
from autogen_core.models import ChatCompletionClient, ModelFamily, ModelInfo
from autogen_ext.models.openai import AzureOpenAIChatCompletionClient, OpenAIChatCompletionClient
from autogen_ext.tools.code_execution import PythonCodeExecutionTool
from maf_sandbox import (
    BoundedExec,
    Capability,
    Egress,
    EgressRule,
    ExecResult,
    HttpMethod,
    Isolation,
    SandboxExecOutputLimitExceeded,
    SandboxKey,
    SandboxRouter,
    SandboxSpec,
)
from maf_sandbox_docker import DockerSandboxBackend, DockerSandboxConfig

if TYPE_CHECKING:
    # The runtime import stays inside `build_model`, so a local run loads no Azure SDK at all.
    from azure.identity.aio import DefaultAzureCredential

# A sandbox is keyed by the caller's scope, thread and agent directory. A host reads the first
# two from its own request context — a user/tenant and a conversation. This program serves
# exactly one request, so they are constants here, but they are still named rather than
# inlined: they belong to the request, not to the agent.
SCOPE = "samples"
THREAD_ID = "19-autogen-docker-codeact"
# An identifier, because AutoGen refuses anything else as an agent name — sample 06's
# `data-analyst` would not construct here.
AGENT_ID = "data_analyst"

#: A standard MCR devcontainer image at Python 3.13; see this directory's README for why.
CODEACT_IMAGE = "mcr.microsoft.com/devcontainers/python:3.13-bookworm"

#: The workload name the spec carries. Part of the sandbox's identity rather than a label, so a
#: host that also runs sample 06's kind never serves the two from one container.
KIND = "autogen-codeact"

TASK = (
    "Write a Python program that computes the 100th Fibonacci number, with "
    "F(0) = 0 and F(1) = 1, and prints just the integer. Run it and tell me "
    "exactly what it printed."
)

INSTRUCTIONS = (
    "You answer computational questions by writing and running Python with the CodeExecutor "
    "tool, never by computing them yourself. Always call the tool, and report exactly what it "
    "returned — do not paraphrase, round, or recompute the number it printed."
)

#: The name `PythonCodeExecutionTool` registers itself under, and the heading this sample prints
#: its evidence under. What it returned is what the live check reads: the model writes the prose
#: around it, the interpreter writes this.
EXECUTOR_TOOL = "CodeExecutor"
EXECUTOR_TOOL_HEADING = "Program output as CodeExecutor returned it"

#: How the executor renders a run: a `stdout:`, `stderr:` or `exit code:` section, each at the
#: start of a line — the same shape `maf_sandbox_codeact` renders and the shared checker reads.
#: A call refused before reaching the interpreter comes back as an `Error:` string carrying none
#: of them, so this is what separates a program that ran from a request that never did.
_RAN = re.compile(r"^(stdout|stderr|exit code):", re.MULTILINE)

#: `python3` is the only language the guest is asked for, and any other block is refused before
#: a sandbox is acquired. `PythonCodeExecutionTool` always sends `python`; the contract allows
#: other callers, and this is where they are told no.
_LANGUAGES = frozenset({"python", "py"})

#: The execution bound — the defaults the Deep Agents adapter carries. A guest that has not
#: answered in two minutes has not answered, and a program that prints forever is stopped by the
#: byte budget before the host ever buffers the whole response.
EXEC_TIMEOUT_SECONDS = 120.0
MAX_OUTPUT_BYTES = 1_048_576

#: The bound on the unclean disposal every lost execution queues — timeout, overflow,
#: cancellation, any other failure the result did not come back from — and the lifecycle
#: release. Short, because the container is a local `docker rm -f` away, and a budget a
#: healthy engine answers well inside.
CLEANUP_TIMEOUT_SECONDS = 30.0

#: The Azure OpenAI API version this client speaks. `AzureOpenAIChatCompletionClient` requires
#: one and has no default; the value is the one sample 17's `AzureChatOpenAI` passes, so the two
#: samples reach one deployment over one surface.
AZURE_API_VERSION = "2024-12-01-preview"

#: What a token for an Azure OpenAI deployment is minted against.
AZURE_SCOPE = "https://cognitiveservices.azure.com/.default"

#: What the Azure road needs beyond the endpoint that selects it. No key: auth is
#: `DefaultAzureCredential`, which an `az login` session or a federated CI credential satisfies.
AZURE_MODEL_VARS = ("AZURE_OPENAI_CHAT_MODEL",)

#: Local-Ollama defaults, as samples 09 and 13 carry them. The model defaults so a running
#: `ollama serve` is the whole of configuration; the base URL is Ollama's OpenAI-compatible
#: endpoint; the key is a non-empty placeholder the server ignores — the client requires
#: *something* here even for a keyless server, and a local one never reads it.
DEFAULT_LOCAL_MODEL = "minimax-m3:cloud"
DEFAULT_LOCAL_BASE_URL = "http://localhost:11434/v1"
LOCAL_API_KEY_PLACEHOLDER = "ollama"


def _model_info(family: str) -> ModelInfo:
    # AutoGen knows only OpenAI's model names, and a deployment named `gpt-5.4` — or a local
    # `minimax-m3:cloud` — is not one. Both roads therefore state the capabilities themselves,
    # and `function_calling` has to be true: the agent has to call the tool.
    return ModelInfo(
        vision=False,
        function_calling=True,
        json_output=True,
        structured_output=True,
        family=family,
    )


class SandboxCodeExecutor(CodeExecutor):
    """AutoGen's code-execution contract, with the sandbox a `maf_sandbox` router serves.

    `execute_code_blocks` acquires the conversation's sandbox with `router.acquire` — the same
    public road the packaged kinds take — and runs each block as ``python3 -c <code>`` through
    `exec_bounded`, so the program travels in argv with no shell in between and its output is
    bounded by the host before it is ever buffered. The spec requires `Capability.EXEC` and
    nothing else, and a sandbox without `exec_bounded` is refused rather than run unbounded.

    Two things this executor deliberately is not. Not a `Component` — a router is not
    serialisable config, so `dump_component()` raises `NotImplementedError`, and so does the
    tool's and an agent holding the tool. And not a call boundary — there is no `enter_call`,
    which is what leaves the guest unadmitted; see this directory's README. Concurrent calls are
    serialised here rather than refused, because nothing upstream of this class can be relied on
    to stop the model asking for two.
    """

    def __init__(self, router: SandboxRouter, key: SandboxKey, spec: SandboxSpec) -> None:
        self._router = router
        self._key = key
        self._spec = spec
        self._one_at_a_time = asyncio.Lock()

    async def start(self) -> None:
        """Nothing to start: the router creates the sandbox on the first execution."""

    async def stop(self) -> None:
        """Release what the executor holds: the sandbox it acquired, with refusal on failure."""
        await self._router.dispose_unclean(
            self._key, kind=self._spec.kind, timeout=CLEANUP_TIMEOUT_SECONDS
        )

    async def restart(self) -> None:
        """The same release ``stop`` performs, for a reset — with the same AutoGen caveat."""
        await self._router.dispose_unclean(
            self._key, kind=self._spec.kind, timeout=CLEANUP_TIMEOUT_SECONDS
        )

    async def execute_code_blocks(
        self, code_blocks: list[CodeBlock], cancellation_token: CancellationToken
    ) -> CodeResult:
        """Run each block in order as ``python3 -c <code>`` inside one acquired sandbox.

        A block in any language but Python stops the list at that block — preceding blocks
        have run, as both of AutoGen's reference executors do. The sandbox is acquired only
        when a runnable block is reached, so a list that begins with an unsupported language
        never pays for one.

        Held against `_one_at_a_time`, so a model response carrying two calls runs them one
        after another over the single sandbox this key admits.  Waiting for that lock is part of
        the call, so `cancellation_token` reaches the wait as well as the execution under it.
        """
        waiting = asyncio.ensure_future(self._one_at_a_time.acquire())
        # `_execute_one` links the execution, which a queued call has not started: without this
        # a cancelled call stays queued until the call ahead of it finishes.
        cancellation_token.link_future(waiting)
        try:
            await waiting
        except BaseException:
            # Leaving while the lock changes hands must not strand it: an acquisition that
            # already completed is not undone by cancelling the future it completed on.
            waiting.cancel()
            if waiting.done() and not waiting.cancelled() and waiting.exception() is None:
                self._one_at_a_time.release()
            raise
        try:
            # `CancellationToken.cancel()` cancels the futures linked to it, and cancelling a
            # *finished* future does nothing — so a token cancelled between the acquisition
            # completing and this line resuming leaves the await above with nothing to report.
            # Ask the token instead of trusting it, or a cancelled call runs, and condemns the
            # sandbox it shares with the call that is still using it.
            if cancellation_token.is_cancelled():
                raise asyncio.CancelledError
            return await self._execute_blocks(code_blocks, cancellation_token)
        finally:
            self._one_at_a_time.release()

    async def _execute_blocks(
        self, code_blocks: list[CodeBlock], cancellation_token: CancellationToken
    ) -> CodeResult:
        """`execute_code_blocks` with the lock already held."""
        sandbox: BoundedExec | None = None
        rendered: list[str] = []
        exit_code = 0
        for block in code_blocks:
            if block.language.casefold() not in _LANGUAGES:
                rendered.append(f"Error: only Python runs in this sandbox, not {block.language!r}")
                exit_code = 1
                break
            if sandbox is None:
                acquired = await self._router.acquire(self._key, self._spec)
                if not isinstance(acquired, BoundedExec):
                    return CodeResult(
                        exit_code=1,
                        output="Error: the backend has no exec_bounded, so no program runs here",
                    )
                sandbox = acquired
            try:
                text, code = await self._execute_one(sandbox, block.code, cancellation_token)
            except _ExecutionLost as lost:
                text, code = lost.rendered, 1
            rendered.append(text)
            # The guest's own exit code answers for the result — the tool reads `success` off
            # it, so a program that exited nonzero is not reported as a success. A list stops
            # at the first nonzero exit, as both of AutoGen's reference executors do.
            exit_code = code
            if code:
                break
        return CodeResult(exit_code=exit_code, output="\n\n".join(rendered))

    @staticmethod
    def _timeout_text() -> str:
        return f"Error: the program timed out after {EXEC_TIMEOUT_SECONDS}s"

    @staticmethod
    def _overflow_text() -> str:
        return f"Error: the program's output exceeded the {MAX_OUTPUT_BYTES}-byte budget"

    async def _execute_one(
        self, sandbox: BoundedExec, code: str, cancellation_token: CancellationToken
    ) -> tuple[str, int]:
        execution = asyncio.ensure_future(
            sandbox.exec_bounded(
                ["python3", "-c", code],
                working_directory=".",
                timeout=EXEC_TIMEOUT_SECONDS,
                max_output_bytes=MAX_OUTPUT_BYTES,
            )
        )
        # Cancelling the tool call cancels the *wait*; the guest's end stays unknown either
        # way, and the handler below condemns the sandbox on every lost execution.
        cancellation_token.link_future(execution)
        try:
            result = await execution
        except TimeoutError as error:
            await self._condemn()
            raise _ExecutionLost(self._timeout_text()) from error
        except SandboxExecOutputLimitExceeded as error:
            await self._condemn()
            raise _ExecutionLost(self._overflow_text()) from error
        except asyncio.CancelledError:
            # A cancellation condemns like any other lost execution — the guest may still be
            # running — and propagates.
            await self._condemn()
            raise
        except BaseException as error:
            # Any other failure the result did not come back from condemns too: the guest's
            # end is unknown, so the sandbox is not reusable, and the model reads a refusal.
            await self._condemn()
            raise _ExecutionLost(
                "Error: the program's execution failed — it may still be running; its "
                "sandbox was condemned — refused until its delete lands"
            ) from error
        return _render(result), result.exit_code

    async def _condemn(self) -> None:
        """Dispose the acquired sandbox through the unclean path: the key stays refused until
        a delete lands, and a landed one retires the refusal for a fresh create. The kind
        selector keeps a sibling workload on the same key out of the deletion."""
        await self._router.dispose_unclean(
            self._key, kind=self._spec.kind, timeout=CLEANUP_TIMEOUT_SECONDS
        )


class _ExecutionLost(Exception):
    """An execution whose end is unknown, carrying the rendered error the model should see.

    Raised after the sandbox is condemned, and caught by ``execute_code_blocks`` — which
    turns it back into the ``Error:`` string the tool reports, so a failed execution reads
    to the model as a refusal rather than as an exception out of the tool.
    """

    @property
    def rendered(self) -> str:
        return str(self)


def _render(result: ExecResult) -> str:
    """One run as the packaged kind renders it: `stdout:`, `stderr:`, then a nonzero exit.

    Empty sections are omitted rather than shown blank, and the trailing newline ``print``
    leaves is dropped, so a one-line program's answer is one line.
    """
    stdout = (result.stdout_text or "").rstrip("\n")
    stderr = (result.stderr_text or "").rstrip("\n")
    sections: list[str] = []
    if stdout:
        sections.append(f"stdout:\n{stdout}")
    if stderr:
        sections.append(f"stderr:\n{stderr}")
    if result.exit_code:
        sections.append(f"exit code: {result.exit_code}")
    return "\n\n".join(sections) if sections else "(the program printed nothing)"


def build_model() -> tuple[ChatCompletionClient, DefaultAzureCredential | None] | None:
    """One client library, two endpoints. CI sets `AZURE_OPENAI_ENDPOINT`; a laptop does not.

    Samples 09 and 13 make the same split on the framework's own client. Returns the model and
    the credential to close, or ``None`` when the environment names an endpoint and then does
    not say which deployment to reach on it.
    """
    azure_endpoint = os.environ.get("AZURE_OPENAI_ENDPOINT")
    if not azure_endpoint:
        return (
            OpenAIChatCompletionClient(
                model=os.environ.get("OPENAI_CHAT_MODEL") or DEFAULT_LOCAL_MODEL,
                base_url=os.environ.get("OPENAI_BASE_URL") or DEFAULT_LOCAL_BASE_URL,
                api_key=os.environ.get("OPENAI_API_KEY") or LOCAL_API_KEY_PLACEHOLDER,
                model_info=_model_info(ModelFamily.UNKNOWN),
            ),
            None,
        )
    env = require_env_vars(AZURE_MODEL_VARS)
    if env is None:
        return None
    from azure.identity.aio import DefaultAzureCredential, get_bearer_token_provider

    credential = DefaultAzureCredential()
    return (
        AzureOpenAIChatCompletionClient(
            # One variable names the deployment, as sample 06's client reads it: the model name
            # and the deployment are the same string here, and the README says so.
            model=env["AZURE_OPENAI_CHAT_MODEL"],
            azure_endpoint=azure_endpoint,
            azure_deployment=env["AZURE_OPENAI_CHAT_MODEL"],
            api_version=AZURE_API_VERSION,
            # The async half of the pair, as samples 05, 09 and 13 carry it: the client is
            # awaited, and an async provider mints its token off the event loop's thread.
            azure_ad_token_provider=get_bearer_token_provider(credential, AZURE_SCOPE),
            # Sample 06's prerequisite: the deployment is a reasoning model, and `gpt-5.4` is
            # not one of the names AutoGen knows.
            model_info=_model_info(ModelFamily.GPT_5),
        ),
        credential,
    )


def executor_results(result: TaskResult) -> list[str]:
    """Everything the `CodeExecutor` tool returned during `result`, in the order it came back.

    A `ToolCallExecutionEvent` is written by the framework beside the call that asked for it; a
    model can say a program printed something, but it cannot put that sentence here without the
    interpreter. AutoGen's replies are not MAF's, so this sample reads its own list rather than
    the scaffold's `tool_results`, the way sample 17 reads `ToolMessage`s.
    """
    return [
        str(item.content)
        for message in result.messages
        if isinstance(message, ToolCallExecutionEvent)
        for item in message.content
        if item.name == EXECUTOR_TOOL
    ]


def final_reply(result: TaskResult) -> str:
    """The last thing the model said, which is its own account of the results below it."""
    for message in reversed(result.messages):
        if isinstance(message, TextMessage) and message.source == AGENT_ID and message.content:
            return message.content
    return ""


async def run() -> int:
    """Wire the stack, run one turn, and take the container down again."""
    env = require_env_vars(("MAF_EGRESS_PROXY_IMAGE",))
    if env is None:
        return 2

    backend = DockerSandboxBackend(
        DockerSandboxConfig(egress_proxy_image=env["MAF_EGRESS_PROXY_IMAGE"])
    )
    router = SandboxRouter([backend], min_isolation=Isolation.CONTAINER)
    spec = SandboxSpec(
        kind=KIND,
        image=CODEACT_IMAGE,
        requires=frozenset({Capability.EXEC}),
        egress=Egress.ALLOWLIST,
        egress_allow=(
            EgressRule("pypi.org", methods=(HttpMethod.GET,)),
            EgressRule("files.pythonhosted.org", methods=(HttpMethod.GET,)),
        ),
    )
    router.ensure_can_serve(spec)

    configured = build_model()
    if configured is None:
        return 2
    model, credential = configured

    key = SandboxKey(scope=SCOPE, thread_id=THREAD_ID, agent_id=AGENT_ID)
    executor = SandboxCodeExecutor(router, key, spec)
    try:
        agent = AssistantAgent(
            name=AGENT_ID,
            model_client=model,
            system_message=INSTRUCTIONS,
            tools=[PythonCodeExecutionTool(executor)],
            reflect_on_tool_use=True,
        )
        result = await agent.run(task=TASK)
        # Quoted first, because the reply and the block below share one stream and the live
        # check trusts the `[measured]` tag completely.
        print(quoted(final_reply(result)))

        # Prose is never execution evidence: a model can recite the constant, so the count is
        # read out of the tool's own recorded results, filtered to what reached the sandbox.
        runs = [one for one in executor_results(result) if _RAN.search(one)]
        print()
        print(
            evidence(
                EXECUTOR_TOOL_HEADING,
                runs,
                "programs whose output came back from the sandbox",
            )
        )
    finally:
        # Deletes rather than relying on the container living on — see sample 01's README.
        purge = await router.dispose_scope(SCOPE, THREAD_ID)
        print(f"\n{MEASURED}Disposed {purge.disposed} sandbox(es).")
        if purge.undisposed is not None:
            print(f"{MEASURED}Not fully disposed: {purge.undisposed}")
        # The client holds the HTTP transport both roads run on, and the credential holds the
        # identity minted for it. Each close is guarded by the other's: a model close that
        # raises must not skip the credential's, and the reverse holds too, so the two run as
        # separate `finally`-guarded steps rather than one sequential tail.
        try:
            await model.close()
        finally:
            if credential is not None:
                await credential.close()

    return 0


if __name__ == "__main__":
    print(installed_versions())
    raise SystemExit(asyncio.run(run()))
