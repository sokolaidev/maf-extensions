"""Opt-in real-engine calls through the Docker adapter, with daemon-observed disposal."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest
from maf_sandbox import CallerContext, SandboxRouter
from maf_sandbox.testing import InMemoryStore

from maf_sandbox_terraform import make_terraform_tools

pytest.importorskip("maf_sandbox_docker", exc_type=ImportError)
from maf_sandbox_docker import DockerSandboxBackend, DockerSandboxConfig  # noqa: E402

IMAGES = {
    engine: os.environ.get(f"MAF_{engine.upper()}_E2E_IMAGE", "")
    for engine in ("terraform", "opentofu")
}
pytestmark = pytest.mark.skipif(
    not all(IMAGES.values()) or not shutil.which("docker"),
    reason="needs Docker, MAF_TERRAFORM_E2E_IMAGE and MAF_OPENTOFU_E2E_IMAGE (random profiles)",
)
_RANDOM = """terraform {
  required_providers {
    random = { source = "hashicorp/random", version = "3.7.2" }
  }
}
resource "random_integer" "value" {
  min = 1
  max = 10
}
"""
_LOCAL = """variable "required" { type = string }
module "child" { source = "../modules/child" }
output "hello" { value = module.child.hello }
"""


async def containers(scope: str) -> set[str]:
    process = await asyncio.create_subprocess_exec(
        "docker",
        "ps",
        "-aq",
        "--no-trunc",
        "--filter",
        f"label=maf-sandbox.scope={scope}",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(process.communicate(), 30)
    assert process.returncode == 0, stderr.decode()
    return set(stdout.decode().split())


@pytest.mark.parametrize("engine", ["terraform", "opentofu"])
@pytest.mark.parametrize(
    "case",
    [
        "local",
        "json",
        "invalid",
        "syntax",
        "missing-dependency",
        "provider",
        "provider-invalid",
        "formatting",
        "wrong-engine",
        "cancelled",
        "tofu-precedence",
        "timeout",
    ],
)
def _body(result) -> str:
    """What the call said about the configuration, between completion line and guidance.

    The wrapper renders a fixed completion sentence first, an optional verdict, then this
    tool's own text and the engine's, and the committed sentence last. These tests are about
    what the text says, so they read the middle whole.
    """
    return chr(10).join(str(item.text) for item in result[1:-1])


def test_real_calls_dispose_without_mutating_store(engine, case, monkeypatch):
    async def scenario():
        scope = "terraform-1246-" + uuid.uuid4().hex
        backend = await DockerSandboxBackend.create(DockerSandboxConfig())
        router = SandboxRouter([backend], min_isolation=backend.isolation)
        data = {
            "root/main.tf": _LOCAL,
            "modules/child/main.tf": 'output "hello" { value = "hi" }\n',
        }
        root = "root"
        if case == "json":
            data = {"main.tf.json": '{"output":{"hello":{"value":"world"}}}'}
        elif case == "invalid":
            data = {"main.tf": 'output "hello" { value = var.undeclared }\n'}
        elif case == "syntax":
            data = {"main.tf": "this is not HCL !"}
        elif case == "missing-dependency":
            data = {"main.tf": 'module "child" { source = "./absent" }\n'}
        elif case in {"provider", "provider-invalid", "cancelled", "timeout"}:
            data = {
                "main.tf": _RANDOM.replace("min = 1", 'min = "wrong"')
                if case == "provider-invalid"
                else _RANDOM
            }
        elif case == "formatting":
            data = {"main.tf": "locals {\nx=1\nlonger   = 2\n}\n"}
        elif case == "tofu-precedence":
            data = {"main.tf": "invalid !", "main.tofu": 'output "hello" { value = "hi" }\n'}
        if "root/main.tf" not in data:
            root = "."
        store = InMemoryStore(data.copy())
        context = CallerContext(
            current_scope=lambda: scope,
            current_thread_id=lambda: "live",
            list_files=InMemoryStore.list,
        )
        image = (
            IMAGES["opentofu" if engine == "terraform" else "terraform"]
            if case == "wrong-engine"
            else IMAGES[engine]
        )
        tool = make_terraform_tools(
            router,
            store,
            "live",
            context,
            engine=engine,
            image=image,
            exec_timeout_seconds=0.001 if case == "timeout" else 30,
        )[0]
        instances: list[str] = []
        started = asyncio.Event()
        original_acquire = backend.acquire

        async def acquire(key, spec):
            sandbox = await original_acquire(key, spec)
            observed = await containers(scope)
            assert sandbox.instance_id in observed
            instances.append(sandbox.instance_id)
            if case == "cancelled":
                original_exec = sandbox.exec

                async def execute(*args, **kwargs):
                    # Enter the adapter's real exec task before cancelling the tool's wait.
                    running = asyncio.create_task(original_exec(*args, **kwargs))
                    await asyncio.sleep(0.05)
                    started.set()
                    return await running

                monkeypatch.setattr(sandbox, "exec", execute)
            return sandbox

        monkeypatch.setattr(backend, "acquire", acquire)
        try:
            for _ in range(2):
                if case == "cancelled":
                    started.clear()
                    pending = asyncio.create_task(tool.func(files=list(data), root_module=root))
                    await asyncio.wait_for(started.wait(), 30)
                    pending.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await pending
                else:
                    result = await tool.func(files=list(data), root_module=root)
                    report = _body(result)
                    if case in {"syntax", "missing-dependency", "wrong-engine", "timeout"} or (
                        case == "tofu-precedence" and engine == "terraform"
                    ):
                        assert "INCOMPLETE" in report, report
                    elif case in {"invalid", "provider-invalid"}:
                        assert "validation FAIL" in report, report
                    else:
                        assert "validation PASS" in report, report
                        if case == "formatting":
                            assert "formatting CHANGES REQUIRED" in report, report
                assert not await containers(scope)
                assert store.files == data
            assert len(instances) == len(set(instances))
        finally:
            await router.dispose_scope(scope, "live")

    asyncio.run(scenario())


@pytest.mark.parametrize("engine", ["terraform", "opentofu"])
def test_launcher_supervision_in_linux(engine):
    source = Path(__file__).resolve().parents[3] / "images/terraform-sandbox/test_runner.py"
    result = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "--read-only",
            "--tmpfs",
            "/tmp:rw,exec,size=256m",
            "-i",
            IMAGES[engine],
            "python3",
            "-",
        ],
        input=source.read_bytes(),
        capture_output=True,
        # Above the guest budgets it contains: the lock test alone may spend 30 seconds per
        # call over three calls. A cap below that turns a slow runner into a failure here
        # instead of a verdict from the suite.
        timeout=150,
    )
    assert result.returncode == 0, result.stderr.decode()


@pytest.mark.parametrize("engine", ["terraform", "opentofu"])
@pytest.mark.parametrize("case", ["changed", "unchanged", "oversized", "escaped", "syntax"])
def test_format_returns_complete_files_without_store_writes(engine, case, monkeypatch):
    async def scenario():
        scope = "terraform-format-" + uuid.uuid4().hex
        backend = await DockerSandboxBackend.create(DockerSandboxConfig())
        router = SandboxRouter([backend], min_isolation=backend.isolation)
        original = "locals {\nx=1\nlonger   = 2\n}\n"
        formatted = "locals {\n  x      = 1\n  longer = 2\n}\n"
        data = {"root/main.tf": original, "modules/clean/main.tf": formatted}
        if case == "unchanged":
            data["root/main.tf"] = formatted
        elif case in {"oversized", "escaped"}:
            # The escaped case fits as UTF-8 but exceeds the bound when JSON-encoded.
            payload = "x" * (128 * 1024) if case == "oversized" else "\u00e9" * 24000
            data["root/main.tf"] = f'locals {{\nx="{payload}"\n}}\n'
        elif case == "syntax":
            data["root/broken.tf"] = "invalid !"
        store = InMemoryStore(data.copy())
        context = CallerContext(
            current_scope=lambda: scope,
            current_thread_id=lambda: "live",
            list_files=InMemoryStore.list,
        )
        tools = make_terraform_tools(
            router, store, "live", context, engine=engine, image=IMAGES[engine], formatting=True
        )
        observed = []
        original_acquire = backend.acquire

        async def acquire(key, spec):
            sandbox = await original_acquire(key, spec)
            assert sandbox.instance_id in await containers(scope)
            observed.append(sandbox.instance_id)
            return sandbox

        monkeypatch.setattr(backend, "acquire", acquire)
        try:
            result = await tools[1].func(files=list(data), root_module="root")
            report = _body(result)
            assert store.files == data
            if case in {"oversized", "escaped", "syntax"}:
                assert "Formatting INCOMPLETE" in report, report
                assert "mapping):" not in report
            else:
                files = json.loads(report.split("mapping):\n")[1])
                assert files == ({"root/main.tf": formatted} if case == "changed" else {})
                if files:
                    # A host file tool can persist the returned whole text for check-only validation.
                    store.files.update(files)
                    checked = await tools[0].func(files=list(data), root_module="root")
                    assert "formatting PASS" in checked[0].text, checked[0].text
                    store.files.update(data)
            assert store.files == data
            assert observed and not await containers(scope)
            assert len(observed) == len(set(observed))
        finally:
            await router.dispose_scope(scope, "live")

    asyncio.run(scenario())
