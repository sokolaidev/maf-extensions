"""Generate a documented Azure CLI script and validate it without deploying resources."""

# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "agent-framework-openai",
#     "azure-core[aio]",
#     "azure-identity",
#     "maf-sandbox-docker",
#     "maf-sandbox-shell>=0.1.0",
#     "maf-sandbox>=0.52",
#     "mcp>=2.2.0,<3",
#     "tenacity>=9.1.4,<9.2.0",
# ]
# ///

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import shlex
from contextlib import AsyncExitStack
from pathlib import Path

from _scaffold import (
    MEASURED,
    conversation_id,
    installed_versions,
    quoted,
    require_env_vars,
    result_text,
)
from agent_framework import Agent, InMemoryAgentFileStore, MCPStreamableHTTPTool
from agent_framework.openai import OpenAIChatClient
from azure.identity.aio import DefaultAzureCredential
from maf_sandbox import SandboxDisposed, SandboxObserver, SandboxRouter, ToolCallEnded
from maf_sandbox.maf import list_all_files, make_caller_context
from maf_sandbox_docker import DockerSandboxBackend, DockerSandboxConfig
from maf_sandbox_shell import ShellFlavor, make_shell_tools

SCOPE = "samples"
THREAD_ID = conversation_id("21-shell-storage")
LEARN_URL = "https://learn.microsoft.com/api/mcp"
MODEL_VARS = ("AZURE_OPENAI_ENDPOINT", "AZURE_OPENAI_CHAT_MODEL", "SHELL_SANDBOX_IMAGE")
MAX_ATTEMPTS = 3
PROMPT = """Use microsoft_docs_search to find official Azure CLI documentation for creating
a StorageV2 account. Then return only a Bash script, starting with #!/usr/bin/env bash.
Use set -euo pipefail. Require RESOURCE_GROUP, AZURE_LOCATION and STORAGE_ACCOUNT_NAME
from the environment using ${NAME:?message}; never invent subscription or account names.
Create the resource group with az group create, then use az storage account create with
--kind StorageV2 --sku Standard_LRS --https-only true --min-tls-version TLS1_2
--allow-blob-public-access false. Quote all variable expansions. Do not add ShellCheck
directives or download or execute other scripts. The script will be statically validated,
not executed. Do not claim a deployment occurred."""


def measure(stage: str, **values: object) -> None:
    """Print host observations with untrusted strings confined inside JSON."""
    print(f"{MEASURED}{json.dumps({'stage': stage, **values})}")


def learn_searches(reply: object) -> int:
    """Count successful search results linked to actual Microsoft Learn tool calls."""
    contents = [
        content for message in getattr(reply, "messages", []) for content in message.contents
    ]
    calls = {
        content.call_id
        for content in contents
        if getattr(content, "type", None) == "function_call"
        and getattr(content, "name", None) == "microsoft_docs_search"
        and content.call_id
    }
    return sum(
        getattr(content, "type", None) == "function_result"
        and getattr(content, "call_id", None) in calls
        and not getattr(content, "exception", None)
        and re.search(
            r"https://learn\.microsoft\.com/(?!api/)[a-z]{2}-[a-z]{2}/",
            result_text(getattr(content, "result", "")),
        )
        is not None
        for content in contents
    )


def script_text(answer: str) -> str:
    """Extract one Bash artifact and require the requested deployment shape."""
    script = answer.strip()
    fenced = re.fullmatch(r"```(?:bash|sh)?\s*\n(.*?)\n```", script, re.DOTALL)
    if fenced:
        script = fenced.group(1)
    if not script.startswith("#!/usr/bin/env bash\n"):
        raise ValueError("Return only a Bash script with #!/usr/bin/env bash")
    if "shellcheck" in script.casefold():
        raise ValueError("Analyzer suppression directives are not allowed in this sample")
    if not re.search(r"(?m)^set -euo pipefail\s*$", script):
        raise ValueError("Use set -euo pipefail")
    for variable in ("RESOURCE_GROUP", "AZURE_LOCATION", "STORAGE_ACCOUNT_NAME"):
        if "${" + variable + ":?" not in script:
            raise ValueError(f"Require {variable} from the caller")
    commands = [
        shlex.split(line, comments=True)
        for line in script.replace("\\\n", " ").splitlines()
        if line.lstrip().startswith("az ")
    ]
    if not any(command[:3] == ["az", "group", "create"] for command in commands):
        raise ValueError("Create the resource group with az group create")
    accounts = [
        command for command in commands if command[:4] == ["az", "storage", "account", "create"]
    ]
    if len(accounts) != 1:
        raise ValueError("Create exactly one account with az storage account create")
    command = accounts[0]
    for flag, value in (
        ("--kind", "StorageV2"),
        ("--sku", "Standard_LRS"),
        ("--https-only", "true"),
        ("--min-tls-version", "TLS1_2"),
        ("--allow-blob-public-access", "false"),
    ):
        if not any(command[i : i + 2] == [flag, value] for i in range(len(command) - 1)):
            raise ValueError(f"Use {flag} {value}")
    return script + "\n"


class Cleanup(SandboxObserver):
    """Require successful disposal for every validation call."""

    def __init__(self) -> None:
        self.disposals: list[SandboxDisposed] = []
        self.calls: list[bool] = []

    def sandbox_disposed(self, event: SandboxDisposed) -> None:
        self.disposals.append(event)

    def tool_call_ended(self, event: ToolCallEnded) -> None:
        disposed = [item for item in self.disposals if item.call == event.call]
        complete = (
            bool(event.keys)
            and {item.key for item in disposed} == set(event.keys)
            and all(item.outcome == "gone" and item.failure is None for item in disposed)
            and event.failure is None
            and event.unclean == 0
        )
        self.calls.append(complete)
        measure("validation_call", call=event.call, seconds=event.seconds, complete=complete)


def clean_result(answer: object) -> bool:
    """Read the shell kind's trusted completion and verdict items."""
    return (
        isinstance(answer, list)
        and len(answer) >= 2
        and getattr(answer[0], "text", None) == "The workload ran to a definitive result."
        and getattr(answer[1], "text", None) == "Result: clean"
    )


async def run(output: Path) -> int:
    """Ground generation in Learn, repair findings and retain only a validated script."""
    env = require_env_vars(MODEL_VARS)
    if env is None:
        return 2
    if output.exists():
        raise FileExistsError(output)
    print(installed_versions())
    store = InMemoryAgentFileStore()
    observer = Cleanup()
    backend = await DockerSandboxBackend.create(DockerSandboxConfig())
    router = SandboxRouter([backend], min_isolation=backend.isolation, observer=observer)
    context = make_caller_context(list_all_files, lambda: SCOPE, lambda: THREAD_ID)
    [validator] = make_shell_tools(
        router,
        store,
        "storage-author",
        context,
        flavor=ShellFlavor.BASH,
        image=env["SHELL_SANDBOX_IMAGE"],
    )
    candidate: str | None = None
    docs = 0
    try:
        async with AsyncExitStack() as stack:
            credential = await stack.enter_async_context(DefaultAzureCredential())
            learn = await stack.enter_async_context(
                MCPStreamableHTTPTool(
                    name="Microsoft Learn",
                    url=LEARN_URL,
                    load_prompts=False,
                    request_timeout=60,
                )
            )
            async with Agent(
                client=OpenAIChatClient(
                    model=env["AZURE_OPENAI_CHAT_MODEL"],
                    azure_endpoint=env["AZURE_OPENAI_ENDPOINT"],
                    credential=credential,
                ),
                name="storage-author",
                instructions=PROMPT,
                tools=[learn],
            ) as agent:
                session = agent.create_session()
                request = "Find the documentation and write the deployment script."
                for attempt in range(1, MAX_ATTEMPTS + 1):
                    reply = await agent.run(request, session=session)
                    print(quoted(reply.text))
                    docs += learn_searches(reply)
                    measure("learn", results=docs)
                    if not docs:
                        request = (
                            "Call microsoft_docs_search successfully before writing the script."
                        )
                        continue
                    try:
                        script = script_text(reply.text)
                    except ValueError as exc:
                        request = f"Repair the script: {exc}. Return only the complete Bash script."
                        continue
                    await store.write("deploy.sh", script)
                    before = len(observer.calls)
                    result = await validator.invoke(arguments={"files": ["deploy.sh"]})
                    if len(observer.calls) != before + 1 or not observer.calls[-1]:
                        raise RuntimeError("Validation did not complete with confirmed disposal")
                    readback = await store.read("deploy.sh")
                    if readback != script:
                        raise RuntimeError("Validation changed the submitted script")
                    clean = clean_result(result)
                    measure(
                        "validation",
                        attempt=attempt,
                        clean=clean,
                        sha256=hashlib.sha256(script.encode()).hexdigest(),
                    )
                    print(quoted(result_text(result)))
                    if clean:
                        candidate = script
                        break
                    request = (
                        "Repair the script using these analyzer findings. Return only Bash.\n"
                        + result_text(result)
                    )
    finally:
        try:
            purge = await router.dispose_scope(SCOPE, THREAD_ID)
            if purge.undisposed:
                raise RuntimeError("Scope cleanup left undisposed sandboxes")
        finally:
            if await store.file_exists("deploy.sh"):
                await store.delete("deploy.sh")
            if await store.file_exists("deploy.sh"):
                raise RuntimeError("Temporary script remains in the store")
        measure("cleanup", complete=bool(observer.calls) and all(observer.calls))
    if candidate is None:
        raise RuntimeError("No clean deployment script after three attempts")
    with output.open("x", encoding="utf-8", newline="\n") as destination:
        destination.write(candidate)
    content = output.read_bytes()
    if content != candidate.encode():
        raise RuntimeError("Output read-back differs from the validated script")
    measure("artifact", sha256=hashlib.sha256(content).hexdigest(), bytes=len(content))
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("deploy.sh"))
    args = parser.parse_args()
    raise SystemExit(asyncio.run(asyncio.wait_for(run(args.output), timeout=600)))
