"""Experimental MCP workload registration and process-wide supervision."""

from __future__ import annotations

import asyncio
import json
import math
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import anyio
import jsonschema
from mcp import types
from mcp.server.fastmcp import FastMCP


async def drain(task: asyncio.Task[Any]) -> bool:
    """Retain supervision through repeated cancellation until a task settles."""
    current = asyncio.current_task()
    assert current is not None
    interrupted = False
    with anyio.CancelScope(shield=True):
        while not task.done():
            count = current.cancelling()
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                interrupted |= current.cancelling() > count or not task.cancelled()
    return interrupted


def error(message: str) -> types.CallToolResult:
    """Return an authority-free, bounded MCP error."""
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=message)], isError=True
    )


@dataclass(frozen=True)
class CallContext:
    """Identity minted by transport supervision, never supplied by tool arguments."""

    session_id: str
    request_id: int | str
    call_id: str = field(default_factory=lambda: uuid.uuid4().hex)


@dataclass(frozen=True)
class Resource:
    """Application-owned resource; cleanup confirms no call-owned work remains."""

    name: str
    start: Callable[[], Awaitable[None]]
    cleanup: Callable[[], Awaitable[bool]]
    close: Callable[[], Awaitable[None]]
    capabilities: frozenset[str] = frozenset()


@dataclass(frozen=True)
class Binding:
    """One explicitly configured MCP operation and its bounded workload contract."""

    tool: types.Tool
    execute: Callable[[dict[str, Any], CallContext], Awaitable[types.CallToolResult]]
    resources: tuple[str, ...]
    required_capabilities: frozenset[str]
    max_input_bytes: int
    max_output_bytes: int
    deadline_seconds: float
    failure: Callable[[str, dict[str, Any]], types.CallToolResult] | None = None


class WorkloadService(FastMCP[None]):
    """FastMCP host with one global active call across all registered bindings."""

    def __init__(self, bindings: list[Binding], resources: list[Resource]) -> None:
        self.bindings = {b.tool.name: b for b in bindings}
        self.resources = {r.name: r for r in resources}
        if len(self.bindings) != len(bindings) or len(self.resources) != len(resources):
            raise ValueError("Duplicate tool or resource name.")
        if not bindings:
            raise ValueError("At least one workload binding is required.")
        for b in bindings:
            if not b.resources or any(r not in self.resources for r in b.resources):
                raise ValueError("Missing binding resource.")
            available = frozenset().union(*(self.resources[r].capabilities for r in b.resources))
            if not b.required_capabilities <= available:
                raise ValueError("Unsupported binding policy.")
            if (
                not math.isfinite(b.deadline_seconds)
                or b.deadline_seconds <= 0
                or not 0 < b.max_input_bytes <= 2 * 1024 * 1024
                or not 256 <= b.max_output_bytes <= 2 * 1024 * 1024
            ):
                raise ValueError("Invalid binding limits.")
            jsonschema.Draft202012Validator.check_schema(b.tool.inputSchema)
            if b.tool.outputSchema is None:
                raise ValueError("A concrete output schema is required.")
            jsonschema.Draft202012Validator.check_schema(b.tool.outputSchema)
        self.active: tuple[CallContext, asyncio.Task[types.CallToolResult]] | None = None
        self.ready = False
        self.poisoned = False
        self.session_open: Callable[[str], bool] = lambda _: False
        super().__init__("maf-workloads", json_response=True)

    async def list_tools(self) -> list[types.Tool]:
        """Expose only the immutable, operator-registered workload set."""
        return [b.tool.model_copy(deep=True) for b in self.bindings.values()]

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        """Obtain transport identity separately from model-controlled arguments."""
        ctx = self.get_context().request_context
        request = ctx.request
        session_id = request.headers.get("mcp-session-id", "") if request else ""
        request_id = (
            request.scope.get("workload_request_id", ctx.request_id) if request else ctx.request_id
        )
        return await self.invoke(name, arguments, CallContext(session_id, request_id))

    async def cleanup(self, names: tuple[str, ...]) -> bool:
        """Fail global admission closed when any resource cannot confirm cleanup."""
        clean = True
        for name in dict.fromkeys(names):
            try:
                clean = await self.resources[name].cleanup() and clean
            except Exception:
                clean = False
        self.poisoned |= not clean
        return clean

    def _failure(self, binding: Binding, status: str, arguments: dict[str, Any]):
        if binding.failure:
            try:
                result = self._bounded(binding, binding.failure(status, arguments))
                result.isError = True
                return result
            except Exception:
                pass
        return error("Workload did not complete; no successful result is available.")

    @staticmethod
    def _bounded(binding: Binding, result: types.CallToolResult) -> types.CallToolResult:
        json.dumps(result.structuredContent, allow_nan=False)
        if len(result.model_dump_json().encode("utf-8")) > binding.max_output_bytes:
            return error("Workload result exceeds its response budget.")
        if not result.isError:
            assert binding.tool.outputSchema is not None
            jsonschema.Draft202012Validator(binding.tool.outputSchema).validate(
                result.structuredContent
            )
        return result

    async def _execute(self, b: Binding, args: dict[str, Any], ctx: CallContext):
        try:
            answer = self._bounded(b, await b.execute(args, ctx))
        except Exception:
            answer = self._failure(b, "error", args)
        finally:
            cleanup = asyncio.create_task(self.cleanup(b.resources))
            interrupted = await drain(cleanup)
            if not cleanup.result():
                answer = self._failure(b, "cleanup_failed", args)
            if interrupted:
                raise asyncio.CancelledError
        return answer

    async def invoke(self, name: str, arguments: dict[str, Any], ctx: CallContext):
        """Admit one call, retaining its slot through timeout and cancellation settlement."""
        if not self.ready or self.poisoned or not self.session_open(ctx.session_id):
            return error("Service or session is unavailable.")
        if self.active is not None:
            return error("Service is busy; retry after the active call settles.")
        b = self.bindings.get(name)
        if b is None:
            return error("Unknown tool.")
        try:
            if (
                len(json.dumps(arguments, ensure_ascii=False, allow_nan=False).encode("utf-8"))
                > b.max_input_bytes
            ):
                return error("Tool input exceeds its byte budget.")
            jsonschema.Draft202012Validator(b.tool.inputSchema).validate(arguments)
        except Exception:
            return error("Invalid tool arguments.")
        task = asyncio.create_task(self._execute(b, arguments, ctx))
        self.active = (ctx, task)
        try:
            return await asyncio.wait_for(asyncio.shield(task), timeout=b.deadline_seconds)
        except TimeoutError:
            await self._settle(task)
            return self._failure(b, "cleanup_failed" if self.poisoned else "timeout", arguments)
        except asyncio.CancelledError:
            await self._settle(task)
            raise
        finally:
            self.active = None

    @staticmethod
    async def _settle(task: asyncio.Task[Any]) -> None:
        if not task.done() and not task.cancelling():
            task.cancel()
        interrupted = await drain(task)
        if not task.cancelled():
            task.result()
        if interrupted:
            raise asyncio.CancelledError

    async def cancel_session(self, session_id: str) -> None:
        """Cancel only the active call owned by this transport session."""
        if self.active and self.active[0].session_id == session_id:
            await self._settle(self.active[1])

    @asynccontextmanager
    async def lifespan(self) -> AsyncIterator[None]:
        """Recover resources before readiness and drain them before releasing ownership."""
        started: list[Resource] = []
        try:
            for resource in self.resources.values():
                started.append(resource)
                await resource.start()
                if not await self.cleanup((resource.name,)):
                    raise RuntimeError("Startup recovery failed.")
            self.ready = True
            yield
        finally:
            self.ready = False

            async def finish() -> None:
                if self.active:
                    await self._settle(self.active[1])
                clean = await self.cleanup(tuple(r.name for r in started))
                for resource in reversed(started):
                    try:
                        await resource.close()
                    except Exception:
                        clean = False
                self.poisoned |= not clean
                if not clean:
                    raise RuntimeError("Resource shutdown could not confirm cleanup.")

            closing = asyncio.create_task(finish())
            interrupted = await drain(closing)
            closing.result()
            if interrupted:
                raise asyncio.CancelledError
