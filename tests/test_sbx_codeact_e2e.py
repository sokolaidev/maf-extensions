"""A real CodeAct workload calling a host tool through the sbx backend and router."""

from __future__ import annotations

import asyncio
import os
from uuid import uuid4

import pytest
from maf_sandbox import (
    CallerContext,
    HostToolRegistry,
    Identity,
    SandboxRouter,
    SourceIntegrity,
    TransferLimits,
    sandbox_tool,
)
from maf_sandbox_codeact import make_codeact_tools
from maf_sandbox_docker_sbx import SbxSandboxBackend, SbxSandboxConfig

pytestmark = pytest.mark.skipif(
    os.environ.get("MAF_SANDBOX_SBX_E2E") != "1", reason="requires a confined real sbx host"
)


def test_codeact_receives_a_real_host_effect(tmp_path):
    async def scenario():
        scope = "sbx-codeact-" + uuid4().hex
        backend = SbxSandboxBackend(
            SbxSandboxConfig(
                sbx_path=os.environ.get("MAF_SANDBOX_SBX_PATH", "sbx"),
                workspace_root=tmp_path,
            )
        )
        registry = HostToolRegistry(
            max_host_tool_calls_per_run=1,
            response_limits=TransferLimits(
                max_bytes_per_file=1024, max_total_bytes=1024, max_files=1
            ),
        )
        effects = []

        @sandbox_tool(source=SourceIntegrity.TRUSTED, sink=None, identity=Identity.APP)
        def remember(value: str) -> str:
            effects.append(value)
            return "recorded:" + value

        registry.register(remember)
        router = SandboxRouter([backend], min_isolation=backend.isolation)
        context = CallerContext(
            current_scope=lambda: scope,
            current_thread_id=lambda: "thread",
            list_files=lambda: (),
        )
        limits = TransferLimits(
            max_bytes_per_file=1024 * 1024, max_total_bytes=4 * 1024 * 1024, max_files=8
        )
        tool = make_codeact_tools(
            router, "agent", context, host_tools=registry, files_in=limits, files_out=limits
        )[0]
        body = getattr(tool, "func", None) or getattr(tool, "__wrapped__", None) or tool
        try:
            result = await body(
                code="import maf_host_tools\nprint(maf_host_tools.call('remember', value='once'))"
            )
            text = (
                result if isinstance(result, str) else "\n".join(str(item.text) for item in result)
            )
            assert "recorded:once" in text and "Result: ok" in text, text
            assert effects == ["once"]
        finally:
            purged = await backend.dispose_scope(scope, "thread")
            assert not purged.undisposed, purged

    asyncio.run(scenario())
