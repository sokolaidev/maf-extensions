"""Run a local-module or mirrored-provider validation from the repository checkout."""

import argparse
import asyncio
import hashlib
import json
import uuid
from pathlib import Path
from typing import cast

from agent_framework import AgentFileStore
from maf_sandbox import CallerContext, SandboxRouter
from maf_sandbox.testing import InMemoryStore
from maf_sandbox_docker import DockerSandboxBackend, DockerSandboxConfig
from maf_sandbox_terraform import TerraformEngine, make_terraform_tools


async def validate(
    engine: TerraformEngine, image: str, provider: bool, prepared: Path | None = None
) -> None:
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
    if prepared is not None:
        receipt = json.loads((prepared / "receipt.json").read_text(encoding="utf-8"))
        if receipt["engine"] != engine:
            raise ValueError("prepared dependencies belong to a different engine")
        dependency = receipt["providers"][0]
        module = receipt["modules"][0]
        files = {
            "root/main.tf": (
                "terraform {\n  required_providers {\n"
                f'    random = {{ source = "{dependency["source"]}", '
                f'version = "{dependency["version"]}" }}\n'
                '  }\n}\nmodule "approved" {\n'
                f'  source = "../modules/{module["name"]}"\n'
                "}\n"
            )
        }
        for name, digest in module["files"].items():
            data = (prepared / "modules" / module["name"] / name).read_bytes()
            if hashlib.sha256(data).hexdigest() != digest:
                raise ValueError("prepared module content has changed")
            files[f"modules/{module['name']}/{name}"] = data.decode("utf-8")
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
    profile = parser.add_mutually_exclusive_group()
    profile.add_argument("--provider", action="store_true")
    profile.add_argument("--prepared", type=Path, help="verified output from the example manifest")
    arguments = parser.parse_args()
    asyncio.run(validate(arguments.engine, arguments.image, arguments.provider, arguments.prepared))
