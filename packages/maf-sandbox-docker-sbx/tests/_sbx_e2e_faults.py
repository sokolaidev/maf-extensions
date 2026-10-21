"""Daemon API fault injection for a disposable Unix live-test host."""

from __future__ import annotations

import contextlib
import http.client
import http.server
import socket
import socketserver
import stat
import threading
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import urlsplit


class _UnixConnection(http.client.HTTPConnection):
    def __init__(self, path: Path):
        super().__init__("localhost", timeout=30)
        self.path = path

    def connect(self) -> None:
        self.sock = socket.socket(getattr(socket, "AF_UNIX"), socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(str(self.path))


@contextlib.contextmanager
def engine_fault_responses(daemon_socket: Path) -> Iterator[list[str]]:
    """Substitute engine-fault HTTP responses, restoring the real daemon on every exit."""
    mode = daemon_socket.lstat().st_mode
    assert stat.S_ISSOCK(mode), f"not a daemon socket: {daemon_socket}"
    hidden = daemon_socket.with_name(f"{daemon_socket.name}.maf-hidden")
    assert not hidden.exists() and not hidden.is_symlink(), f"refusing to replace {hidden}"
    observed: list[str] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            pass

        def do_GET(self) -> None:
            self.respond()

        def do_POST(self) -> None:
            self.respond()

        def do_DELETE(self) -> None:
            self.respond()

        def respond(self) -> None:
            path = urlsplit(self.path).path.rstrip("/")
            if path in ("/sandbox", "/sandboxes") and self.command == "GET":
                observed.append("listing")
                status, body = 200, b"[]"
            elif (
                path in ("/sandbox", "/sandboxes")
                or path.startswith("/sandbox/")
                or path.startswith("/sandboxes/")
            ):
                observed.append(f"{self.command} {path}")
                status, body = 500, b'{"message":"backend unavailable"}'
            else:
                # Host posture and daemon health still come from the running daemon.
                with contextlib.closing(_UnixConnection(hidden)) as upstream:
                    payload = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                    upstream.request(
                        self.command, self.path, body=payload, headers=dict(self.headers)
                    )
                    response = upstream.getresponse()
                    status, body = response.status, response.read()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    class Server(socketserver.ThreadingMixIn, socketserver.TCPServer):
        address_family = getattr(socket, "AF_UNIX")
        daemon_threads = True

    daemon_socket.rename(hidden)
    try:
        with Server(str(daemon_socket), Handler) as server:
            daemon_socket.chmod(stat.S_IMODE(mode))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                yield observed
            finally:
                server.shutdown()
                thread.join(timeout=30)
    finally:
        daemon_socket.unlink(missing_ok=True)
        hidden.rename(daemon_socket)
