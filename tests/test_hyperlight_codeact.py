"""Cross-package panic sanitization through Hyperlight, the router, and CodeAct."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest
from maf_sandbox import CallerContext, Cleanup, ListedFile, SandboxKey, SandboxRouter, Selection
from maf_sandbox_codeact import CodeactOutputs, CodeactRuntime, make_codeact_tools
from maf_sandbox_hyperlight import (
    FILE_RUNTIME_INSTRUCTIONS,
    RUNTIME_INSTRUCTIONS,
    HyperlightSandboxBackend,
    HyperlightSandboxConfig,
    _backend,
)
from maf_sandbox_tui import MonitoredSandboxBackend, MonitoredSandboxRouter


def _said(answer) -> str:
    """One call's text, whichever parts the result contract rendered it into."""
    if isinstance(answer, str):
        return answer
    return chr(10).join(str(item.text) for item in answer)


@pytest.mark.parametrize("cancel", [False, True])
@pytest.mark.parametrize("monitored", [False, True])
def test_failed_output_delivery_cleans_before_reuse(monkeypatch, cancel, monitored):
    from maf_sandbox import LandedArtifact, OutputSink
    from maf_sandbox_codeact import CodeactOutputs

    directories = []

    class FileWorker:
        def __init__(self, config):
            self.alive = True

        def request(self, message, *, deadline):
            if message["op"] == "init":
                self.directory = Path(message["output_dir"])
                directories.append(self.directory)
            elif message["op"] == "run":
                (self.directory / "result.bin").write_bytes(b"\x00\xff")
                return {"stdout": "", "stderr": "", "exit_code": 0}
            return {"ok": True}

        def close(self):
            self.alive = False

    monkeypatch.setattr(_backend, "Worker", FileWorker)
    monkeypatch.setattr(_backend, "check_host", lambda: None)
    backend = HyperlightSandboxBackend(HyperlightSandboxConfig(file_outputs=True))
    router = (
        MonitoredSandboxRouter([MonitoredSandboxBackend(backend)], min_cleanup=Cleanup.RESET)
        if monitored
        else SandboxRouter([backend], min_cleanup=Cleanup.RESET)
    )

    async def check():
        delivering = asyncio.Event()
        fail = True

        async def deliver(artifact):
            if fail:
                delivering.set()
                if cancel:
                    await asyncio.Future()
                raise OSError("sink unavailable")
            return LandedArtifact(artifact.name, "saved")

        async def no_files(store):
            return []

        context = CallerContext(
            current_scope=lambda: "delivery",
            current_thread_id=lambda: "thread",
            list_files=no_files,
        )
        tool = make_codeact_tools(
            router,
            "agent",
            context,
            runtime=CodeactRuntime(FILE_RUNTIME_INSTRUCTIONS, "/output", use_call_directory=False),
            outputs=CodeactOutputs.DECLARED,
            output_sink=OutputSink(deliver),
        )[0]
        function = getattr(tool, "func", None) or getattr(tool, "__wrapped__", None) or tool
        try:
            task = asyncio.create_task(function(code="write", outputs=["result.bin"]))
            await asyncio.wait_for(delivering.wait(), 2)
            if cancel:
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                assert "could not be saved" in _said(await task)
            assert all(
                not directory.exists() or not list(directory.iterdir()) for directory in directories
            )
            fail = False
            assert "saved" in _said(await function(code="write", outputs=["result.bin"]))
        finally:
            await backend.aclose()
        assert all(not directory.exists() for directory in directories)

    asyncio.run(check())


@pytest.mark.parametrize("selection", list(Selection))
def test_native_panics_are_sanitized_for_codeact(
    monkeypatch: pytest.MonkeyPatch, selection: Selection
):
    workers: list[PanicWorker] = []

    class PanicWorker:
        def __init__(self, config: HyperlightSandboxConfig) -> None:
            self.alive = True

        def request(self, message: dict[str, object], *, deadline: float) -> dict[str, object]:
            if message["op"] == "run":
                workers.append(self)
                return {
                    "stdout": "native stdout",
                    "stderr": "native panic details",
                    "exit_code": -1,
                }
            return {"ok": True}

        def close(self) -> None:
            self.alive = False

    monkeypatch.setattr(_backend, "Worker", PanicWorker)
    monkeypatch.setattr(_backend, "check_host", lambda: None)
    backend = HyperlightSandboxBackend()
    router = SandboxRouter([backend], selection=selection, min_cleanup=Cleanup.RESET)
    key = SandboxKey("tenant", "conversation", "agent")

    async def no_files(store: object) -> list[ListedFile]:
        return []

    context = CallerContext(
        current_scope=lambda: key.scope,
        current_thread_id=lambda: key.thread_id,
        list_files=no_files,
    )
    tool = make_codeact_tools(
        router, key.agent_id, context, runtime=CodeactRuntime(RUNTIME_INSTRUCTIONS)
    )[0]
    function = getattr(tool, "func", None) or getattr(tool, "__wrapped__", None) or tool
    try:
        result = asyncio.run(function(code="print('hello')"))
        assert "Error: could not run the program in the sandbox" in _said(result)
        assert workers and all(not worker.alive for worker in workers)
    finally:
        asyncio.run(backend.aclose())
    assert not backend._sandboxes


@pytest.mark.skipif(os.environ.get("MAF_HYPERLIGHT_LIVE") != "1", reason="requires live Hyperlight")
@pytest.mark.parametrize("selection", list(Selection))
@pytest.mark.parametrize("monitored", [False, True])
@pytest.mark.parametrize("mode", [CodeactOutputs.DECLARED, CodeactOutputs.MANIFEST])
def test_live_codeact_delivers_flat_binary_outputs_and_cleans(selection, monitored, mode):
    from maf_sandbox import LandedArtifact, OutputSink, TransferLimits

    backend = HyperlightSandboxBackend(
        HyperlightSandboxConfig(
            file_outputs=True, linux_cgroup_root=os.environ.get("MAF_HYPERLIGHT_CGROUP_ROOT")
        )
    )
    router = (
        MonitoredSandboxRouter(
            [MonitoredSandboxBackend(backend)], selection=selection, min_cleanup=Cleanup.RESET
        )
        if monitored
        else SandboxRouter([backend], selection=selection, min_cleanup=Cleanup.RESET)
    )
    landed = []

    async def deliver(artifact):
        landed.append(artifact)
        return LandedArtifact(artifact.name, "saved " + artifact.name)

    async def no_files(store):
        return []

    context = CallerContext(
        current_scope=lambda: "files-live", current_thread_id=lambda: "thread", list_files=no_files
    )
    manifest_bytes = 128 if mode is CodeactOutputs.MANIFEST else 0
    tool = make_codeact_tools(
        router,
        "agent",
        context,
        runtime=CodeactRuntime(
            FILE_RUNTIME_INSTRUCTIONS, guest_work_dir="/output", use_call_directory=False
        ),
        outputs=mode,
        output_sink=OutputSink(deliver),
        files_out=TransferLimits(
            max_bytes_per_file=256,
            max_total_bytes=256 + manifest_bytes,
            max_files=2 + bool(manifest_bytes),
        ),
    )[0]
    function = getattr(tool, "func", None) or getattr(tool, "__wrapped__", None) or tool

    async def invoke(code, outputs):
        if mode is CodeactOutputs.MANIFEST:
            manifest = json.dumps({"outputs": [{"path": name} for name in outputs]}).encode()
            assert len(manifest) <= manifest_bytes
            # Keep the artifacts' aggregate allowance identical for every manifest shape.
            manifest = manifest.ljust(manifest_bytes, b" ")
            code += f"\nwith open(guest_call_path + '/outputs.json', 'wb') as f:\n    f.write({manifest!r})"
            return await function(code=code)
        return await function(code=code, outputs=outputs)

    async def check():
        try:
            result = await invoke(
                code="with open(guest_call_path + '/result.bin', 'wb') as f:\n    f.write(bytes(range(256)))",
                outputs=["result.bin"],
            )
            assert "saved" in _said(result), result
            assert len(landed) == 1 and landed[0].content == bytes(range(256))
            result = await invoke(code="print('next')", outputs=["result.bin"])
            assert "Not written by the program, so not saved: 'result.bin'" in _said(result), result
            assert len(landed) == 1, result
            result = await invoke(
                code="with open(guest_call_path + '/big.bin', 'wb') as f:\n    f.write(b'x' * 257)",
                outputs=["big.bin"],
            )
            assert "Error" in _said(result) and len(landed) == 1, result
            result = await invoke(
                code="with open(guest_call_path + '/a', 'wb') as f:\n    f.write(b'a' * 129)\nwith open(guest_call_path + '/b', 'wb') as f:\n    f.write(b'b' * 128)",
                outputs=["a", "b"],
            )
            assert "Error" in _said(result) and len(landed) == 1, result
            result = await invoke(code="print('count check')", outputs=["a", "b", "c"])
            assert "Error" in _said(result) and len(landed) == 1, result
        finally:
            await backend.aclose()

    asyncio.run(check())
