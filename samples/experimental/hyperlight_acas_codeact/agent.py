"""Run the same CodeAct task on Hyperlight in DEV/CI and ACAS elsewhere.

This source-only sample uses the workspace while Hyperlight is unreleased.
See README.md for the environment contract and native Windows/Linux prerequisites.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections.abc import Mapping
from contextlib import AsyncExitStack
from typing import TYPE_CHECKING, Any, Literal

from _scaffold import (
    MEASURED,
    conversation_id,
    evidence,
    installed_versions,
    quoted,
    require_env_vars,
    result_text,
    tool_results,
)
from maf_sandbox import CallerContext, Cleanup, SandboxRouter
from maf_sandbox.maf import list_no_files, make_caller_context
from maf_sandbox_codeact import CodeactRuntime, make_codeact_tools

if TYPE_CHECKING:
    from maf_sandbox_acas import AcasSandboxBackend
    from maf_sandbox_hyperlight import HyperlightSandboxBackend

BackendName = Literal["hyperlight", "acas"]
SCOPE = "samples"
AGENT_ID = "data-analyst"
ANSWER = "354224848179261915075"
PROGRAM = "a, b = 0, 1\nfor _ in range(100):\n    a, b = b, a + b\nprint(a)"
TASK = (
    "Write a Python program that computes the 100th Fibonacci number, with "
    "F(0) = 0 and F(1) = 1, and prints just the integer. Run it with execute_code "
    "and tell me exactly what it printed."
)
SANDBOX_VARS = (
    "ACAS_SANDBOX_ENDPOINT",
    "ACAS_SANDBOX_SUBSCRIPTION_ID",
    "ACAS_SANDBOX_RESOURCE_GROUP",
    "ACAS_SANDBOX_GROUP",
)
MODEL_VARS = ("AZURE_OPENAI_ENDPOINT", "AZURE_OPENAI_CHAT_MODEL")


def select_backend(env: Mapping[str, str]) -> BackendName:
    """Select from host configuration; a CI signal takes precedence over APP_ENV."""
    ci = env.get("CI", "").strip().casefold()
    if ci not in {"", "0", "false", "no", "1", "true", "yes"}:
        raise ValueError("CI must be true/false, yes/no, or 1/0 when set.")
    if ci in {"1", "true", "yes"}:
        return "hyperlight"
    environment = env.get("APP_ENV", "").strip().casefold()
    if not environment:
        raise ValueError("Set APP_ENV (DEV, CI, STAGING, PROD, etc.) or CI=true.")
    return "hyperlight" if environment in {"dev", "ci"} else "acas"


def build_backend(
    name: BackendName, env: Mapping[str, str]
) -> HyperlightSandboxBackend | AcasSandboxBackend:
    """Import and construct only the backend selected by the host."""
    if name == "hyperlight":
        from maf_sandbox_hyperlight import HyperlightSandboxBackend, HyperlightSandboxConfig

        cgroup_root = env.get("MAF_HYPERLIGHT_CGROUP_ROOT") if sys.platform == "linux" else None
        return HyperlightSandboxBackend(
            HyperlightSandboxConfig(linux_cgroup_root=cgroup_root or None)
        )

    from maf_sandbox_acas import AcasSandboxBackend, AcasSandboxConfig

    return AcasSandboxBackend(
        AcasSandboxConfig(
            endpoint=env["ACAS_SANDBOX_ENDPOINT"],
            subscription_id=env["ACAS_SANDBOX_SUBSCRIPTION_ID"],
            resource_group=env["ACAS_SANDBOX_RESOURCE_GROUP"],
            sandbox_group=env["ACAS_SANDBOX_GROUP"],
        )
    )


def tools_for(router: SandboxRouter, name: BackendName, context: CallerContext) -> list[Any]:
    """Pair each backend with the CodeAct execution contract it supports."""
    if name == "hyperlight":
        from maf_sandbox_hyperlight import RUNTIME_INSTRUCTIONS

        return make_codeact_tools(
            router, AGENT_ID, context, runtime=CodeactRuntime(RUNTIME_INSTRUCTIONS)
        )
    return make_codeact_tools(router, AGENT_ID, context, image="python-3.13")


async def purge_scope(router: SandboxRouter, thread_id: str) -> None:
    """Report final cleanup and fail the run if disposal cannot be confirmed."""
    purge = await router.dispose_scope(SCOPE, thread_id)
    print(f"{MEASURED}Disposed {purge.disposed} sandbox(es).")
    if purge.undisposed is not None:
        raise RuntimeError(f"Not fully disposed: {purge.undisposed}")


async def run(*, smoke: bool = False) -> int:
    """Run one agent turn, or call its CodeAct tool directly for a model-free smoke check."""
    try:
        name = select_backend(os.environ)
    except ValueError as error:
        print(error, file=sys.stderr)
        return 2
    required = (() if smoke else MODEL_VARS) + (SANDBOX_VARS if name == "acas" else ())
    env = require_env_vars(required)
    if env is None:
        return 2

    print(f"{MEASURED}Backend: {name}")
    if name == "hyperlight":
        print(f"{MEASURED}Hyperlight host: {sys.platform}")
    thread_id = conversation_id("hyperlight-acas-codeact")
    async with AsyncExitStack() as cleanup:
        backend = build_backend(name, os.environ)
        cleanup.push_async_callback(backend.aclose)
        # RESET enables warm Hyperlight reuse; ACAS satisfies this floor with DISPOSE.
        router = SandboxRouter([backend], min_cleanup=Cleanup.RESET)
        cleanup.push_async_callback(purge_scope, router, thread_id)
        context = make_caller_context(list_no_files, lambda: SCOPE, lambda: thread_id)
        tools = tools_for(router, name, context)
        if not tools:
            raise RuntimeError("execute_code was not attached.")

        if smoke:
            outputs = [result_text(await tools[0].invoke(arguments={"code": PROGRAM}))]
            reply = None
        else:
            from agent_framework import Agent
            from agent_framework.openai import OpenAIChatClient
            from azure.identity.aio import DefaultAzureCredential

            credential = await cleanup.enter_async_context(DefaultAzureCredential())
            agent = Agent(
                client=OpenAIChatClient(
                    model=env["AZURE_OPENAI_CHAT_MODEL"],
                    azure_endpoint=env["AZURE_OPENAI_ENDPOINT"],
                    credential=credential,
                ),
                name=AGENT_ID,
                instructions=(
                    "Answer computational questions by running Python with execute_code. "
                    "Always call the tool and report exactly the integer it printed."
                ),
                tools=tools,
            )
            response = await agent.run(TASK)
            reply = response.text
            print(quoted(reply))
            outputs = tool_results(response, "execute_code")

        print(evidence("CodeAct tool results", outputs, "CodeAct results returned"))
        # A correct reply alone does not prove that the sandbox ran the program.
        # The result is several items now: a completion line, a verdict drawn from the
        # tool's declared set, then the program's own text. The scaffold renders them
        # in order, so the check reads the parts it needs rather than the whole.
        if not any("Result: ok" in output and f"stdout:\n{ANSWER}" in output for output in outputs):
            raise RuntimeError("No successful CodeAct result contained the expected integer.")
        if reply is not None and ANSWER not in reply:
            raise RuntimeError("The model did not report the integer returned by CodeAct.")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--smoke", action="store_true", help="Run CodeAct without calling a model.")
    args = parser.parse_args()
    print(installed_versions())
    raise SystemExit(asyncio.run(run(smoke=args.smoke)))
