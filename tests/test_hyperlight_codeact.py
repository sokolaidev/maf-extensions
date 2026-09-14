"""Cross-package panic sanitization through Hyperlight, the router, and CodeAct."""

from __future__ import annotations

import asyncio

import pytest
from maf_sandbox import CallerContext, Cleanup, ListedFile, SandboxKey, SandboxRouter, Selection
from maf_sandbox_codeact import CodeactRuntime, make_codeact_tools
from maf_sandbox_hyperlight import (
    RUNTIME_INSTRUCTIONS,
    HyperlightSandboxBackend,
    HyperlightSandboxConfig,
    _backend,
)


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
        assert result == "Error: could not run the program in the sandbox"
        assert workers and all(not worker.alive for worker in workers)
    finally:
        asyncio.run(backend.aclose())
    assert not backend._sandboxes
