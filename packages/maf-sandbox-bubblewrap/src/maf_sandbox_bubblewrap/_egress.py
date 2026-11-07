"""A per-sandbox native iron-proxy, reachable only through a mounted Unix socket."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import socket
import sys
from pathlib import Path

from maf_sandbox import SandboxSpec


def policy(spec: SandboxSpec, directory: Path, ports: list[int]) -> dict[str, object]:
    """Translate only uncredentialed HTTP allowlists into the pinned proxy's policy."""
    domains: list[str] = []
    rules: list[dict[str, object]] = []
    for entry in spec.egress_allow:
        if isinstance(entry, str):
            domains.append(entry)
            continue
        if entry.authority is not None:
            raise ValueError("Bubblewrap does not support egress authority")
        rule: dict[str, object] = {"host": entry.host}
        if entry.methods is not None:
            rule["methods"] = list(entry.methods)
        if entry.paths is not None:
            rule["paths"] = list(entry.paths)
        rules.append(rule)
    return {
        "dns": {"enabled": False},
        "proxy": {
            "http_listen": f"127.0.0.1:{ports[0]}",
            "https_listen": f"127.0.0.1:{ports[1]}",
            "upstream_deny_cidrs": [
                "0.0.0.0/8",
                "10.0.0.0/8",
                "100.64.0.0/10",
                "127.0.0.0/8",
                "169.254.0.0/16",
                "172.16.0.0/12",
                "192.168.0.0/16",
                "224.0.0.0/4",
                "240.0.0.0/4",
                "168.63.129.16/32",
                "::/128",
                "::1/128",
                "fc00::/7",
                "fe80::/10",
                "ff00::/8",
            ],
        },
        "tls": {
            "mode": "mitm",
            "ca_cert": str(directory / "ca.crt"),
            "ca_key": str(directory / "ca.key"),
        },
        "transforms": [{"name": "allowlist", "config": {"domains": domains, "rules": rules}}],
        "metrics": {"listen": f"127.0.0.1:{ports[2]}"},
        "log": {"level": "error"},
    }


class Proxy:
    """Own the proxy process, bounded relay connections and host-only signing material."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.process: asyncio.subprocess.Process | None = None
        self.server: asyncio.Server | None = None
        self.tasks: set[asyncio.Task[None]] = set()
        self.diagnostics: asyncio.Task[None] | None = None
        self.error = bytearray()

    async def start(self, binary: Path, sha256: str, spec: SandboxSpec, group: Path) -> None:
        """Verify the provisioned binary before launching it under owner-death supervision."""
        with binary.open("rb") as source:
            if hashlib.file_digest(source, "sha256").hexdigest() != sha256:
                raise ValueError("Egress proxy binary does not match its configured SHA-256")
        self.directory.mkdir(mode=0o700)
        public = self.directory / "public"
        public.mkdir(mode=0o700)
        ca = await asyncio.create_subprocess_exec(
            str(binary),
            "generate-ca",
            "--outdir",
            str(self.directory),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            async with asyncio.timeout(10):
                if await ca.wait() != 0:
                    raise RuntimeError("Native egress proxy certificate generation failed")
        finally:
            if ca.returncode is None:
                ca.kill()
                await ca.wait()
        (public / "ca.crt").write_bytes((self.directory / "ca.crt").read_bytes())
        with contextlib.ExitStack() as stack:
            sockets = [stack.enter_context(socket.socket()) for _ in range(4)]
            for listener in sockets:
                listener.bind(("127.0.0.1", 0))
            ports = [int(listener.getsockname()[1]) for listener in sockets]
        config = self.directory / "config.json"
        config.write_text(json.dumps(policy(spec, self.directory, ports)), encoding="utf-8")
        self.process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-I",
            str(Path(__file__).with_name("_launch.py")),
            str(os.getpid()),
            str(group),
            str(binary),
            "-config",
            str(config),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env={"PATH": "/usr/bin:/bin", "IRON_PROXY_TUNNEL_LISTEN": f"127.0.0.1:{ports[3]}"},
        )
        self.diagnostics = asyncio.create_task(self._drain())
        async with asyncio.timeout(10):
            while True:
                if self.process.returncode is not None:
                    raise RuntimeError(
                        "Native egress proxy startup failed: " + self.error.decode(errors="replace")
                    )
                try:
                    _, writer = await asyncio.open_connection("127.0.0.1", ports[3])
                except OSError:
                    await asyncio.sleep(0.02)
                else:
                    writer.close()
                    await writer.wait_closed()
                    break

        def connect(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            if len(self.tasks) >= 32 or self.process is None or self.process.returncode is not None:
                writer.close()
                return
            task = asyncio.create_task(self._relay(reader, writer, ports[3]))
            self.tasks.add(task)
            task.add_done_callback(self.tasks.discard)

        self.server = await asyncio.start_unix_server(
            connect, path=public / "proxy.sock", limit=65536
        )

    async def _drain(self) -> None:
        assert self.process is not None and self.process.stdout is not None
        while chunk := await self.process.stdout.read(4096):
            self.error.extend(chunk[: max(0, 8192 - len(self.error))])

    async def _relay(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, port: int
    ) -> None:
        upstream: asyncio.StreamWriter | None = None

        async def copy(source: asyncio.StreamReader, target: asyncio.StreamWriter) -> None:
            while data := await source.read(65536):
                target.write(data)
                await target.drain()

        try:
            async with asyncio.timeout(180):
                remote, upstream = await asyncio.open_connection("127.0.0.1", port)
                tasks = [
                    asyncio.create_task(copy(reader, upstream)),
                    asyncio.create_task(copy(remote, writer)),
                ]
                try:
                    await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                finally:
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
        except (OSError, TimeoutError):
            # Failed connections need only the cleanup below, not a relay retry.
            pass
        finally:
            writer.close()
            if upstream is not None:
                upstream.close()

    async def close(self) -> None:
        """Close the relay after the sandbox's cgroup has been killed."""
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()
        tasks = list(self.tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if self.process is not None:
            await asyncio.wait_for(self.process.wait(), 10)
        if self.diagnostics is not None:
            await self.diagnostics
