"""Call the unreleased shell validators through Docker without a model or credentials."""

from __future__ import annotations

import argparse
import asyncio
import uuid

from maf_sandbox import CallerContext, SandboxRouter
from maf_sandbox.testing import InMemoryStore
from maf_sandbox_docker import DockerSandboxBackend, DockerSandboxConfig
from maf_sandbox_shell import ShellFlavor, make_shell_tools


async def run(image: str) -> None:
    """Show findings and suppression auditing, leaving the file store unchanged."""
    backend = await DockerSandboxBackend.create(DockerSandboxConfig())
    router = SandboxRouter([backend], min_isolation=backend.isolation)
    scope = "shell-sample-" + uuid.uuid4().hex
    store = InMemoryStore(
        {
            "deploy.sh": "#!/bin/sh\n# shellcheck disable=SC2086\necho $resource_group\n",
            "deploy.ps1": "Write-Host 'Deployment preview'\n",
        }
    )
    context = CallerContext(
        current_scope=lambda: scope,
        current_thread_id=lambda: "sample",
        list_files=InMemoryStore.list,
    )
    try:
        for flavor, path in ((ShellFlavor.BASH, "deploy.sh"), (ShellFlavor.PWSH, "deploy.ps1")):
            [tool] = make_shell_tools(
                router,
                store,  # pyright: ignore[reportArgumentType] -- protocol-compatible sample store
                "reviewer",
                context,
                flavor=flavor,
                image=image,
            )
            result = await tool.func(files=[path])
            print(f"{tool.name}:")
            for item in result:
                print(item.text)
    finally:
        await router.dispose_scope(scope, "sample")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True, help="Locally built shell image tag or digest")
    asyncio.run(run(parser.parse_args().image))
