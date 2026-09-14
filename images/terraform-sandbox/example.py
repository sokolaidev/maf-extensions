"""Run a local-module or mirrored-provider validation from the repository checkout."""

import argparse
import asyncio
import uuid
from typing import cast

from agent_framework import AgentFileStore
from maf_sandbox import CallerContext, SandboxRouter
from maf_sandbox.testing import InMemoryStore
from maf_sandbox_docker import DockerSandboxBackend, DockerSandboxConfig
from maf_sandbox_terraform import TerraformEngine, make_terraform_tools


async def validate(engine: TerraformEngine, image: str, provider: bool) -> None:
    """Wire a real Docker backend and invoke the selected tool without a model or credentials."""
    backend = await DockerSandboxBackend.create(DockerSandboxConfig())
    router = SandboxRouter([backend], min_isolation=backend.isolation)
    scope = "terraform-example-" + uuid.uuid4().hex
    files = {
        "root/main.tf": 'module "child" { source = "../modules/child" }\n',
        "modules/child/main.tf": 'output "hello" { value = "world" }\n',
    }
    if provider:
        files = {
            "main.tf": """terraform {
  required_providers {
    random = { source = "hashicorp/random", version = "3.7.2" }
  }
}
resource "random_integer" "example" {
  min = 1
  max = 10
}
"""
        }
    store = InMemoryStore(files)
    context = CallerContext(
        current_scope=lambda: scope,
        current_thread_id=lambda: "example",
        list_files=InMemoryStore.list,
    )
    # The in-memory demonstration store supplies the read surface used by this workload.
    tool = make_terraform_tools(
        router, cast(AgentFileStore, store), "example", context, engine=engine, image=image
    )[0]
    try:
        result = await tool.func(files=list(files), root_module="." if provider else "root")
        for item in result:
            print(item.text)
    finally:
        await router.dispose_scope(scope, "example")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine", choices=["terraform", "opentofu"], default="terraform")
    parser.add_argument("--image", required=True)
    parser.add_argument("--provider", action="store_true")
    arguments = parser.parse_args()
    asyncio.run(validate(arguments.engine, arguments.image, arguments.provider))
