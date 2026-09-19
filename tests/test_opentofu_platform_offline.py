"""Opt-in Docker and ACAS validation of the prepared OpenTofu platform image."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import uuid
from pathlib import Path
from typing import Any

import pytest
from maf_sandbox import CallerContext, SandboxRouter
from maf_sandbox.testing import InMemoryStore
from maf_sandbox_terraform import make_terraform_tools

MANIFEST = (
    Path(__file__).resolve().parents[1]
    / "images/terraform-sandbox/dependencies.opentofu-platform.json"
)
DOCKER_IMAGE = os.environ.get("MAF_OPENTOFU_PLATFORM_IMAGE", "")
ACAS_IMAGE = os.environ.get("MAF_OPENTOFU_PLATFORM_ACAS_IMAGE", "")
BACKENDS = [
    pytest.param(
        "docker",
        marks=pytest.mark.skipif(not DOCKER_IMAGE, reason="needs MAF_OPENTOFU_PLATFORM_IMAGE"),
    ),
    pytest.param(
        "acas",
        marks=pytest.mark.skipif(
            not ACAS_IMAGE
            or not all(
                os.environ.get(name)
                for name in (
                    "ACAS_SANDBOX_ENDPOINT",
                    "ACAS_SANDBOX_SUBSCRIPTION_ID",
                    "ACAS_SANDBOX_RESOURCE_GROUP",
                    "ACAS_SANDBOX_GROUP",
                    "ACAS_SANDBOX_REGISTRY",
                )
            ),
            reason="needs MAF_OPENTOFU_PLATFORM_ACAS_IMAGE and ACAS_SANDBOX_*",
        ),
    ),
]
ROOTS = {
    "azure": (
        "hashicorp/azurerm",
        "azurerm",
        'provider "azurerm" {\n  features {}\n}\n',
        "azurerm_resource_group",
        '  name     = "example"\n  location = "westeurope"\n',
    ),
    "databricks": (
        "databricks/databricks",
        "databricks",
        "",
        "databricks_cluster",
        '  cluster_name  = "example"\n  spark_version = "15.4.x-scala2.12"\n  node_type_id  = "Standard_DS3_v2"\n  num_workers   = 1\n',
    ),
    "fabric": (
        "microsoft/fabric",
        "fabric",
        "",
        "fabric_workspace",
        '  display_name = "example"\n',
    ),
    "azuredevops": (
        "microsoft/azuredevops",
        "azuredevops",
        "",
        "azuredevops_project",
        '  name = "example"\n',
    ),
}


def project(platform: str, invalid: bool) -> dict[str, str]:
    source, name, configuration, resource, arguments = ROOTS[platform]
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    version = next(
        item["version"]
        for item in manifest["providers"]
        if item["source"] == f"registry.opentofu.org/{source}"
    )
    if invalid:
        arguments += '  unsupported_platform_argument = "invalid"\n'
    return {
        "root/main.tf": (
            "terraform {\n  required_providers {\n"
            f"    {name} = {{\n"
            f'      source  = "registry.opentofu.org/{source}"\n'
            f'      version = "{version}"\n'
            "    }\n  }\n}\n\n"
            f'{configuration}\nresource "{resource}" "example" {{\n{arguments}}}\n'
        )
    }


async def create_backend(name: str) -> Any:
    if name == "docker":
        from maf_sandbox_docker import DockerSandboxBackend, DockerSandboxConfig

        return await DockerSandboxBackend.create(DockerSandboxConfig())
    from maf_sandbox_acas import AcasSandboxBackend, AcasSandboxConfig

    return AcasSandboxBackend(
        AcasSandboxConfig(
            endpoint=os.environ["ACAS_SANDBOX_ENDPOINT"],
            subscription_id=os.environ["ACAS_SANDBOX_SUBSCRIPTION_ID"],
            resource_group=os.environ["ACAS_SANDBOX_RESOURCE_GROUP"],
            sandbox_group=os.environ["ACAS_SANDBOX_GROUP"],
            registry=os.environ["ACAS_SANDBOX_REGISTRY"],
        )
    )


@pytest.mark.parametrize("backend_name", BACKENDS)
@pytest.mark.parametrize("platform", ROOTS)
@pytest.mark.parametrize("invalid", [False, True], ids=["valid", "schema-error"])
def test_platform_resource_validates_offline(backend_name, platform, invalid, monkeypatch):
    async def scenario() -> None:
        files = project(platform, invalid)
        store = InMemoryStore(files.copy())
        scope = "opentofu-platform-" + uuid.uuid4().hex
        context = CallerContext(
            current_scope=lambda: scope,
            current_thread_id=lambda: "live",
            list_files=InMemoryStore.list,
        )
        backend = await create_backend(backend_name)
        router = SandboxRouter([backend], min_isolation=backend.isolation)
        acquire = backend.acquire
        instances: list[str] = []

        async def checked_acquire(key, spec):
            sandbox = await acquire(key, spec)
            instances.append(sandbox.instance_id)
            if backend_name == "docker":
                inspection = subprocess.run(
                    ["docker", "inspect", sandbox.instance_id],
                    capture_output=True,
                    check=True,
                    timeout=10,
                )
                assert json.loads(inspection.stdout)[0]["HostConfig"]["NetworkMode"] == "none"
            return sandbox

        monkeypatch.setattr(backend, "acquire", checked_acquire)
        tool = make_terraform_tools(
            router,
            store,
            "live",
            context,
            engine="opentofu",
            image=DOCKER_IMAGE if backend_name == "docker" else ACAS_IMAGE,
        )[0]
        try:
            result = await tool.func(files=list(files), root_module="root")
            text = "\n".join(item.text or "" for item in result)
            assert "Validation INCOMPLETE" not in text, text
            if invalid:
                assert "Unsupported argument" in text, text
                assert "validation PASS" not in text, text
            else:
                assert "validation PASS (0 errors, 0 warnings)" in text, text
            assert store.files == files
            assert len(instances) == 1
            if backend_name == "docker":
                inspected = subprocess.run(
                    ["docker", "inspect", instances[0]], capture_output=True, timeout=10
                )
                assert inspected.returncode != 0
        finally:
            await router.dispose_scope(scope, "live")
            if backend_name == "acas":
                await backend.aclose()

    asyncio.run(scenario())
