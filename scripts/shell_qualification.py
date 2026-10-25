"""Qualify both public shell tools against an immutable Docker image."""

from __future__ import annotations

import asyncio
import json
import subprocess
from importlib.metadata import version
from typing import TYPE_CHECKING, Any, cast
from uuid import uuid4

from container_release import require_digest
from maf_sandbox import (
    CallerContext,
    Isolation,
    Sandbox,
    SandboxKey,
    SandboxObserver,
    SandboxRouter,
    SandboxSpec,
    ToolCallEnded,
)
from maf_sandbox.maf import COMPLETED_TEXT, NOT_COMPLETED_TEXT
from maf_sandbox.testing import InMemoryStore
from maf_sandbox_docker import DockerSandboxBackend, DockerSandboxConfig
from maf_sandbox_shell import ShellFlavor, make_shell_tools

if TYPE_CHECKING:
    from agent_framework import AgentFileStore

CASES = (
    (ShellFlavor.BASH, "clean", "main.sh", 'echo "hello"\n', "clean"),
    (ShellFlavor.BASH, "empty", "empty.sh", "", "clean"),
    (ShellFlavor.BASH, "findings", "-main.sh", "echo $word\n", "findings"),
    (ShellFlavor.BASH, "syntax", "main.sh", "if then\n", None),
    (
        ShellFlavor.BASH,
        "suppression",
        "main.sh",
        "# shellcheck disable=SC2086\necho $HOME\n",
        "clean",
    ),
    (ShellFlavor.PWSH, "clean", "main.ps1", "Write-Output 'hello'\n", "clean"),
    (ShellFlavor.PWSH, "empty", "empty.ps1", "", "clean"),
    (ShellFlavor.PWSH, "findings", "-main.ps1", "Write-Host 'hello'\n", "findings"),
    (ShellFlavor.PWSH, "syntax", "main.ps1", "function {\n", None),
    (ShellFlavor.PWSH, "module", "-main.PSM1", "function global:Get-Widget {}\n", "findings"),
    (
        ShellFlavor.PWSH,
        "suppression",
        "main.ps1",
        (
            "[Diagnostics.CodeAnalysis.SuppressMessageAttribute('PSAvoidUsingWriteHost','')]\n"
            "param()\nWrite-Host 'hello'\n"
        ),
        "clean",
    ),
)


def require(condition: bool, message: str) -> None:
    """Keep qualification gates enabled under optimized Python."""
    if not condition:
        raise ValueError(message)


def docker(*args: str) -> str:
    """Read bounded engine evidence from the SDK's Docker environment."""
    return subprocess.run(
        ["docker", *args], check=True, capture_output=True, text=True, encoding="utf-8", timeout=30
    ).stdout


def owned(scope: str) -> list[str]:
    """Include stopped containers when verifying call-scoped disposal."""
    return docker("ps", "-aq", "--no-trunc", "--filter", f"label=maf-sandbox.scope={scope}").split()


def observation(scope: str, image_id: str) -> dict[str, Any]:
    """Require the selected image and closed networking before staging input."""
    ids = owned(scope)
    require(len(ids) == 1, "Expected one shell qualification container")
    value = json.loads(docker("inspect", ids[0]))[0]
    require(value["State"]["Running"] is True, "Shell container is not running")
    require(
        value["Image"] == value["Config"]["Image"] == image_id,
        "Shell container differs from the selected image",
    )
    require(value["HostConfig"]["NetworkMode"] == "none", "Shell networking is not closed")
    require(
        set(value["NetworkSettings"]["Networks"]) <= {"none"},
        "Shell container has an additional network",
    )
    return {"imageId": value["Image"], "network": "none", "containerId": ids[0]}


async def check(image_id: str) -> dict[str, Any]:
    """Retain SDK results, engine observations and disposal for both shell flavors."""
    require_digest(image_id)
    server = json.loads(await asyncio.to_thread(docker, "version", "--format", "{{json .Server}}"))
    require(server["Os"] == "linux" and server["Arch"] == "amd64", "Requires Linux/amd64 Docker")
    scope = "shell-release-" + uuid4().hex
    calls: list[ToolCallEnded] = []
    observations: list[dict[str, Any]] = []

    class Observer(SandboxObserver):
        def tool_call_ended(self, event: ToolCallEnded) -> None:
            calls.append(event)

    class Router(SandboxRouter):
        async def acquire(self, key: SandboxKey, spec: SandboxSpec, **kwargs: Any) -> Sandbox:
            sandbox = await super().acquire(key, spec, **kwargs)
            observations.append(await asyncio.to_thread(observation, scope, image_id))
            return sandbox

    backend = await DockerSandboxBackend.create(DockerSandboxConfig(memory="1g", cpus=2))
    router = Router([backend], min_isolation=Isolation.CONTAINER, observer=Observer())
    context = CallerContext(
        current_scope=lambda: scope,
        current_thread_id=lambda: "shell",
        list_files=InMemoryStore.list,
    )
    reports = []
    try:
        for flavor, case, path, code, verdict in CASES:
            store = InMemoryStore({path: code})
            [tool] = make_shell_tools(
                router,
                cast("AgentFileStore", store),
                "shell-release",
                context,
                flavor=flavor,
                image=image_id,
            )
            reply = await tool.func(files=[path])
            texts = [item.text or "" for item in reply]
            completed = verdict is not None
            require(
                bool(texts) and texts[0] == (COMPLETED_TEXT if completed else NOT_COMPLETED_TEXT),
                f"Shell {flavor}/{case} completion differs",
            )
            require(
                [text for text in texts if text.startswith("Result: ")]
                == ([f"Result: {verdict}"] if completed else []),
                f"Shell {flavor}/{case} verdict differs",
            )
            if not completed:
                require(
                    "Validation incomplete: syntax, lint or suppression auditing could not complete."
                    in "\n".join(texts),
                    "Shell syntax probe failed before reaching analysis",
                )
            if case == "suppression":
                require(
                    "Inline suppressions are present; see the untrusted audit report."
                    in "\n".join(texts),
                    "Shell suppression audit is missing",
                )
            require(store.files == {path: code}, "Shell validation changed the source store")
            require(
                len(calls) == len(reports) + 1 and calls[-1].unclean == 0,
                "Shell call cleanup failed",
            )
            require(len(observations) == len(reports) + 1, "Shell call lacks engine observation")
            require(not await asyncio.to_thread(owned, scope), "Shell call left containers behind")
            reports.append(
                {
                    "name": f"{flavor}/{case}",
                    "passed": True,
                    "completed": completed,
                    "verdict": verdict,
                    "sourceUnchanged": True,
                    "unclean": calls[-1].unclean,
                }
            )
    finally:
        purge = await router.dispose_scope(scope, "shell")
        require(not purge.undisposed, "Shell scope cleanup failed")
        require(
            not await asyncio.to_thread(owned, scope), "Shell qualification left containers behind"
        )
    return {
        "backend": "maf-sandbox-docker",
        "sdkSource": "checkout",
        "packages": {
            name: version(name)
            for name in (
                "maf-sandbox",
                "maf-sandbox-docker",
                "maf-sandbox-shell",
                "agent-framework-core",
            )
        },
        "docker": {key: server[key] for key in ("Version", "Os", "Arch")},
        "observations": observations,
        "checks": reports,
        "containersRemaining": [],
        "acasVerified": False,
    }
