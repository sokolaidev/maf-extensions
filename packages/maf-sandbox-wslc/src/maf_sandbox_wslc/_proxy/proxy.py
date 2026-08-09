"""A filtering CONNECT proxy — the egress half of ``Egress.ALLOWLIST`` on the wslc backend.

It runs dual-homed beside a sandbox whose only network is internal, so the allowlist is
enforced by topology rather than by the client's cooperation: the sandbox has no route out
except through this process, and this process opens a tunnel only to the ``host:port`` pairs
it was told to allow. CONNECT tunnels TLS end to end — nothing is decrypted, and the target
hostname is resolved here, on the egress side, never inside the sandbox.

Stdlib only, and importable with no side effects: the file is copied into the image verbatim
and run as ``python3 /proxy.py``, while the package imports it for its tests.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from collections.abc import Sequence
from fnmatch import fnmatchcase

_DEFAULT_PORT = 3128
_CHUNK = 65536


def host_allowed(host: str, allowlist: Sequence[str]) -> bool:
    """Whether ``host`` matches the allowlist — exact or ``*.`` wildcard, case-insensitive."""
    lowered = host.lower()
    return any(fnmatchcase(lowered, pattern.lower()) for pattern in allowlist)


class ProxyServer:
    """Accepts CONNECT to allowed ``host:port`` pairs and tunnels bytes; refuses the rest."""

    def __init__(self, allowlist: Sequence[str], *, ports: Sequence[int] = (443,)) -> None:
        self._allowlist = tuple(allowlist)
        self._ports = tuple(ports)
        self._server: asyncio.Server | None = None

    @property
    def bound_port(self) -> int:
        """The port the listener actually bound, for callers that asked for port 0."""
        assert self._server is not None
        return self._server.sockets[0].getsockname()[1]

    async def start(self, host: str = "127.0.0.1", port: int = 0) -> None:
        """Start listening; ``bound_port`` is valid once this returns."""
        self._server = await asyncio.start_server(self._serve, host, port)

    async def aclose(self) -> None:
        """Stop listening. In-flight tunnels are left to finish on their own."""
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await self._handle(reader, writer)
        finally:
            with contextlib.suppress(Exception):
                writer.close()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await reader.readuntil(b"\r\n\r\n")
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, ConnectionError):
            await self._respond(writer, "400 Bad Request")
            return
        parts = head.split(b"\r\n", 1)[0].decode("latin-1").split()
        if len(parts) != 3 or not parts[2].startswith("HTTP/"):
            await self._respond(writer, "400 Bad Request")
            return
        method, target = parts[0], parts[1]
        if method != "CONNECT":
            await self._respond(writer, "405 Method Not Allowed")
            return
        host, _, port_text = target.rpartition(":")
        if not host or not port_text.isdigit():
            await self._respond(writer, "400 Bad Request")
            return
        port = int(port_text)
        if not host_allowed(host, self._allowlist) or port not in self._ports:
            print(f"DENY {host}:{port}", flush=True)
            await self._respond(writer, "403 Forbidden")
            return
        try:
            target_reader, target_writer = await asyncio.open_connection(host, port)
        except OSError:
            print(f"UNREACHABLE {host}:{port}", flush=True)
            await self._respond(writer, "502 Bad Gateway")
            return
        print(f"ALLOW {host}:{port}", flush=True)
        writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        await writer.drain()
        try:
            await asyncio.gather(
                self._pipe(reader, target_writer), self._pipe(target_reader, writer)
            )
        finally:
            with contextlib.suppress(Exception):
                target_writer.close()

    @staticmethod
    async def _respond(writer: asyncio.StreamWriter, status: str) -> None:
        with contextlib.suppress(ConnectionError):
            writer.write(f"HTTP/1.1 {status}\r\ncontent-length: 0\r\n\r\n".encode())
            await writer.drain()

    @staticmethod
    async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while data := await reader.read(_CHUNK):
                writer.write(data)
                await writer.drain()
        except (ConnectionError, OSError):
            return
        finally:
            with contextlib.suppress(Exception):
                writer.write_eof()


def main() -> None:
    """Run as the container entrypoint, configured from the environment."""
    allow = tuple(
        host.strip() for host in os.environ.get("MAF_SANDBOX_ALLOW", "").split(",") if host.strip()
    )
    port = int(os.environ.get("MAF_SANDBOX_PROXY_PORT", str(_DEFAULT_PORT)))

    async def run() -> None:
        server = ProxyServer(allow)
        await server.start("0.0.0.0", port)
        print(f"listening on {port}; allowing: {', '.join(allow) or 'nothing'}", flush=True)
        await asyncio.Event().wait()

    asyncio.run(run())


if __name__ == "__main__":
    main()
