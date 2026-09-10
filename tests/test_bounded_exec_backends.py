"""Both CLI backends use the bounded reader through their actual execution seam."""

import asyncio
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
def test_backend_exec_caps_live_output_before_a_result_exists(engine, channel):
    async def scenario():
        if engine == "docker":
            backend = DockerSandboxBackend(DockerSandboxConfig(docker_path=sys.executable))
            invoke, sandbox_class = backend._docker, _DockerSandbox
        else:
            backend = WslcSandboxBackend(WslcSandboxConfig(wslc_path=sys.executable))
            invoke, sandbox_class = backend._wslc, _WslcSandbox

        async def run(*args, **kwargs):
            assert "exec" in args
            assert args[-1] == "probe"
            script = f"import os,time; os.write({channel}, b'x'*65536); time.sleep(30)"
            return await invoke("-c", script, **kwargs)

        sandbox = sandbox_class(cast(Any, run), "one", 5, instance_id="one")
        with pytest.raises(SandboxExecOutputLimitExceeded):
            await sandbox.exec_bounded(
                "probe", working_directory="/work", timeout=5, max_output_bytes=1024
            )

    asyncio.run(scenario())
