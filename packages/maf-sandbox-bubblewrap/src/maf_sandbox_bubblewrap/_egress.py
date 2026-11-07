"""A per-sandbox native iron-proxy, reachable only through a mounted Unix socket."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import cast

from maf_sandbox import SandboxSpec


def policy(spec: SandboxSpec, directory: Path) -> dict[str, object]:
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
            "http_listen": "127.0.0.1:0",
            "https_listen": "127.0.0.1:0",
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
        "metrics": {"listen": "127.0.0.1:0"},
        "log": {"level": "info"},
    }


def _owns_listener(pid: int, port: int, proc_root: Path = Path("/proc")) -> bool:
    """Require the listener inode to belong to this process, not just its network namespace."""
    root = proc_root / str(pid)
    try:
        sockets: set[str] = set()
        for descriptor in (root / "fd").iterdir():
            with contextlib.suppress(OSError):
                sockets.add(os.readlink(descriptor))
        with (root / "net/tcp").open(encoding="ascii") as table:
            for line in table:
                fields = line.split()
                if (
                    len(fields) > 9
                    and fields[1] == f"0100007F:{port:04X}"
                    and fields[3] == "0A"
                    and f"socket:[{fields[9]}]" in sockets
                ):
                    return True
    except OSError:
        return False
    return False


class Proxy:
    """Own the proxy process, bounded relay connections and host-only signing material."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.process: asyncio.subprocess.Process | None = None
        self.server: asyncio.Server | None = None
        self.tasks: set[asyncio.Task[None]] = set()
        self.diagnostics: asyncio.Task[None] | None = None
        self.error = bytearray()
        self.tunnel_port: int | None = None

    async def start(self, binary: Path, sha256: str, spec: SandboxSpec, group: Path) -> None:
        """Verify the provisioned binary before launching it under owner-death supervision."""
        with binary.open("rb") as source:
            if hashlib.file_digest(source, "sha256").hexdigest() != sha256:
                raise ValueError("Egress proxy binary does not match its configured SHA-256")
        self.directory.mkdir(mode=0o700)
        public = self.directory / "public"
        public.mkdir(mode=0o700)
        launch = (
            sys.executable,
            "-I",
            str(Path(__file__).with_name("_launch.py")),
            str(os.getpid()),
            str(group),
        )
        ca = await asyncio.create_subprocess_exec(
            *launch,
            str(binary),
            "generate-ca",
            "--outdir",
            str(self.directory),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            env={"PATH": "/usr/bin:/bin"},
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
        config = self.directory / "config.json"
        config.write_text(json.dumps(policy(spec, self.directory)), encoding="utf-8")
        self.process = await asyncio.create_subprocess_exec(
            *launch,
            str(binary),
            "-config",
            str(config),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env={"PATH": "/usr/bin:/bin", "IRON_PROXY_TUNNEL_LISTEN": "127.0.0.1:0"},
        )
        self.diagnostics = asyncio.create_task(self._drain())
        async with asyncio.timeout(10):
            while True:
                if self.process.returncode is not None:
                    raise RuntimeError(
                        "Native egress proxy startup failed: " + self.error.decode(errors="replace")
                    )
                if self.tunnel_port is not None and _owns_listener(
                    self.process.pid, self.tunnel_port
                ):
                    break
                await asyncio.sleep(0.02)

        port = self.tunnel_port
        assert port is not None

        def connect(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            if len(self.tasks) >= 32 or self.process is None or self.process.returncode is not None:
                writer.close()
                return
            task = asyncio.create_task(self._relay(reader, writer, port))
            self.tasks.add(task)
            task.add_done_callback(self.tasks.discard)

        self.server = await asyncio.start_unix_server(
            connect, path=public / "proxy.sock", limit=65536
        )

    async def _drain(self) -> None:
        assert self.process is not None and self.process.stdout is not None
        pending = bytearray()
        discard = False
        while chunk := await self.process.stdout.read(4096):
            self.error.extend(chunk[: max(0, 8192 - len(self.error))])
            parts = chunk.split(b"\n")
            for index, part in enumerate(parts):
                if not discard:
                    pending.extend(part)
                    if len(pending) > 8192:
                        pending.clear()
                        discard = True
                if index < len(parts) - 1:
                    if not discard and self.tunnel_port is None:
                        self._read_ready(pending)
                    pending.clear()
                    discard = False

    def _read_ready(self, line: bytearray) -> None:
        """Read the kernel-selected address from the pinned child's structured startup log."""
        try:
            value: object = json.loads(line)
            if not isinstance(value, dict):
                return
            record = cast(dict[str, object], value)
            if record.get("msg") != "tunnel proxy starting":
                return
            address = record.get("addr")
            if not isinstance(address, str) or not address.startswith("127.0.0.1:"):
                return
            port = int(address.removeprefix("127.0.0.1:"))
            if 0 < port < 65536:
                self.tunnel_port = port
        except (ValueError, UnicodeError):
            return

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
                if (
                    self.process is None
                    or self.process.returncode is not None
                    or not _owns_listener(self.process.pid, port)
                ):
                    return
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
