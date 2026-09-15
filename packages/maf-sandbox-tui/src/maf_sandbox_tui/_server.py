"""Opt-in loopback control endpoint hosted by the owning MAF process."""

from __future__ import annotations

import asyncio
import getpass
import hashlib
import ipaddress
import json
import math
import os
import secrets
import socket
import stat
import tempfile
import threading
from collections.abc import Coroutine
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeoutError
from contextlib import suppress
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, cast
from urllib.parse import parse_qs, unquote, urlsplit

from ._control import SandboxControl
from ._models import DisposalResult, DisposalStatus, validate_source_id

PROTOCOL_VERSION = 1
_CONTROL_FAILURE_STATUS = HTTPStatus(500)
_OPERATION_SETTLEMENT_GRACE = 0.1
_SOCKET_IO_TIMEOUT = 0.5


def _valid_process_id(value: object) -> bool:
    return value is None or (isinstance(value, int) and not isinstance(value, bool))


def _valid_protocol_version(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value == PROTOCOL_VERSION


def _windows_runtime_directory() -> Path:
    local = os.environ.get("LOCALAPPDATA")
    if local:
        return Path(local) / "sokolai" / "maf-sandbox-tui"
    identity = hashlib.sha256(getpass.getuser().encode("utf-8")).hexdigest()[:16]
    return Path(tempfile.gettempdir()) / f"maf-sandbox-tui-{identity}"


def runtime_directory() -> Path:
    """Return the per-user directory used for local endpoint discovery."""
    configured = os.environ.get("MAF_SANDBOX_TUI_RUNTIME_DIR")
    if configured:
        return Path(configured)
    if os.name == "nt":
        return _windows_runtime_directory()
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime:
        return Path(runtime) / "maf-sandbox-tui"
    user_id = os.getuid()
    return Path(tempfile.gettempdir()) / f"maf-sandbox-tui-{user_id}"


def _process_user_id() -> int | None:
    if os.name == "nt":
        return None
    return os.getuid()


def ensure_private_runtime_directory(path: Path, *, create: bool) -> bool:
    """Create or validate a discovery directory before trusting its contents."""
    if create:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            path.mkdir(mode=0o700)
        except FileExistsError:
            # A concurrent creator is trusted only if the checks below accept it.
            pass
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return False
    if not stat.S_ISDIR(metadata.st_mode):
        raise RuntimeError(f"sandbox discovery path is not a directory: {path}")
    user_id = _process_user_id()
    if user_id is not None:
        if metadata.st_uid != user_id:
            raise PermissionError(f"sandbox discovery directory is not owned by this user: {path}")
        if stat.S_IMODE(metadata.st_mode) & 0o077:
            raise PermissionError(f"sandbox discovery directory is not private: {path}")
    return True


@dataclass(frozen=True)
class EndpointManifest:
    """Discovery information for one owning MAF process."""

    source_id: str
    endpoint: str
    process_id: int | None
    protocol_version: int = PROTOCOL_VERSION

    def __post_init__(self) -> None:
        validate_source_id(self.source_id)
        if not _valid_protocol_version(self.protocol_version):
            raise ValueError(f"unsupported control protocol version {self.protocol_version!r}")
        if not _valid_process_id(self.process_id):
            raise ValueError("endpoint manifest process_id must be an integer or null")
        parts = urlsplit(self.endpoint)
        try:
            host, port = parts.hostname, parts.port
            loopback = host is not None and ipaddress.ip_address(host).is_loopback
        except ValueError as error:
            raise ValueError("control endpoint must be an HTTP loopback URL with a port") from error
        if (
            parts.scheme != "http"
            or not loopback
            or port is None
            or parts.username is not None
            or parts.password is not None
            or parts.path not in {"", "/"}
            or parts.query
            or parts.fragment
        ):
            raise ValueError("control endpoint must be an HTTP loopback URL with a port")

    def to_json(self) -> dict[str, object]:
        """Return the discovery file representation."""
        return {
            "source_id": self.source_id,
            "endpoint": self.endpoint,
            "process_id": self.process_id,
            "protocol_version": self.protocol_version,
        }

    @classmethod
    def from_json(cls, value: object) -> EndpointManifest:
        """Read and validate a discovery file."""
        if not isinstance(value, dict):
            raise ValueError("endpoint manifest must be an object")
        data = cast("dict[object, object]", value)
        source_id, endpoint = data.get("source_id"), data.get("endpoint")
        process_id, version = data.get("process_id"), data.get("protocol_version")
        if not all(isinstance(item, str) and item for item in (source_id, endpoint)):
            raise ValueError("endpoint manifest identity fields must be nonempty strings")
        if not _valid_process_id(process_id):
            raise ValueError("endpoint manifest process_id must be an integer or null")
        if not _valid_protocol_version(version):
            raise ValueError(f"unsupported control protocol version {version!r}")
        return cls(
            cast("str", source_id),
            cast("str", endpoint),
            cast("int | None", process_id),
        )


class _ControlHttpServer(ThreadingHTTPServer):
    daemon_threads = False

    def __init__(self, owner: SandboxControlServer) -> None:
        self.owner = owner
        self._clients: set[socket.socket] = set()
        self._busy_clients: set[socket.socket] = set()
        self._clients_lock = threading.Lock()
        super().__init__(("127.0.0.1", 0), _ControlHandler)

    def get_request(self) -> tuple[socket.socket, tuple[str, int]]:
        request, address = super().get_request()
        request.settimeout(_SOCKET_IO_TIMEOUT)
        with self._clients_lock:
            self._clients.add(request)
        return request, address

    def shutdown_request(self, request: Any) -> None:
        with self._clients_lock:
            self._clients.discard(request)
            self._busy_clients.discard(request)
        super().shutdown_request(request)

    def mark_busy(self, request: socket.socket) -> None:
        with self._clients_lock:
            self._busy_clients.add(request)

    def close_clients(self) -> None:
        with self._clients_lock:
            clients = tuple(self._clients - self._busy_clients)
        for request in clients:
            with suppress(OSError):
                request.shutdown(socket.SHUT_RDWR)
            request.close()


async def _dispose_if_unique(
    control: SandboxControl, instance_id: str, *, timeout: float
) -> DisposalResult:
    records = await control.list_sandboxes()
    if sum(item.instance_id == instance_id for item in records) > 1:
        return DisposalResult(
            DisposalStatus.FAILED,
            instance_id,
            "Duplicate physical instance id reported; exact disposal refused.",
        )
    return await control.dispose_sandbox(instance_id, timeout=timeout)


class _ControlHandler(BaseHTTPRequestHandler):
    @property
    def _control_server(self) -> _ControlHttpServer:
        return cast("_ControlHttpServer", self.server)

    def _send(self, status: HTTPStatus, value: object) -> None:
        body = json.dumps(value, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-MAF-Sandbox-Control-Version", str(PROTOCOL_VERSION))
        self.end_headers()
        self.wfile.write(body)

    def _run(self, operation: Coroutine[Any, Any, Any], *, timeout: float | None = None) -> Any:
        server = self._control_server
        server.mark_busy(self.request)
        owner = server.owner
        future = owner.schedule_operation(operation)
        handler_timeout = (
            owner.request_timeout
            if timeout is None
            else min(timeout + _OPERATION_SETTLEMENT_GRACE, owner.request_timeout)
        )
        try:
            return future.result(timeout=handler_timeout)
        except FutureTimeoutError:
            owner.cancel_operation(future)
            raise

    def _operation_timeout(self) -> float:
        values = parse_qs(urlsplit(self.path).query, keep_blank_values=True).get("timeout", [])
        if not values:
            return self._control_server.owner.dispose_timeout
        if len(values) != 1:
            raise ValueError("timeout must be specified once")
        timeout = float(values[0])
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be a finite positive number of seconds")
        return min(timeout, self._control_server.owner.dispose_timeout)

    def do_GET(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path
        try:
            timeout = self._operation_timeout()
        except ValueError as error:
            self._send(HTTPStatus.BAD_REQUEST, {"error": str(error)})
            return
        try:
            if path == "/v1/health":
                self._send(
                    HTTPStatus.OK,
                    {
                        "protocol_version": PROTOCOL_VERSION,
                        "source_id": self._control_server.owner.source_id,
                    },
                )
                return
            if path == "/v1/sandboxes":
                records = self._run(
                    self._control_server.owner.control.list_sandboxes(),
                    timeout=timeout,
                )
                self._send(
                    HTTPStatus.OK,
                    {
                        "protocol_version": PROTOCOL_VERSION,
                        "sandboxes": [record.to_json() for record in records],
                    },
                )
                return
            if path.startswith("/v1/sandboxes/"):
                instance_id = unquote(path.removeprefix("/v1/sandboxes/"))
                records = self._run(
                    self._control_server.owner.control.list_sandboxes(),
                    timeout=timeout,
                )
                matches = tuple(item for item in records if item.instance_id == instance_id)
                if len(matches) > 1:
                    self._send(
                        HTTPStatus.CONFLICT,
                        {"error": "duplicate physical instance id reported by this host"},
                    )
                elif not matches:
                    self._send(HTTPStatus.NOT_FOUND, {"error": "sandbox not found"})
                else:
                    self._send(HTTPStatus.OK, matches[0].to_json())
                return
            self._send(HTTPStatus.NOT_FOUND, {"error": "route not found"})
        except FutureTimeoutError:
            self._send(HTTPStatus.GATEWAY_TIMEOUT, {"error": "control request timed out"})
        except Exception:
            self._send(_CONTROL_FAILURE_STATUS, {"error": "control request failed"})

    def do_DELETE(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path
        try:
            timeout = self._operation_timeout()
        except ValueError as error:
            self._send(HTTPStatus.BAD_REQUEST, {"error": str(error)})
            return
        parts = path.split("/")
        if len(parts) == 6 and parts[1:3] == ["v1", "scopes"] and parts[4] == "threads":
            scope, thread_id = unquote(parts[3]), unquote(parts[5])
            if not scope or not thread_id:
                self._send(HTTPStatus.BAD_REQUEST, {"error": "scope and thread id are required"})
                return
            try:
                result = self._run(
                    self._control_server.owner.control.purge_thread(
                        scope,
                        thread_id,
                        timeout=timeout,
                    ),
                    timeout=timeout,
                )
                self._send(HTTPStatus.OK, result.to_json())
            except FutureTimeoutError:
                self._send(HTTPStatus.GATEWAY_TIMEOUT, {"error": "purge timed out"})
            except Exception:
                self._send(_CONTROL_FAILURE_STATUS, {"error": "purge failed"})
            return
        if not path.startswith("/v1/sandboxes/"):
            self._send(HTTPStatus.NOT_FOUND, {"error": "route not found"})
            return
        instance_id = unquote(path.removeprefix("/v1/sandboxes/"))
        if not instance_id:
            self._send(HTTPStatus.BAD_REQUEST, {"error": "instance id is required"})
            return
        try:
            result = self._run(
                _dispose_if_unique(
                    self._control_server.owner.control,
                    instance_id,
                    timeout=timeout,
                ),
                timeout=timeout,
            )
            status = HTTPStatus.OK if result.status.value != "failed" else HTTPStatus.CONFLICT
            self._send(status, result.to_json())
        except FutureTimeoutError:
            self._send(HTTPStatus.GATEWAY_TIMEOUT, {"error": "disposal timed out"})
        except Exception:
            self._send(_CONTROL_FAILURE_STATUS, {"error": "disposal failed"})

    def log_message(self, format: str, *args: object) -> None:
        del format, args


class SandboxControlServer:
    """Serve one MAF process's explicitly enabled local control surface.

    Construction is inert. A host opens the loopback listener only by awaiting :meth:`start` or
    entering the async context after its own opt-in configuration enables local control.
    """

    def __init__(
        self,
        control: SandboxControl,
        *,
        source_id: str,
        manifest_directory: Path | None = None,
        dispose_timeout: float = 10.0,
    ) -> None:
        if not math.isfinite(dispose_timeout) or dispose_timeout <= 0:
            raise ValueError("dispose_timeout must be finite and positive")
        self.control = control
        self.source_id = validate_source_id(source_id)
        self.dispose_timeout = dispose_timeout
        self.request_timeout = dispose_timeout + 5.0
        self._manifest_directory = manifest_directory or runtime_directory()
        self._manifest_path: Path | None = None
        self._httpd: _ControlHttpServer | None = None
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._operation_lock = threading.Lock()
        self._operations: dict[
            Future[Any],
            tuple[Coroutine[Any, Any, Any], asyncio.Task[Any] | None, threading.Event],
        ] = {}
        self._closing = False
        self._close_task: asyncio.Task[None] | None = None

    @property
    def loop(self) -> asyncio.AbstractEventLoop:
        """Event loop that owns the backend and router."""
        if self._loop is None:
            raise RuntimeError("control server is not started")
        return self._loop

    @property
    def endpoint(self) -> str:
        """Loopback URL of the started endpoint."""
        if self._httpd is None:
            raise RuntimeError("control server is not started")
        host, port = self._httpd.server_address[:2]
        return f"http://{host}:{port}"

    @property
    def manifest(self) -> EndpointManifest:
        """Discovery record for the started endpoint."""
        return EndpointManifest(self.source_id, self.endpoint, os.getpid())

    def schedule_operation(self, operation: Coroutine[Any, Any, Any]) -> Future[Any]:
        """Schedule one handler operation on the owning event loop."""
        bridge: Future[Any] = Future()
        with self._operation_lock:
            loop = self._loop
            if self._closing or loop is None:
                operation.close()
                raise RuntimeError("control server is closing")
            self._operations[bridge] = (operation, None, threading.Event())
            try:
                loop.call_soon_threadsafe(self._begin_operation, bridge)
            except RuntimeError:
                self._operations.pop(bridge, None)
                operation.close()
                raise
        return bridge

    def _begin_operation(self, bridge: Future[Any]) -> None:
        with self._operation_lock:
            entry = self._operations.get(bridge)
            if entry is None:
                return
            operation, _, completed = entry
            if self._closing:
                self._operations.pop(bridge, None)
                task = None
            else:
                task = self.loop.create_task(operation)
                self._operations[bridge] = (operation, task, completed)
        if task is None:
            operation.close()
            bridge.cancel()
            completed.set()
            return
        task.add_done_callback(lambda completed: self._finish_operation(bridge, completed))
        if bridge.cancelled():
            task.cancel()

    def _finish_operation(self, bridge: Future[Any], task: asyncio.Task[Any]) -> None:
        with self._operation_lock:
            entry = self._operations.pop(bridge, None)
        try:
            result = task.result()
        except asyncio.CancelledError:
            if entry is not None and not bridge.done():
                bridge.cancel()
        except BaseException as error:
            if entry is not None and not bridge.done():
                bridge.set_exception(error)
        else:
            if entry is not None and not bridge.done():
                bridge.set_result(result)
        finally:
            if entry is not None:
                entry[2].set()

    def cancel_operation(self, bridge: Future[Any]) -> None:
        """Cancel an operation whose handler-side deadline expired."""
        bridge.cancel()
        with self._operation_lock:
            entry = self._operations.get(bridge)
        loop = self._loop
        if entry is None or loop is None:
            return
        try:
            loop.call_soon_threadsafe(self._cancel_operation, bridge)
        except RuntimeError:
            return
        entry[2].wait(_OPERATION_SETTLEMENT_GRACE)

    def _cancel_operation(self, bridge: Future[Any]) -> None:
        with self._operation_lock:
            entry = self._operations.get(bridge)
            if entry is None:
                return
            operation, task, completed = entry
            if task is None:
                self._operations.pop(bridge, None)
        if task is None:
            operation.close()
            completed.set()
        else:
            task.cancel()

    async def _cancel_active_operations(self) -> None:
        with self._operation_lock:
            entries = tuple(self._operations.items())
            pending = tuple(
                (bridge, operation, completed)
                for bridge, (operation, task, completed) in entries
                if task is None
            )
            for bridge, _, _ in pending:
                self._operations.pop(bridge, None)
        for bridge, operation, completed in pending:
            operation.close()
            bridge.cancel()
            completed.set()
        active = tuple(
            (bridge, task, completed)
            for bridge, (_, task, completed) in entries
            if task is not None
        )
        for bridge, task, _ in active:
            bridge.cancel()
            task.cancel()
        if active:
            await asyncio.gather(*(task for _, task, _ in active), return_exceptions=True)
        with self._operation_lock:
            for bridge, _, completed in active:
                self._operations.pop(bridge, None)
                completed.set()

    async def start(self) -> SandboxControlServer:
        """Start serving and publish an atomic per-user discovery record."""
        if self._httpd is not None:
            return self
        self._loop = asyncio.get_running_loop()
        with self._operation_lock:
            self._closing = False
        httpd = _ControlHttpServer(self)
        self._httpd = httpd
        self._thread = threading.Thread(
            target=httpd.serve_forever,
            name=f"maf-sandbox-control-{self.source_id}",
            daemon=True,
        )
        self._thread.start()
        temporary: Path | None = None
        try:
            ensure_private_runtime_directory(self._manifest_directory, create=True)
            identity = secrets.token_hex(6)
            manifest_path = self._manifest_directory / f"{os.getpid()}-{identity}.json"
            temporary = manifest_path.with_suffix(".tmp")
            temporary.write_text(json.dumps(self.manifest.to_json()), encoding="utf-8")
            temporary.replace(manifest_path)
            self._manifest_path = manifest_path
        except BaseException as startup_error:
            try:
                await self.close()
            except BaseException as teardown_error:
                raise startup_error from teardown_error
            raise
        finally:
            if temporary is not None:
                with suppress(OSError):
                    temporary.unlink(missing_ok=True)
        return self

    async def _close_once(self) -> None:
        """Run every teardown step before reporting any recoverable failure."""
        path, httpd, thread = self._manifest_path, self._httpd, self._thread
        errors: list[Exception] = []
        path_removed = path is None
        if path is not None:
            try:
                path.unlink(missing_ok=True)
                path_removed = True
            except Exception as error:  # noqa: BLE001
                errors.append(error)
        if httpd is not None:
            try:
                await asyncio.to_thread(httpd.shutdown)
            except Exception as error:  # noqa: BLE001
                errors.append(error)
        try:
            await self._cancel_active_operations()
        except Exception as error:  # noqa: BLE001
            errors.append(error)
        if httpd is not None:
            try:
                await asyncio.to_thread(httpd.close_clients)
            except Exception as error:  # noqa: BLE001
                errors.append(error)
        httpd_closed = httpd is None
        if httpd is not None:
            try:
                await asyncio.to_thread(httpd.server_close)
                httpd_closed = True
            except Exception as error:  # noqa: BLE001
                errors.append(error)
        if thread is not None:
            try:
                await asyncio.to_thread(thread.join, 2.0)
            except Exception as error:  # noqa: BLE001
                errors.append(error)
        thread_stopped = thread is None or not thread.is_alive()
        if path_removed:
            self._manifest_path = None
        if httpd_closed and thread_stopped:
            self._httpd = None
        if thread_stopped:
            self._thread = None
        if not self._operations and self._httpd is None and self._thread is None:
            self._loop = None
        if len(errors) == 1:
            raise errors[0]
        if errors:
            raise ExceptionGroup("control server teardown failed", errors)

    async def close(self) -> None:
        """Withdraw discovery, stop accepting requests and drain handler operations."""
        with self._operation_lock:
            self._closing = True
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._close_once())
        task = self._close_task
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    continue
            if not task.cancelled():
                task.exception()
            raise
        finally:
            if task.done() and self._close_task is task:
                self._close_task = None

    async def __aenter__(self) -> SandboxControlServer:
        return await self.start()

    async def __aexit__(self, *exc: object) -> None:
        await self.close()
