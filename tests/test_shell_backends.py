"""Opt-in real shell tools through Docker and ACAS, including disposal and unchanged stores."""

from __future__ import annotations

import asyncio
import os
import uuid

import pytest
from maf_sandbox import CallerContext, SandboxRouter
from maf_sandbox.maf import COMPLETED_TEXT
from maf_sandbox.testing import InMemoryStore
from maf_sandbox_shell import ShellFlavor, make_shell_tools


@pytest.mark.parametrize("backend_name", ["docker", "acas"])
@pytest.mark.parametrize(
    "flavor,case",
    [
        (ShellFlavor.BASH, "findings"),
        (ShellFlavor.PWSH, "findings"),
        (ShellFlavor.BASH, "empty"),
        (ShellFlavor.PWSH, "empty"),
        (ShellFlavor.PWSH, "module"),
    ],
)
def test_real_shell_tool(backend_name, flavor, case):
    variable = f"MAF_SHELL_{backend_name.upper()}_IMAGE"
    image = os.environ.get(variable)
    if not image:
        pytest.skip(f"needs {variable} and configured backend credentials")

    async def scenario():
        acas_backend = None
        if backend_name == "docker":
            from maf_sandbox_docker import DockerSandboxBackend, DockerSandboxConfig

            backend = await DockerSandboxBackend.create(DockerSandboxConfig())
        else:
            from maf_sandbox_acas import AcasSandboxBackend, AcasSandboxConfig

            backend = AcasSandboxBackend(
                AcasSandboxConfig(
                    endpoint=os.environ["ACAS_SANDBOX_ENDPOINT"],
                    subscription_id=os.environ["ACAS_SANDBOX_SUBSCRIPTION_ID"],
                    resource_group=os.environ["ACAS_SANDBOX_RESOURCE_GROUP"],
                    sandbox_group=os.environ["ACAS_SANDBOX_GROUP"],
                    registry=os.environ.get("ACAS_SANDBOX_REGISTRY", ""),
                )
            )
            acas_backend = backend
        router = SandboxRouter([backend], min_isolation=backend.isolation)
        scope = "shell-qualification-" + uuid.uuid4().hex
        context = CallerContext(
            current_scope=lambda: scope,
            current_thread_id=lambda: "test",
            list_files=InMemoryStore.list,
        )
        path, code = (
            ("-main.sh", "echo $word\n")
            if flavor == ShellFlavor.BASH
            else ("-main.ps1", "Write-Host 'hello'\n")
        )
        if case == "empty":
            code = ""
        elif case == "module":
            path, code = "-main.psm1", "function global:Get-Widget {}\n"
        store = InMemoryStore({path: code})
        try:
            [tool] = make_shell_tools(
                router, store, "shell-test", context, flavor=flavor, image=image
            )
            result = await tool.func(files=[path])
            rendered = "\n".join(str(item.text) for item in result)
            assert str(result[0].text) == COMPLETED_TEXT, rendered
            expected = "clean" if case == "empty" else "findings"
            assert f"Result: {expected}" in rendered, rendered
            assert store.files == {path: code}
            if backend_name == "docker":
                process = await asyncio.create_subprocess_exec(
                    "docker",
                    "ps",
                    "-aq",
                    "--filter",
                    f"label=maf-sandbox.scope={scope}",
                    stdout=asyncio.subprocess.PIPE,
                )
                stdout, _ = await asyncio.wait_for(process.communicate(), 30)
                assert process.returncode == 0 and not stdout.strip()
        finally:
            await router.dispose_scope(scope, "test")
            if acas_backend is not None:
                await acas_backend.aclose()

    asyncio.run(scenario())
