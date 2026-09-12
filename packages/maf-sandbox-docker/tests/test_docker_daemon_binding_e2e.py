"""Two real engines must keep same-named containers and freeze records independent."""

from __future__ import annotations

import asyncio
import os
import subprocess
from uuid import uuid4

import pytest
from maf_sandbox import SandboxKey, SandboxSpec

from maf_sandbox_docker import DockerSandboxBackend, DockerSandboxConfig
from maf_sandbox_docker._backend import _container_name, _Freezes

_FIRST = os.environ.get("MAF_SANDBOX_DOCKER_FIRST_HOST", "")
_SECOND = os.environ.get("MAF_SANDBOX_DOCKER_SECOND_HOST", "")
_IMAGE = os.environ.get("MAF_SANDBOX_DOCKER_E2E_IMAGE", "")
pytestmark = pytest.mark.skipif(
    not (_FIRST and _SECOND and _IMAGE), reason="two Docker endpoints and an image are required"
)


@pytest.mark.parametrize("selection", ["context", "host"])
def test_context_switch_keeps_acquire_freeze_and_disposal_on_their_engine(
    monkeypatch, tmp_path, selection
):
    monkeypatch.setenv("DOCKER_CONFIG", str(tmp_path))
    for variable in ("DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_TLS", "DOCKER_TLS_VERIFY"):
        monkeypatch.delenv(variable, raising=False)

    def client(*args):
        return subprocess.run(
            ["docker", *args], capture_output=True, text=True, check=True, timeout=30
        ).stdout.strip()

    client("context", "create", "first", "--docker", f"host={_FIRST}")
    client("context", "create", "second", "--docker", f"host={_SECOND}")
    key = SandboxKey(scope=f"binding-{uuid4().hex}", thread_id="thread", agent_dir="agent")
    spec = SandboxSpec(kind="binding", image=_IMAGE, requires=frozenset())
    name = _container_name(key, spec.kind)

    async def scenario():
        client("context", "use", "first")
        if selection == "host":
            monkeypatch.setenv("DOCKER_HOST", _FIRST)
        first = await DockerSandboxBackend.create(DockerSandboxConfig())
        client("context", "use", "second")
        if selection == "host":
            monkeypatch.setenv("DOCKER_HOST", _SECOND)
        second = await DockerSandboxBackend.create(DockerSandboxConfig())
        try:
            await first.acquire(key, spec)
            await second.acquire(key, spec)
            first_id = await first._docker("inspect", "--format", "{{.Id}}", name)
            second_id = await second._docker("inspect", "--format", "{{.Id}}", name)
            assert first_id.returncode == second_id.returncode == 0
            assert first_id.stdout != second_id.stdout
            async with first._frozen(name):
                assert not _Freezes.claimed(second._freeze_key(name))
                await second.acquire(key, spec)
                assert (await first._container_state(name)) == (True, True)
                assert (await second._container_state(name)) == (True, False)
                forged = await second._docker(
                    "exec",
                    name,
                    "sh",
                    "-c",
                    "echo attempt >> /attempts; "
                    f"echo 'Error response from daemon: Container {name} is paused, "
                    "unpause the container before exec' >&2; exit 1",
                    container=name,
                    timeout=1,
                )
                assert forged.returncode == 1
                attempts = await second._docker("exec", name, "cat", "/attempts")
                assert attempts.stdout == b"attempt\n"
                client("context", "use", "first")
                monkeypatch.setenv("DOCKER_HOST", _SECOND)
                monkeypatch.setenv("DOCKER_CONTEXT", "second")
                assert (await first._container_state(name)) == (True, True)
            assert (await first._container_state(name)) == (True, False)
            await first.dispose(key)
            assert not await first._exists(name)
            assert await second._exists(name)
        finally:
            await first.dispose(key)
            await second.dispose(key)

    asyncio.run(scenario())
