"""Qualify the published shell image through ACAS with service-observed disposal."""

# /// script
# requires-python = ">=3.12,<3.15"
# dependencies = [
#   "maf-sandbox>=0.51,<0.52",
#   "maf-sandbox-acas>=0.31.1",
#   "maf-sandbox-shell>=0.1.1",
#   "azure-containerapps-sandbox",
#   "azure-core",
#   "azure-identity",
# ]
# ///

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import platform
import re
import uuid
from importlib.metadata import version
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from azure.containerapps.sandbox.aio import SandboxGroupClient
from azure.core.exceptions import ResourceNotFoundError
from azure.identity.aio import AzureCliCredential
from maf_sandbox import CallerContext, Egress, SandboxKey, SandboxRouter, SandboxSpec
from maf_sandbox.maf import COMPLETED_TEXT
from maf_sandbox.testing import InMemoryStore
from maf_sandbox_acas import AcasSandboxBackend, AcasSandboxConfig, disk_image_base
from maf_sandbox_shell import ShellFlavor, make_shell_tools
from shell_runtime_devices import check_runtime_devices

if TYPE_CHECKING:
    from agent_framework import AgentFileStore

CASES = (
    ("bash-findings", ShellFlavor.BASH, "main.sh", "echo $word\n", "findings"),
    ("pwsh-findings", ShellFlavor.PWSH, "main.ps1", "Write-Host 'hello'\n", "findings"),
    ("bash-empty", ShellFlavor.BASH, "main.sh", "", "clean"),
    ("pwsh-empty", ShellFlavor.PWSH, "main.ps1", "", "clean"),
    ("pwsh-module", ShellFlavor.PWSH, "main.psm1", "function global:Get-Widget {}\n", "findings"),
)


def image_reference(value: str) -> str:
    """Require an immutable public shell image reference for the import mapping."""
    if not re.fullmatch(r"ghcr\.io/sokolaidev/maf-extensions/shell@sha256:[0-9a-f]{64}", value):
        raise ValueError("MAF_SHELL_ACAS_IMAGE must name the published shell registry digest")
    return value


async def require_absent(client: Any, identities: list[str]) -> None:
    """Only a service 404 establishes deletion; authorization and transport errors fail."""
    if not identities:
        raise ValueError("No acquired sandbox was observed")
    for identity in identities:
        for attempt in range(12):
            try:
                await client.get_sandbox_client(identity).get()
            except ResourceNotFoundError:
                break
            if attempt == 11:
                raise ValueError("An acquired shell sandbox remains after disposal")
            await asyncio.sleep(5)


class ObservedBackend(AcasSandboxBackend):
    """Capture physical identities and confirm presence through an independent client."""

    def __init__(self, config: AcasSandboxConfig, client: Any) -> None:
        super().__init__(config)
        self.observed: list[str] = []
        self.runtime_devices: list[dict[str, Any]] = []
        self.client = client

    async def acquire(self, key: SandboxKey, spec: SandboxSpec) -> Any:
        if spec.egress != Egress.CLOSED:
            raise ValueError("Shell qualification requires closed egress")
        sandbox = await super().acquire(key, spec)
        self.observed.append(sandbox.instance_id)
        await self.client.get_sandbox_client(sandbox.instance_id).get()
        self.runtime_devices.append(
            {"sandbox": sandbox.instance_id, "devices": await check_runtime_devices(sandbox)}
        )
        return sandbox


async def qualify(report: dict[str, Any]) -> None:
    """Run every case against one imported digest and retain only verified successes."""
    reference = image_reference(os.environ.get("MAF_SHELL_ACAS_IMAGE", ""))
    config = AcasSandboxConfig(
        endpoint=os.environ["ACAS_SANDBOX_ENDPOINT"],
        subscription_id=os.environ["ACAS_SANDBOX_SUBSCRIPTION_ID"],
        resource_group=os.environ["ACAS_SANDBOX_RESOURCE_GROUP"],
        sandbox_group=os.environ["ACAS_SANDBOX_GROUP"],
    )
    async with AzureCliCredential() as credential:
        async with SandboxGroupClient(
            endpoint=config.endpoint,
            subscription_id=config.subscription_id,
            resource_group=config.resource_group,
            sandbox_group=config.sandbox_group,
            credential=credential,
        ) as client:
            matches = [
                image.id
                async for image in client.list_disk_images()
                if disk_image_base(image) == reference and image.id
            ]
            if len(set(matches)) != 1:
                raise ValueError("Import the exact shell digest once into this sandbox group")
            report.update(image=reference, diskImage=matches[0])
            backend = ObservedBackend(config, client)
            report["sandboxes"] = backend.observed
            report["runtimeDevices"] = backend.runtime_devices
            router = SandboxRouter([backend], min_isolation=backend.isolation)
            scope = "shell-qualification-" + uuid.uuid4().hex
            context = CallerContext(
                current_scope=lambda: scope,
                current_thread_id=lambda: "test",
                list_files=InMemoryStore.list,
            )
            try:
                for name, flavor, path, code, expected in CASES:
                    before = len(backend.observed)
                    store = InMemoryStore({path: code})
                    [tool] = make_shell_tools(
                        router,
                        cast("AgentFileStore", store),
                        "shell-test",
                        context,
                        flavor=flavor,
                        image=reference,
                        image_id=matches[0],
                    )
                    result = await tool.func(files=[path])
                    rendered = "\n".join(str(item.text) for item in result)
                    if (
                        str(result[0].text) != COMPLETED_TEXT
                        or f"Result: {expected}" not in rendered
                    ):
                        raise ValueError(f"{name}: unexpected tool result: {rendered}")
                    if store.files != {path: code}:
                        raise ValueError(f"{name}: source store changed")
                    await require_absent(client, backend.observed[before:])
                    report["cases"].append({"case": name, "result": expected, "disposed": True})
            finally:
                try:
                    purge = await router.dispose_scope(scope, "test")
                    if purge.undisposed is not None:
                        raise ValueError(f"Shell scope purge incomplete: {purge.undisposed}")
                    if backend.observed:
                        await require_absent(client, backend.observed)
                finally:
                    await backend.aclose()
            report["passed"] = True


def main() -> None:
    """Write partial evidence even when qualification or cleanup fails."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    installed = {
        name: version(name)
        for name in (
            "maf-sandbox",
            "maf-sandbox-acas",
            "maf-sandbox-shell",
            "agent-framework-core",
            "azure-containerapps-sandbox",
        )
    }
    print(
        "[measured] installed: " + ", ".join(f"{name} {value}" for name, value in installed.items())
    )
    report: dict[str, Any] = {
        "passed": False,
        "packages": installed,
        "python": platform.python_version(),
        "harnessSha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "sourceCommit": os.environ.get("GITHUB_SHA"),
        "sdkSource": os.environ.get("SOURCE", "unspecified"),
        "cases": [],
    }
    try:
        asyncio.run(qualify(report))
    finally:
        args.report.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
