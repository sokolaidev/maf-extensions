"""Opt-in live byte capture and disposal, with three disposable sandboxes per image."""

from __future__ import annotations

import asyncio
import os
from uuid import uuid4

import pytest
from maf_sandbox import Capability, SandboxKey, SandboxOutputError, SandboxSpec
from maf_sandbox.conformance import PosixGuestSubject, assert_exec_conformance

from maf_sandbox_acas import AcasSandboxBackend, AcasSandboxConfig

pytestmark = pytest.mark.skipif(
    not os.environ.get("ACAS_SANDBOX_ENDPOINT"), reason="requires a live ACAS sandbox group"
)


@pytest.mark.parametrize(
    "image",
    ["python-3.13", os.environ.get("MAF_SANDBOX_ACAS_E2E_NONROOT_IMAGE")],
    ids=["prebuilt", "nonroot"],
)
def test_byte_capture_and_failure_disposal(image):
    if not image:
        pytest.skip("requires MAF_SANDBOX_ACAS_E2E_NONROOT_IMAGE")

    async def scenario():
        backend = AcasSandboxBackend(
            AcasSandboxConfig(
                endpoint=os.environ["ACAS_SANDBOX_ENDPOINT"],
                subscription_id=os.environ["ACAS_SANDBOX_SUBSCRIPTION_ID"],
                resource_group=os.environ["ACAS_SANDBOX_RESOURCE_GROUP"],
                sandbox_group=os.environ["ACAS_SANDBOX_GROUP"],
                exec_output_limit_bytes=65536,
            )
        )
        key = SandboxKey("exec-bytes-" + uuid4().hex, "validation", "bytes")
        spec = SandboxSpec(
            kind="bytes", image=image, work_dir="/tmp", requires=frozenset({Capability.EXEC})
        )
        try:
            sandbox = await backend.acquire(key, spec)
            program = [
                "python3",
                "-c",
                "import sys; p=bytes(range(256))*256; sys.stdout.buffer.write(p); sys.stderr.buffer.write(p[::-1]); sys.exit(7)",
            ]
            results = await asyncio.gather(
                *(sandbox.exec(program, working_directory="/tmp", timeout=60) for _ in range(2))
            )
            for result in results:
                assert result.stdout_bytes == bytes(range(256)) * 256
                assert result.stderr_bytes == (bytes(range(256)) * 256)[::-1]
                assert result.exit_code == 7
            background = await sandbox.exec(
                "printf before; (sleep .1; printf after) &", working_directory="/tmp", timeout=60
            )
            assert background.stdout_bytes == b"beforeafter"
            first_id = sandbox.instance_id
            with pytest.raises(SandboxOutputError, match="exceeded"):
                await sandbox.exec(
                    ["python3", "-c", "import sys; sys.stdout.buffer.write(b'x'*65537)"],
                    working_directory="/tmp",
                    timeout=60,
                )
            assert not [
                item
                async for item in backend._group_client().list_sandboxes(
                    labels={"scope": key.scope}
                )
            ]
            sandbox = await backend.acquire(key, spec)
            assert sandbox.instance_id != first_id
            results = await assert_exec_conformance(
                PosixGuestSubject(
                    sandbox, "/tmp", backend.declarations.capabilities, exec_cleanup_timeout=30
                )
            )
            assert not any(result.skipped for result in results)
            assert not [
                item
                async for item in backend._group_client().list_sandboxes(
                    labels={"scope": key.scope}
                )
            ]
            sandbox = await backend.acquire(key, spec)
            task = asyncio.create_task(
                sandbox.exec("sleep 30", working_directory="/tmp", timeout=60)
            )
            await asyncio.sleep(1)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert not [
                item
                async for item in backend._group_client().list_sandboxes(
                    labels={"scope": key.scope}
                )
            ]
        finally:
            try:
                await backend.dispose_scope(key.scope, key.thread_id)
                assert not [
                    item
                    async for item in backend._group_client().list_sandboxes(
                        labels={"scope": key.scope}
                    )
                ]
            finally:
                await backend.aclose()

    asyncio.run(scenario())
