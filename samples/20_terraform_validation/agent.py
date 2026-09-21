"""Validate one deliberately invalid module with either engine on Docker or ACAS."""

# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "agent-framework-openai",
#     "azure-core[aio]",
#     "azure-identity",
#     "maf-sandbox-acas",
#     "maf-sandbox-docker",
#     "maf-sandbox-terraform",
#     "maf-sandbox>=0.42",
# ]
# ///

from __future__ import annotations

import asyncio
import json
import os
import sys
from contextlib import AsyncExitStack
from pathlib import Path
from threading import Lock
from typing import Literal

from _scaffold import (
    MEASURED,
    conversation_id,
    evidence,
    installed_versions,
    quoted,
    require_env_vars,
    tool_results,
)
from agent_framework import Agent, InMemoryAgentFileStore
from agent_framework.openai import OpenAIChatClient
from azure.identity.aio import DefaultAzureCredential
from maf_sandbox import (
    Isolation,
    SandboxDisposed,
    SandboxObserver,
    SandboxRouter,
    ToolCallEnded,
)
from maf_sandbox.maf import list_all_files, make_caller_context
from maf_sandbox_acas import AcasSandboxBackend, AcasSandboxConfig
from maf_sandbox_docker import DockerSandboxBackend, DockerSandboxConfig
from maf_sandbox_terraform import make_terraform_tools

SCOPE = "samples"
THREAD_ID = conversation_id("20-terraform-validation")
AGENT_DIR = "infrastructure-validator"
MODEL_VARS = ("AZURE_OPENAI_ENDPOINT", "AZURE_OPENAI_CHAT_MODEL")
SANDBOX_VARS = (
    "ACAS_SANDBOX_ENDPOINT",
    "ACAS_SANDBOX_SUBSCRIPTION_ID",
    "ACAS_SANDBOX_RESOURCE_GROUP",
    "ACAS_SANDBOX_GROUP",
    "ACAS_SANDBOX_REGISTRY",
)


class CallCleanup(SandboxObserver):
    """Join backend disposal reports to the call that requested them."""

    def __init__(self, backend: str) -> None:
        self.backend = backend
        self.disposals: list[SandboxDisposed] = []
        self.complete: list[bool] = []
        self._lock = Lock()

    def sandbox_disposed(self, event: SandboxDisposed) -> None:
        with self._lock:
            self.disposals.append(event)

    def tool_call_ended(self, event: ToolCallEnded) -> None:
        with self._lock:
            disposals = [item for item in self.disposals if item.call == event.call]
            disposed = (
                bool(event.keys)
                and {item.key for item in disposals} == set(event.keys)
                and all(
                    item.backend == self.backend and item.outcome == "gone" and item.failure is None
                    for item in disposals
                )
            )
            self.complete.append(disposed and event.failure is None and event.unclean == 0)
            record = {
                "call": event.call,
                "tool": event.tool,
                "backend": self.backend,
                "disposed": disposed,
                "failure": event.failure,
                "unclean": event.unclean,
                "seconds": event.seconds,
            }
            print(f"{MEASURED}Call: {json.dumps(record)}")


async def run() -> int:
    """Run one agent turn and report validation and cleanup from host observations."""
    backend_name = os.environ.get("SAMPLE_BACKEND", "docker")
    engine: Literal["terraform", "opentofu"]
    selected = os.environ.get("SAMPLE_ENGINE", "terraform")
    if backend_name not in ("docker", "acas") or selected not in ("terraform", "opentofu"):
        print(
            "SAMPLE_BACKEND must be docker or acas; SAMPLE_ENGINE must be terraform or opentofu.",
            file=sys.stderr,
        )
        return 2
    engine = "terraform" if selected == "terraform" else "opentofu"
    # Scope purges must not reach another engine's job in the same workflow run.
    thread_id = f"{THREAD_ID}-{backend_name}-{engine}"
    image_variable = f"{engine.upper()}_SANDBOX_IMAGE"
    env = require_env_vars(
        MODEL_VARS + (image_variable,) + (SANDBOX_VARS if backend_name == "acas" else ())
    )
    if env is None:
        return 2

    async with AsyncExitStack() as stack:
        if backend_name == "acas":
            backend = AcasSandboxBackend(
                AcasSandboxConfig(
                    endpoint=env["ACAS_SANDBOX_ENDPOINT"],
                    subscription_id=env["ACAS_SANDBOX_SUBSCRIPTION_ID"],
                    resource_group=env["ACAS_SANDBOX_RESOURCE_GROUP"],
                    sandbox_group=env["ACAS_SANDBOX_GROUP"],
                    registry=env["ACAS_SANDBOX_REGISTRY"],
                )
            )
            stack.push_async_callback(backend.aclose)
            isolation = Isolation.MICROVM
        else:
            backend = await DockerSandboxBackend.create(DockerSandboxConfig())
            isolation = Isolation.CONTAINER
        observer = CallCleanup(backend.name)
        router = SandboxRouter([backend], min_isolation=isolation, observer=observer)
        credential = await stack.enter_async_context(DefaultAzureCredential())
        try:
            store = InMemoryAgentFileStore()
            await store.write("main.tf", Path(__file__).with_name("main.tf").read_text("utf-8"))
            context = make_caller_context(list_all_files, lambda: SCOPE, lambda: thread_id)
            tools = make_terraform_tools(
                router, store, AGENT_DIR, context, engine=engine, image=env[image_variable]
            )
            if not tools:
                print("No sandbox backend: validation tool was not attached.", file=sys.stderr)
                return 2
            tool = f"{engine}_validate"
            agent = Agent(
                client=OpenAIChatClient(
                    model=env["AZURE_OPENAI_CHAT_MODEL"],
                    azure_endpoint=env["AZURE_OPENAI_ENDPOINT"],
                    credential=credential,
                ),
                name=AGENT_DIR,
                instructions=f"Always call {tool} with files=['main.tf'] and root_module='.'. Report its diagnostics without repairing the module.",
                tools=tools,
            )
            response = await agent.run("Validate main.tf once and report every diagnostic.")
            print(quoted(response.text))
            print(
                evidence(
                    f"Diagnostics as {tool} returned them",
                    tool_results(response, tool),
                    "validation results",
                )
            )
        finally:
            purge = await router.dispose_scope(SCOPE, thread_id)
            print(f"{MEASURED}Disposed {purge.disposed} sandbox(es).")
            if purge.undisposed is not None:
                print(f"{MEASURED}Not fully disposed: {purge.undisposed}")
        return 0 if observer.complete and all(observer.complete) and purge.undisposed is None else 1


if __name__ == "__main__":
    print(installed_versions())
    raise SystemExit(asyncio.run(run()))
