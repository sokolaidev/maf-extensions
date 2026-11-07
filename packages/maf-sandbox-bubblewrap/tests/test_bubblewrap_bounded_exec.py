"""Host-side command budgets remain enforced when the broker returns invalid output."""

import asyncio
import base64
import json
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest
from maf_sandbox import SandboxSpec
from maf_sandbox.bounded_exec import BoundedExec, SandboxExecOutputLimitExceeded

from maf_sandbox_bubblewrap._backend import _Sandbox


@pytest.mark.parametrize("case", ["valid", "combined", "frame", "broker", "configured"])
def test_host_and_guest_output_budgets(tmp_path: Path, case: str) -> None:
    async def run() -> None:
        process = Mock(returncode=None)
        process.stdin.drain = AsyncMock()
        process.stdout = asyncio.StreamReader()
        body = {
            "stdout": base64.b64encode(b"abc").decode(),
            "stderr": base64.b64encode(b"de").decode(),
            "exit_code": 7,
        }
        reply = {"id": 1, "result": body}
        if case == "frame":
            data = b"x" * 8192
        elif case == "broker":
            data = json.dumps({"id": 1, "error": "OutputLimitExceeded"}).encode() + b"\n"
        else:
            data = json.dumps(reply).encode() + b"\n"
        process.stdout.feed_data(data)
        diagnostics = asyncio.create_task(asyncio.sleep(0, result=""))
        sandbox = _Sandbox(
            Mock(config=Mock(max_timeout=5, output_bytes=3 if case == "configured" else 1024)),
            "instance",
            process,
            -1,
            tmp_path,
            SandboxSpec(kind="bounded"),
            diagnostics,
        )
        sandbox.stop = AsyncMock()
        assert isinstance(sandbox, BoundedExec)
        try:
            if case == "valid":
                result = await sandbox.exec_bounded(
                    "true", working_directory=".", timeout=1, max_output_bytes=5
                )
                assert (result.stdout, result.stderr, result.exit_code) == ("abc", "de", 7)
                sandbox.stop.assert_not_called()
            else:
                with pytest.raises(SandboxExecOutputLimitExceeded):
                    await sandbox.exec_bounded(
                        "true", working_directory=".", timeout=1, max_output_bytes=4
                    )
                if case != "broker":
                    sandbox.stop.assert_awaited_once()
            request = json.loads(process.stdin.write.call_args.args[0])
            assert request["output_limit"] == (
                5 if case == "valid" else 3 if case == "configured" else 4
            )
        finally:
            await diagnostics

    asyncio.run(run())


@pytest.mark.parametrize("budget", [0, -1, True, 1.5])
def test_invalid_budget_does_not_dispatch(tmp_path: Path, budget: int) -> None:
    async def run() -> None:
        diagnostics = asyncio.create_task(asyncio.sleep(0, result=""))
        sandbox = _Sandbox(
            Mock(), "instance", Mock(), -1, tmp_path, SandboxSpec(kind="bounded"), diagnostics
        )
        sandbox._request = AsyncMock()
        with pytest.raises(ValueError, match="max_output_bytes"):
            await sandbox.exec_bounded(
                "true", working_directory=".", timeout=1, max_output_bytes=budget
            )
        sandbox._request.assert_not_called()
        await diagnostics

    asyncio.run(run())
