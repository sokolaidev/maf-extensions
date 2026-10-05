"""Run CodeAct inside a controller-owned Hyperlight AKS pod."""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from contextlib import AsyncExitStack
from pathlib import Path
from urllib.parse import urlsplit

from _scaffold import (
    MEASURED,
    evidence,
    installed_versions,
    quoted,
    require_env_vars,
    result_text,
    tool_results,
)
from maf_sandbox import Cleanup, SandboxRouter
from maf_sandbox.maf import COMPLETED_TEXT, list_no_files, make_caller_context
from maf_sandbox_codeact import CodeactRuntime, make_codeact_tools
from maf_sandbox_hyperlight import (
    RUNTIME_INSTRUCTIONS,
    HyperlightPodConfig,
    HyperlightSandboxBackend,
    HyperlightSandboxConfig,
)

ANSWER = "354224848179261915075"
PROGRAM = "a, b = 0, 1\nfor _ in range(100):\n    a, b = b, a + b\nprint(a)"
TASK = (
    "Write Python to compute the 100th Fibonacci number, with F(0) = 0 and F(1) = 1. "
    "Use execute_code and report exactly the integer it prints."
)
MODEL_VARS = ("OPENAI_BASE_URL", "OPENAI_MODEL", "OPENAI_API_KEY")


async def purge_scope(router: SandboxRouter, binding: HyperlightPodConfig) -> None:
    """Require confirmed disposal before reporting sample success."""
    purge = await router.dispose_scope(binding.key.scope, binding.key.thread_id)
    print(f"{MEASURED}Disposed {purge.disposed} sandbox(es).", flush=True)
    if purge.undisposed is not None:
        raise RuntimeError(f"Not fully disposed: {purge.undisposed}")


def memory_peak_bytes() -> int | None:
    """Return available cgroup telemetry without changing the application outcome."""
    try:
        return int(Path("/sys/fs/cgroup/memory.peak").read_text())
    except (OSError, ValueError):
        return None


async def run(*, smoke: bool = True) -> int:
    """Use the supervisor's identity for every tool call; local execution is refused."""
    binding = HyperlightPodConfig.from_environment()
    if binding.kind != "codeact":
        raise ValueError("this sample requires a codeact pod binding")
    env = {} if smoke else require_env_vars(MODEL_VARS)
    if env is None:
        return 2
    if not smoke:
        endpoint = urlsplit(env["OPENAI_BASE_URL"])
        if (
            endpoint.scheme != "https"
            or not endpoint.hostname
            or endpoint.username
            or endpoint.password
        ):
            raise ValueError("OPENAI_BASE_URL must be an HTTPS endpoint without credentials")
    started = time.monotonic()
    async with AsyncExitStack() as cleanup:
        backend = HyperlightSandboxBackend(
            HyperlightSandboxConfig(pod=binding, max_worker_memory_bytes=None)
        )
        cleanup.push_async_callback(backend.aclose)
        router = SandboxRouter([backend], min_cleanup=Cleanup.RESET)
        cleanup.push_async_callback(purge_scope, router, binding)
        context = make_caller_context(
            list_no_files, lambda: binding.key.scope, lambda: binding.key.thread_id
        )
        tools = make_codeact_tools(
            router, binding.key.agent_id, context, runtime=CodeactRuntime(RUNTIME_INSTRUCTIONS)
        )
        if not tools:
            raise RuntimeError("execute_code was not attached")
        if smoke:
            outputs = [result_text(await tools[0].invoke(arguments={"code": PROGRAM}))]
            reply = None
        else:
            from agent_framework import Agent
            from agent_framework.openai import OpenAIChatClient

            agent = Agent(
                client=OpenAIChatClient(
                    model=env["OPENAI_MODEL"],
                    base_url=env["OPENAI_BASE_URL"],
                    api_key=env["OPENAI_API_KEY"],
                ),
                name=binding.key.agent_id,
                instructions="Always use execute_code and report the integer the tool returned.",
                tools=tools,
            )
            response = await agent.run(TASK)
            reply = response.text
            print(quoted(reply), flush=True)
            outputs = tool_results(response, "execute_code")
        print(evidence("CodeAct tool results", outputs, "CodeAct results returned"), flush=True)
        expected = f"{COMPLETED_TEXT}\nResult: ok\nstdout:\n{ANSWER}"
        if not any(output.strip() == expected for output in outputs):
            raise RuntimeError("No successful CodeAct result contained the expected integer")
        if reply is not None and ANSWER not in reply:
            raise RuntimeError("The model did not report the integer returned by CodeAct")
    print(
        f"{MEASURED}"
        + json.dumps(
            {
                "complete": True,
                "mode": "smoke" if smoke else "model",
                "application_seconds": time.monotonic() - started,
                "memory_peak_bytes": memory_peak_bytes(),
            }
        ),
        flush=True,
    )
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--model", action="store_true", help="Call the configured model (billable)."
    )
    args = parser.parse_args()
    print(installed_versions(), flush=True)
    raise SystemExit(asyncio.run(run(smoke=not args.model)))
