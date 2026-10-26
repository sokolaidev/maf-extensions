"""Opt-in shell tools, runtime devices and cleanup through Docker, ACAS and WSLC."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
from maf_sandbox import CallerContext, SandboxRouter
from maf_sandbox.maf import COMPLETED_TEXT
from maf_sandbox.testing import InMemoryStore
from maf_sandbox_shell import ShellFlavor, make_shell_tools

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from shell_runtime_devices import check_runtime_devices  # noqa: E402


@pytest.mark.parametrize("backend_name", ["docker", "acas", "wslc"])
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
def test_real_shell_tool(backend_name, flavor, case, record_property):
    variable = f"MAF_SHELL_{backend_name.upper()}_IMAGE"
    image = os.environ.get(variable)
    if not image:
        pytest.skip(f"needs {variable} and configured backend credentials")
    record_property("image", image)

    async def scenario():
        acas_backend = None
        if backend_name == "docker":
            from maf_sandbox_docker import DockerSandboxBackend, DockerSandboxConfig

            backend = await DockerSandboxBackend.create(DockerSandboxConfig())
        elif backend_name == "wslc":
            from maf_sandbox_wslc import WslcSandboxBackend, WslcSandboxConfig

            backend = await WslcSandboxBackend.create(WslcSandboxConfig())
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
        identities = []

        class Router(SandboxRouter):
            async def acquire(self, key, spec, **kwargs):
                sandbox = await super().acquire(key, spec, **kwargs)
                identities.append(sandbox.instance_id)
                if backend_name == "wslc":
                    observed = await asyncio.to_thread(observe_wslc, sandbox.instance_id, image)
                    record_property("engine_observation", json.dumps(observed))
                devices = await check_runtime_devices(sandbox)
                record_property("runtime_devices", json.dumps(devices))
                return sandbox

        router = Router([backend], min_isolation=backend.isolation)
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
            assert len(identities) == 1
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
            elif backend_name == "wslc":
                await asyncio.to_thread(require_wslc_absent, identities)
        finally:
            try:
                purge = await router.dispose_scope(scope, "test")
                assert purge.undisposed is None
            finally:
                if acas_backend is not None:
                    await acas_backend.aclose()
                if backend_name == "wslc":
                    await asyncio.to_thread(require_wslc_absent, identities)

    asyncio.run(scenario())


def observe_wslc(identity, image):
    """Require the selected immutable image and closed network on a running container."""
    result = subprocess.run(
        ["wslc", "container", "inspect", identity],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    rows = json.loads(result.stdout)
    assert len(rows) == 1
    row = rows[0]
    assert row["Id"] == identity and row["State"]["Running"] is True
    assert row["Image"] == image and row["Config"]["Image"] == image
    assert row["HostConfig"]["NetworkMode"] == "none"
    assert set(row["NetworkSettings"]["Networks"]) <= {"none"}
    return {"containerId": identity, "imageId": image, "network": "none"}


def require_wslc_absent(identities):
    """Check all engine containers, including stopped ones, independently of the SDK."""
    result = subprocess.run(
        ["wslc", "container", "list", "-a", "--format", "json"],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    payload = result.stdout.strip()
    rows = (
        json.loads(payload)
        if payload.startswith("[")
        else [json.loads(line) for line in payload.splitlines() if line.strip()]
    )
    assert isinstance(rows, list) and all(isinstance(row, dict) for row in rows)
    assert not any(identity in json.dumps(rows) for identity in identities)
