"""Authenticated loopback control endpoint hosted by the owning MAF process."""

from __future__ import annotations

import asyncio
import hmac
import json
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
from urllib.parse import unquote, urlsplit

from ._control import SandboxControl

PROTOCOL_VERSION = 1


def runtime_directory() -> Path:
    """Return the per-user directory used for local endpoint discovery."""
    configured = os.environ.get("MAF_SANDBOX_TUI_RUNTIME_DIR")
    if configured:
        return Path(configured)
    if os.name == "nt":
        local = os.environ.get("LOCALAPPDATA")
        if local:
            return Path(local) / "sokolai" / "maf-sandbox-tui"
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime:
        return Path(runtime) / "maf-sandbox-tui"
    user_id = getattr(os, "getuid", os.getpid)()
    return Path(tempfile.gettempdir()) / f"maf-sandbox-tui-{user_id}"


@dataclass(frozen=True)
class EndpointManifest:
    """Discovery information for one owning MAF process."""

    source_id: str
    endpoint: str
    token: str
    process_id: int
    protocol_version: int = PROTOCOL_VERSION

    def to_json(self) -> dict[str, object]:
        """Return the discovery file representation."""
        return {
            "source_id": self.source_id,
            "endpoint": self.endpoint,
            "token": self.token,
            "process_id": self.process_id,
            "protocol_version": self.protocol_version,
        }

    @classmethod
    def from_json(cls, value: object) -> EndpointManifest:
        """Read and validate a discovery file."""
        if not isinstance(value, dict):
            raise ValueError("endpoint manifest must be an object")
        data = cast("dict[object, object]", value)
        source_id, endpoint, token = (
            data.get("source_id"),
            data.get("endpoint"),
            data.get("token"),
        )
        process_id, version = data.get("process_id"), data.get("protocol_version")
        if not all(isinstance(item, str) and item for item in (source_id, endpoint, token)):
            raise ValueError("endpoint manifest identity fields must be nonempty strings")
        if isinstance(process_id, bool) or not isinstance(process_id, int):
            raise ValueError("endpoint manifest process_id must be an integer")
        if version != PROTOCOL_VERSION:
            raise ValueError(f"unsupported control protocol version {version!r}")
        return cls(
            cast("str", source_id),
            cast("str", endpoint),
            cast("str", token),
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

    def _authorized(self) -> bool:
        authorization = self.headers.get("Authorization", "")
        scheme, _, supplied = authorization.partition(" ")
        return scheme.lower() == "bearer" and hmac.compare_digest(
            supplied, self._control_server.owner.token
        )

    def _send(self, status: HTTPStatus, value: object) -> None:
        body = json.dumps(value, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-MAF-Sandbox-Control-Version", str(PROTOCOL_VERSION))
        self.end_headers()
        self.wfile.write(body)

    def _run(self, operation: Coroutine[Any, Any, Any]) -> Any:
        future = asyncio.run_coroutine_threadsafe(operation, self._control_server.owner.loop)
        return future.result(timeout=self._control_server.owner.request_timeout)

    def _require_authorization(self) -> bool:
        if self._authorized():
            return True
        self._send(HTTPStatus.UNAUTHORIZED, {"error": "missing or invalid bearer token"})
        return False

    def do_GET(self) -> None:  # noqa: N802
        if not self._require_authorization():
            return
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
                records = self._run(self._control_server.owner.control.list_sandboxes())
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
                records = self._run(self._control_server.owner.control.list_sandboxes())
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
            self._send(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "control request failed"})

    def do_DELETE(self) -> None:  # noqa: N802
        if not self._require_authorization():
            return
        path = urlsplit(self.path).path
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
                    timeout=self._control_server.owner.dispose_timeout,
                )
            )
            status = HTTPStatus.OK if result.status.value != "failed" else HTTPStatus.CONFLICT
            self._send(status, result.to_json())
        except FutureTimeoutError:
            self._send(HTTPStatus.GATEWAY_TIMEOUT, {"error": "disposal timed out"})
        except Exception:
            self._send(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "disposal failed"})

    def log_message(self, format: str, *args: object) -> None:
        del format, args


class SandboxControlServer:
    """Serve one MAF process's cooperative sandbox control surface on loopback."""

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
        if dispose_timeout <= 0:
            raise ValueError("dispose_timeout must be positive")
        self.control = control
        self.source_id = source_id
        self.dispose_timeout = dispose_timeout
        self.request_timeout = dispose_timeout + 5.0
        self.token = secrets.token_urlsafe(32)
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
        return EndpointManifest(self.source_id, self.endpoint, self.token, os.getpid())

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
            temporary.chmod(0o600)
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
