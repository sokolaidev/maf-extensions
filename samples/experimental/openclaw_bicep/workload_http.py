"""Bounded stateful HTTP transport for the experimental workload service (MCP 1.26.0)."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import math
import re
import secrets
import socket
import time
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from importlib.metadata import version

import uvicorn
from mcp import types
from mcp.server.streamable_http import StreamableHTTPServerTransport
from mcp.server.transport_security import TransportSecuritySettings
from mcp.shared.version import SUPPORTED_PROTOCOL_VERSIONS
from pydantic import ValidationError
from starlette.applications import Starlette
from starlette.responses import Response
from starlette.types import Receive, Scope, Send
from uvicorn.protocols.http.h11_impl import H11Protocol
from workload_service import WorkloadService, drain

BODY_BYTES = 2 * 1024 * 1024
HEADER_BYTES = 16 * 1024
BODY_SECONDS = 10
CONNECTIONS = 32
BODY_READERS = 16
SESSION_LIMIT = 8
IDLE_SECONDS = 15 * 60


def _finite_float(value: str) -> float:
    """Parse JSON numbers without non-finite constants or overflow."""
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError("Non-finite JSON number.")
    return parsed


@dataclass
class Session:
    """A capacity reservation retained until SDK and workload tasks have settled."""

    transport: StreamableHTTPServerTransport
    touched: float = field(default_factory=time.monotonic)
    started: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task[None] | None = None
    retiring: asyncio.Task[None] | None = None
    ids: set[str] = field(default_factory=set)
    initialized: bool = False
    closing: bool = False
    get_active: bool = False
    requests: int = 0


class WorkloadHTTP:
    """Authenticated ASGI endpoint owning the actual bounded SDK session registry."""

    def __init__(self, service: WorkloadService, token: str, port: int) -> None:
        if version("mcp") != "1.26.0":
            raise RuntimeError("This adapter requires mcp==1.26.0.")
        if not re.fullmatch(r"[0-9a-f]{64}", token) or not 1 <= port <= 65535:
            raise ValueError("Expected a 256-bit hex credential and a valid loopback port.")
        self.service = service
        self.authorization = ("Bearer " + token).encode("ascii")
        self.host = f"127.0.0.1:{port}".encode("ascii")
        self.port = port
        self.sessions: dict[str, Session] = {}
        self.readers = 0
        self.idle_seconds = IDLE_SECONDS
        self.body_seconds = BODY_SECONDS
        self.service.session_open = self.session_open
        self.app = Starlette(lifespan=self.lifespan)
        # SDK session diagnostics include identifiers; the service exposes no raw SDK logs.
        logging.getLogger("mcp").setLevel(logging.CRITICAL)

    def session_open(self, session_id: str) -> bool:
        """Reject dispatch queued before a session began retirement."""
        record = self.sessions.get(session_id)
        return record is not None and record.initialized and not record.closing

    @contextlib.asynccontextmanager
    async def lifespan(self, _: Starlette) -> AsyncIterator[None]:
        async with self.service.lifespan():
            sweeper = asyncio.create_task(self._expire())
            try:
                yield
            finally:
                self.service.ready = False

                async def finish() -> None:
                    sweeper.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await sweeper
                    tasks = [self.retire(sid) for sid in list(self.sessions)]
                    for task in tasks:
                        await drain(task)
                        task.result()

                closing = asyncio.create_task(finish())
                interrupted = await drain(closing)
                closing.result()
                if interrupted:
                    raise asyncio.CancelledError

    async def _expire(self) -> None:
        while True:
            await asyncio.sleep(min(1, self.idle_seconds))
            for sid, record in list(self.sessions.items()):
                active = self.service.active
                if (
                    not record.closing
                    and not record.ids
                    and record.requests == int(record.get_active)
                    and not (active and active[0].session_id == sid)
                    and time.monotonic() - record.touched >= self.idle_seconds
                ):
                    self.retire(sid)

    def retire(self, sid: str) -> asyncio.Task[None]:
        """Reserve capacity until active work, SDK processing and HTTP handlers settle."""
        record = self.sessions[sid]
        record.closing = True
        if record.retiring is None:
            record.retiring = asyncio.create_task(self._retire(sid, record))
        return record.retiring

    async def _retire(self, sid: str, record: Session) -> None:
        try:
            await self.service.cancel_session(sid)
            await record.transport.terminate()
            if record.task:
                await drain(record.task)
                record.task.result()
            while record.requests:
                await asyncio.sleep(0.01)
            self.sessions.pop(sid, None)
        except Exception:
            self.service.poisoned = True
            # Failed retirement keeps its capacity reservation and stops new workload admission.

    async def _start(self, sid: str, record: Session) -> None:
        try:
            async with record.transport.connect() as streams:
                record.started.set()
                # FastMCP exposes custom session ownership through this pinned SDK boundary.
                server = self.service._mcp_server
                await server.run(*streams, server.create_initialization_options())
        except Exception:
            self.service.poisoned = True
        finally:
            record.started.set()
            if not record.closing:
                self.retire(sid)

    @contextlib.contextmanager
    def _request(self, record: Session | None) -> Iterator[None]:
        """Retain a session through the complete lifetime of an HTTP handler."""
        if record is not None:
            record.requests += 1
            record.touched = time.monotonic()
        try:
            yield
        finally:
            if record is not None:
                record.requests -= 1

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = scope.get("headers", [])
        grouped: dict[bytes, list[bytes]] = {}
        for name, value in headers:
            grouped.setdefault(name.lower(), []).append(value)
        status = 0
        if sum(len(n) + len(v) + 4 for n, v in headers) + 2 > HEADER_BYTES:
            status = 431
        elif grouped.get(b"authorization") is None or len(grouped[b"authorization"]) != 1:
            status = 401
        elif not secrets.compare_digest(grouped[b"authorization"][0], self.authorization):
            status = 401
        elif grouped.get(b"host") != [self.host] or b"origin" in grouped:
            status = 403
        elif scope["path"] == "/ready":
            status = 200 if self.service.ready and not self.service.poisoned else 503
        elif scope["path"] != "/mcp":
            status = 404
        elif not self.service.ready:
            status = 503
        elif scope["method"] not in {"GET", "POST", "DELETE"}:
            status = 405
        elif any(
            len(grouped.get(h, [])) > 1
            for h in (
                b"mcp-session-id",
                b"mcp-protocol-version",
                b"content-length",
                b"content-type",
            )
        ):
            status = 400
        if status:
            await Response(status_code=status)(scope, receive, send)
            return
        sid = grouped.get(b"mcp-session-id", [b""])[0].decode("ascii", errors="replace")
        if (
            sid
            and grouped.get(b"mcp-protocol-version", [b"2025-03-26"])[0].decode(
                "ascii", errors="replace"
            )
            not in SUPPORTED_PROTOCOL_VERSIONS
        ):
            await Response(status_code=400)(scope, receive, send)
            return
        if sid and (sid not in self.sessions or self.sessions[sid].closing):
            await Response(status_code=404)(scope, receive, send)
            return
        if scope["method"] == "POST":
            with self._request(self.sessions.get(sid)):
                await self._post(scope, receive, send, sid, grouped)
            return
        if not sid:
            await Response(status_code=400)(scope, receive, send)
            return
        record = self.sessions[sid]
        record.touched = time.monotonic()
        if scope["method"] == "DELETE":
            self.retire(sid)
            await Response(status_code=200)(scope, receive, send)
        elif record.get_active:
            await Response(status_code=409)(scope, receive, send)
        else:
            record.get_active = True
            record.requests += 1
            try:
                await record.transport.handle_request(scope, receive, send)
            finally:
                record.requests -= 1
                record.get_active = False

    async def _body(self, receive: Receive) -> bytes:
        data = bytearray()
        async with asyncio.timeout(self.body_seconds):
            while True:
                message = await receive()
                if message["type"] != "http.request":
                    raise ValueError("Incomplete body.")
                data.extend(message.get("body", b""))
                if len(data) > BODY_BYTES:
                    raise OverflowError
                if not message.get("more_body", False):
                    return bytes(data)

    async def _post(self, scope: Scope, receive: Receive, send: Send, sid: str, headers):
        if self.readers >= BODY_READERS:
            await Response(status_code=503)(scope, receive, send)
            return
        self.readers += 1
        try:
            lengths = headers.get(b"content-length", [])
            if lengths and (not lengths[0].isdigit() or int(lengths[0]) > BODY_BYTES):
                raise OverflowError
            body = await self._body(receive)
        except OverflowError:
            await Response(status_code=413)(scope, receive, send)
            return
        except TimeoutError:
            await Response(status_code=408)(scope, receive, send)
            return
        except ValueError:
            await Response(status_code=400)(scope, receive, send)
            return
        finally:
            self.readers -= 1
        if sid and (sid not in self.sessions or self.sessions[sid].closing):
            await Response(status_code=404)(scope, receive, send)
            return
        try:
            raw = json.loads(body, parse_constant=_finite_float, parse_float=_finite_float)
            message = types.JSONRPCMessage.model_validate(raw).root
            if isinstance(message, types.JSONRPCRequest):
                types.ClientRequest.model_validate(raw)
                if isinstance(raw.get("id"), bool):
                    raise ValueError
            elif isinstance(message, types.JSONRPCNotification):
                types.ClientNotification.model_validate(raw)
            else:
                raise ValueError
            accept = b",".join(headers.get(b"accept", []))
            content_type = headers.get(b"content-type", [b""])[0].split(b";")[0]
            if b"application/json" not in accept or b"text/event-stream" not in accept:
                raise ValueError
            if content_type != b"application/json":
                raise ValueError
            if sid:
                if message.method == "initialize":
                    raise ValueError
                if (
                    not self.sessions[sid].initialized
                    and message.method != "notifications/initialized"
                ):
                    raise ValueError
            elif not isinstance(message, types.JSONRPCRequest) or message.method != "initialize":
                raise ValueError
        except (ValueError, TypeError, ValidationError, RecursionError):
            await Response(status_code=400)(scope, receive, send)
            return
        fresh = not sid
        if fresh:
            if len(self.sessions) >= SESSION_LIMIT:
                await Response(status_code=503)(scope, receive, send)
                return
            sid = secrets.token_hex(16)
            record = Session(
                StreamableHTTPServerTransport(
                    mcp_session_id=sid,
                    is_json_response_enabled=True,
                    security_settings=TransportSecuritySettings(
                        enable_dns_rebinding_protection=False
                    ),
                )
            )
            self.sessions[sid] = record
            record.task = asyncio.create_task(self._start(sid, record))
        else:
            record = self.sessions[sid]
        with self._request(record if fresh else None):
            if fresh:
                await record.started.wait()
            if record.closing:
                await Response(status_code=404)(scope, receive, send)
                return
            request_id = str(message.id) if isinstance(message, types.JSONRPCRequest) else None
            if request_id is not None and request_id in record.ids:
                await Response(status_code=409)(scope, receive, send)
                return
            if request_id is not None:
                record.ids.add(request_id)
            consumed = False
            response_status = 500

            async def replay():
                nonlocal consumed
                if not consumed:
                    consumed = True
                    return {"type": "http.request", "body": body, "more_body": False}
                return await receive()

            async def capture(message):
                nonlocal response_status
                if message["type"] == "http.response.start":
                    response_status = message["status"]
                await send(message)

            async def dispatch():
                try:
                    await record.transport.handle_request(scope, replay, capture)
                    if message.method == "notifications/initialized" and response_status == 202:
                        record.initialized = True
                finally:
                    active = self.service.active
                    if (
                        active
                        and active[0].session_id == sid
                        and active[0].request_id == request_id
                    ):
                        await drain(active[1])
                    if request_id is not None:
                        record.ids.discard(request_id)
                    if fresh and response_status != 200:
                        self.retire(sid)

            handling = asyncio.create_task(dispatch())
            interrupted = await drain(handling)
            handling.result()
            if interrupted:
                raise asyncio.CancelledError


class BoundedH11Protocol(H11Protocol):
    """Cap accepted connections and incomplete headers before ASGI dispatch."""

    rejected = False
    header_timer: asyncio.TimerHandle | None = None

    def connection_made(self, transport: asyncio.Transport) -> None:
        if len(self.connections) >= CONNECTIONS:
            self.rejected = True
            transport.close()
            return
        super().connection_made(transport)
        self.header_timer = self.loop.call_later(BODY_SECONDS, self.transport.close)

    def data_received(self, data: bytes) -> None:
        if self.rejected:
            return
        if self.header_timer is None and self.cycle and self.cycle.response_complete:
            self.header_timer = self.loop.call_later(BODY_SECONDS, self.transport.close)
        super().data_received(data)
        if self.cycle and not self.cycle.response_complete and self.header_timer:
            self.header_timer.cancel()
            self.header_timer = None

    def connection_lost(self, exc: Exception | None) -> None:
        if self.header_timer:
            self.header_timer.cancel()
        if not self.rejected:
            super().connection_lost(exc)


class WorkloadServer(uvicorn.Server):
    """Retire MCP streams before Uvicorn waits for HTTP requests to finish."""

    def __init__(self, app: WorkloadHTTP, config: uvicorn.Config) -> None:
        super().__init__(config)
        self.workload_app = app

    async def shutdown(self, sockets: list[socket.socket] | None = None) -> None:
        self.workload_app.service.ready = False
        for listener in self.servers:
            listener.close()
        for sid in list(self.workload_app.sessions):
            closing = self.workload_app.retire(sid)
            await drain(closing)
            closing.result()
        await super().shutdown(sockets)


def server(app: WorkloadHTTP) -> uvicorn.Server:
    """Construct the single-worker loopback server with the qualified framing layer."""
    if version("uvicorn") != "0.54.0":
        raise RuntimeError("The bounded HTTP protocol requires uvicorn==0.54.0.")
    return WorkloadServer(
        app,
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=app.port,
            http=BoundedH11Protocol,
            ws="none",
            access_log=False,
            log_level="warning",
            proxy_headers=False,
            h11_max_incomplete_event_size=HEADER_BYTES,
            timeout_keep_alive=5,
        ),
    )
