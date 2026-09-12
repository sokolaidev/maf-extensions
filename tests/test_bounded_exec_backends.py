"""Both CLI backends use the bounded reader through their actual execution seam."""

import asyncio
import contextlib
import sys
from typing import Any, cast

import pytest
from maf_sandbox import SandboxExecOutputLimitExceeded
from maf_sandbox_docker import DockerSandboxBackend, DockerSandboxConfig
from maf_sandbox_docker._backend import _DockerSandbox
from maf_sandbox_wslc import WslcSandboxBackend, WslcSandboxConfig
from maf_sandbox_wslc._backend import _WslcSandbox


@pytest.mark.parametrize("engine", ["docker", "wslc"])
@pytest.mark.parametrize("channel", [1, 2])
@pytest.mark.parametrize("directory", ["/work", "child"])
def test_backend_exec_caps_live_output_before_a_result_exists(engine, channel, directory):
    async def scenario():
        if engine == "docker":
            backend = DockerSandboxBackend(DockerSandboxConfig(docker_path=sys.executable))
            # Never taken here: nothing below is a tar-plane member, and the engine is a
            # Python interpreter rather than a daemon with a container to freeze.
            invoke, sandbox_class = backend._docker, _DockerSandbox
            extra: dict[str, Any] = {"freeze": contextlib.nullcontext}
        else:
            backend = WslcSandboxBackend(WslcSandboxConfig(wslc_path=sys.executable))
            invoke, sandbox_class = backend._wslc, _WslcSandbox
            extra = {}

        async def run(*args, **kwargs):
            assert "exec" in args
            assert args[-1] == "probe"
            assert args[args.index("-w") + 1] == (
                "/work" if directory == "/work" else "/maf-sandbox/work/child"
            )
            script = f"import os,time; os.write({channel}, b'x'*65536); time.sleep(30)"
            return await invoke("-c", script, **kwargs)

        sandbox = sandbox_class(cast(Any, run), "one", 5, instance_id="one", **extra)
        with pytest.raises(SandboxExecOutputLimitExceeded):
            await sandbox.exec_bounded(
                "probe", working_directory=directory, timeout=5, max_output_bytes=1024
            )

    asyncio.run(scenario())


@pytest.mark.parametrize("engine", ["docker", "wslc"])
def test_bounded_backend_preserves_both_binary_streams(engine):
    async def scenario():
        if engine == "docker":
            backend = DockerSandboxBackend(DockerSandboxConfig(docker_path=sys.executable))
            # Never taken here: nothing below is a tar-plane member, and the engine is a
            # Python interpreter rather than a daemon with a container to freeze.
            invoke, sandbox_class = backend._docker, _DockerSandbox
            extra: dict[str, Any] = {"freeze": contextlib.nullcontext}
        else:
            backend = WslcSandboxBackend(WslcSandboxConfig(wslc_path=sys.executable))
            invoke, sandbox_class = backend._wslc, _WslcSandbox
            extra = {}

        async def run(*args, **kwargs):
            script = "import os; os.write(1, bytes(range(256))); os.write(2, bytes(range(255,-1,-1))); raise SystemExit(7)"
            return await invoke("-c", script, **kwargs)

        sandbox = sandbox_class(cast(Any, run), "one", 5, instance_id="one", **extra)
        result = await sandbox.exec_bounded(
            "probe", working_directory="/", timeout=5, max_output_bytes=1024
        )
        assert result.stdout_bytes == bytes(range(256))
        assert result.stderr_bytes == bytes(range(255, -1, -1))
        assert result.exit_code == 7

    asyncio.run(scenario())
