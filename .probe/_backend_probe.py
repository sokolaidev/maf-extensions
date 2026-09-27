"""Temporary: acquire a sandbox from an image through the real backend and run one command."""

import asyncio
import sys
import tempfile
import time
from pathlib import Path

from maf_sandbox import Capability, Egress, SandboxKey, SandboxSpec
from maf_sandbox_docker_sbx import SbxSandboxBackend, SbxSandboxConfig


async def main(image: str, probe: str) -> None:
    backend = SbxSandboxBackend(SbxSandboxConfig(workspace_root=Path(tempfile.mkdtemp()), cpus=2))
    key = SandboxKey(scope=f"probe-{int(time.time())}", thread_id="t", agent_id="a")
    spec = SandboxSpec(
        kind="probe",
        image=image,
        egress=Egress.CLOSED,
        requires=frozenset({Capability.EXEC, Capability.FILES_IN, Capability.FILES_OUT}),
    )
    try:
        sandbox = await backend.acquire(key, spec)
        await sandbox.write_file("in.txt", b"hello", working_directory=".")
        result = await sandbox.exec(["sh", "-c", probe], working_directory=".", timeout=120)
        print(f"BACKEND exit {result.exit_code}\n{result.stdout}\n{result.stderr}")
    except Exception as error:  # noqa: BLE001 - a probe reports everything
        print(f"BACKEND {type(error).__name__}: {error}")
    finally:
        print(f"dispose: {await backend.dispose(key)}")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1], sys.argv[2]))
