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
import tempfile
import threading
from collections.abc import Coroutine
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, cast
from urllib.parse import parse_qs, unquote, urlsplit

from ._control import SandboxControl

PROTOCOL_VERSION = 1
_CONTROL_FAILURE_STATUS = HTTPStatus(500)


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


@dataclass(frozen=True)
class EndpointManifest:
    """Discovery information for one owning MAF process."""

    source_id: str
    endpoint: str
    process_id: int
    protocol_version: int = PROTOCOL_VERSION

    def __post_init__(self) -> None:
        if not self.source_id:
            raise ValueError("endpoint manifest source_id must be a nonempty string")
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
        if isinstance(process_id, bool) or not isinstance(process_id, int):
            raise ValueError("endpoint manifest process_id must be an integer")
        if isinstance(version, bool) or not isinstance(version, int) or version != PROTOCOL_VERSION:
            raise ValueError(f"unsupported control protocol version {version!r}")
        return cls(
            cast("str", source_id),
            cast("str", endpoint),
            process_id,
        )


class _ControlHttpServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, owner: SandboxControlServer) -> None:
        self.owner = owner
        super().__init__(("127.0.0.1", 0), _ControlHandler)


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
        future = asyncio.run_coroutine_threadsafe(operation, self._control_server.owner.loop)
        try:
            return future.result(
                timeout=(
                    self._control_server.owner.request_timeout
                    if timeout is None
                    else min(timeout, self._control_server.owner.request_timeout)
                )
            )
        except FutureTimeoutError:
            future.cancel()
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
                    timeout=self._operation_timeout(),
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
                    timeout=self._operation_timeout(),
                )
                record = next((item for item in records if item.instance_id == instance_id), None)
                if record is None:
                    self._send(HTTPStatus.NOT_FOUND, {"error": "sandbox not found"})
                else:
                    self._send(HTTPStatus.OK, record.to_json())
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
                    )
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
                self._control_server.owner.control.dispose_sandbox(
                    instance_id,
                    timeout=timeout,
                )
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
        if not source_id:
            raise ValueError("source_id must not be empty")
        if not math.isfinite(dispose_timeout) or dispose_timeout <= 0:
            raise ValueError("dispose_timeout must be finite and positive")
        self.control = control
        self.source_id = source_id
        self.dispose_timeout = dispose_timeout
        self.request_timeout = dispose_timeout + 5.0
        self._manifest_directory = manifest_directory or runtime_directory()
        self._manifest_path: Path | None = None
        self._httpd: _ControlHttpServer | None = None
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

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

    async def start(self) -> SandboxControlServer:
        """Start serving and publish an atomic per-user discovery record."""
        if self._httpd is not None:
            return self
        self._loop = asyncio.get_running_loop()
        httpd = _ControlHttpServer(self)
        self._httpd = httpd
        self._thread = threading.Thread(
            target=httpd.serve_forever,
            name=f"maf-sandbox-control-{self.source_id}",
            daemon=True,
        )
        self._thread.start()
        try:
            self._manifest_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            identity = secrets.token_hex(6)
            manifest_path = self._manifest_directory / f"{os.getpid()}-{identity}.json"
            temporary = manifest_path.with_suffix(".tmp")
            temporary.write_text(json.dumps(self.manifest.to_json()), encoding="utf-8")
            temporary.replace(manifest_path)
            self._manifest_path = manifest_path
        except BaseException:
            await self.close()
            raise
        return self

    async def close(self) -> None:
        """Withdraw discovery and stop accepting control requests."""
        path, httpd, thread = self._manifest_path, self._httpd, self._thread
        self._manifest_path = None
        self._httpd = None
        self._thread = None
        if path is not None:
            path.unlink(missing_ok=True)
        if httpd is not None:
            await asyncio.to_thread(httpd.shutdown)
            httpd.server_close()
        if thread is not None:
            await asyncio.to_thread(thread.join, 2.0)

    async def __aenter__(self) -> SandboxControlServer:
        return await self.start()

    async def __aexit__(self, *exc: object) -> None:
        await self.close()
