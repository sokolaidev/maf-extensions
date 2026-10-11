"""Qualify real Bicep ownership over HTTP and MAF with published dependencies."""

# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "agent-framework-core==1.19.0",
#     "maf-sandbox==0.46.0",
#     "maf-sandbox-bicep==0.22.0",
#     "maf-sandbox-docker==0.24.4",
#     "mcp==1.28.1",
#     "httpx>=0.27,<1",
#     "jsonschema>=4.26,<5",
#     "starlette==1.7.0",
#     "uvicorn==0.54.0",
# ]
# ///

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib
import importlib.metadata
import json
import platform
import secrets
import socket
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
from agent_framework import MCPStreamableHTTPTool
from openclaw_gateway_http_check import records, require
from openclaw_http_observer import ObserveHTTP, digest, resolve_image

BASELINE = {
    "agent-framework-core": "1.19.0",
    "maf-sandbox": "0.46.0",
    "maf-sandbox-bicep": "0.22.0",
    "maf-sandbox-docker": "0.24.4",
    "mcp": "1.28.1",
    "uvicorn": "0.54.0",
    "starlette": "1.7.0",
}
SIMPLE = {"files": [{"path": "main.bicep", "content": "output greeting string = 'hello'"}]}
LARGE = {
    "files": [
        {
            "path": "main.bicep",
            "content": "\n".join(f"var v{i} = range(0, 1000)" for i in range(1500)),
        }
    ]
}


def environment() -> dict[str, str]:
    versions = {}
    for dist in importlib.metadata.distributions():
        direct = json.loads(dist.read_text("direct_url.json") or "{}")
        require(not direct, "Qualification requires published distributions without local fallback")
        versions[dist.metadata["Name"]] = dist.version
    require(
        all(importlib.metadata.version(k) == v for k, v in BASELINE.items()),
        "Unexpected published baseline",
    )
    return dict(sorted(versions.items()))


async def docker(*args: str) -> str:
    process = await asyncio.create_subprocess_exec(
        "docker", *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        out, _ = await asyncio.wait_for(process.communicate(), 20)
    except TimeoutError:
        process.kill()
        await process.communicate()
        raise
    require(process.returncode == 0, "Independent Docker observation failed")
    return out.decode().strip()


async def until(predicate, message: str, seconds: float = 45) -> None:
    async with asyncio.timeout(seconds):
        while not predicate():
            await asyncio.sleep(0.02)
    require(predicate(), message)


def verify_result(value: Any, prototype: Any, image: str, config: str) -> dict[str, Any]:
    require(isinstance(value, dict), "Missing structured Bicep result")
    require(
        value.get("completed") is True
        and value.get("verdict") == "valid"
        and value.get("status") == "ok"
        and value.get("cleanup") == "confirmed",
        "Bicep call did not finish with confirmed cleanup",
    )
    require(
        value.get("image") == image
        and value.get("config_sha256") == hashlib.sha256(config.encode()).hexdigest()
        and value.get("source_sha256") == prototype.snapshot(SIMPLE).digest,
        "Wrong Bicep result identity",
    )
    return {
        k: value[k]
        for k in ("completed", "verdict", "cleanup", "image", "config_sha256", "source_sha256")
    }


def compiler_running(processes: str) -> bool:
    for line in processes.splitlines():
        words = line.split()
        if words and words[0].isdigit():
            words = words[1:]
        if (
            len(words) > 1
            and words[0].rsplit("/", 1)[-1] == "bicep"
            and words[1] in {"build", "lint", "build-params"}
            and "--no-restore" in words
        ):
            return True
    return False


def verify_controls(events, sid: str, *, cancels: int = 0, deleted: bool = False) -> None:
    identity = digest(sid.encode().hex())
    requests = [
        e
        for e in events
        if e.get("event") == "request"
        and e.get("method") == "notifications/cancelled"
        and e.get("session") == identity
        and e.get("target") == digest(42)
    ]
    require(len(requests) == cancels, "Wrong matching cancellation count")
    for request in requests:
        require(
            any(
                e.get("event") == "response"
                and e.get("exchange") == request["exchange"]
                and type(e.get("status")) is int
                and e["status"] == 202
                and e["time_ns"] >= request["time_ns"]
                for e in events
            ),
            "Cancellation lacks its accepted response",
        )
    if deleted:
        retired = [
            e for e in events if e.get("event") == "retired" and e.get("session") == identity
        ]
        require(len(retired) == 1, "Missing or duplicate MAF retirement")
        r = retired[0]
        require(
            all(r.get(k) is True for k in ("completed", "sdk_finished", "sdk_terminated"))
            and all(
                type(r.get(k)) is int and r[k] == 0
                for k in ("sdk_streams", "request_ids", "requests")
            )
            and r.get("session_registered") is False
            and r.get("poisoned") is False,
            "MAF retirement did not settle SDK and adapter state",
        )
        require(
            any(
                e.get("event") == "delete_requested"
                and e.get("exchange") == r["exchange"]
                and e.get("session") == identity
                for e in events
            ),
            "Retirement lacks MAF DELETE",
        )


def observe_binding(observer: ObserveHTTP) -> None:
    binding = observer.app.service.bindings["bicep_validate"]

    async def execute(arguments, context):
        fields = {
            "session": digest(context.session_id.encode().hex()),
            "request": digest(context.request_id),
        }
        observer.record(event="binding_started", **fields)
        try:
            result = await binding.execute(arguments, context)
        except asyncio.CancelledError:
            observer.record(event="binding_cancelled", **fields)
            raise
        structured = result.structuredContent or {}
        observer.record(
            event="binding_result",
            completed=structured.get("completed"),
            cleanup=structured.get("cleanup"),
            status=structured.get("status"),
            source_sha256=structured.get("source_sha256"),
            **fields,
        )
        return result

    observer.app.service.bindings[binding.tool.name] = replace(binding, execute=execute)


class RemovalGate:
    """Hold one real owned-container removal before delegating to the unmodified backend."""

    def __init__(self, backend_type: Any, scope: str):
        self.backend_type = backend_type
        self.scope = scope
        self.original = backend_type._invoke
        self.target: str | None = None
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.removals: list[dict[str, Any]] = []

    def install(self) -> None:
        original = self.original

        async def invoke(backend, *args, **kwargs):
            gated = False
            if len(args) == 3 and args[:2] == ("rm", "-f") and self.target:
                info = json.loads(await docker("inspect", args[2]))[0]
                if info["Id"] == self.target:
                    require(
                        info["Config"]["Labels"].get("maf-sandbox.scope") == self.scope,
                        "Removal barrier crossed ownership",
                    )
                    gated = True
                    self.entered.set()
                    await self.release.wait()
            result = await original(backend, *args, **kwargs)
            if gated:
                self.removals.append(
                    {"container": digest(self.target), "returncode": result.returncode}
                )
            return result

        self.backend_type._invoke = invoke

    def arm(self, container: str) -> None:
        require(self.target is None, "Removal gate already armed")
        self.target = container
        self.entered.clear()
        self.release.clear()

    def reset(self) -> None:
        self.target = None
        self.release.set()

    def restore(self) -> None:
        self.release.set()
        self.backend_type._invoke = self.original


class Check:
    def __init__(
        self,
        prototype: Any,
        app: Any,
        observer: ObserveHTTP,
        client: httpx.AsyncClient,
        image: str,
        config: str,
        scope: str,
        gate: RemovalGate,
    ):
        self.prototype, self.app, self.observer, self.client = prototype, app, observer, client
        self.image, self.config, self.scope, self.gate = image, config, scope, gate
        self.tasks: list[asyncio.Task[Any]] = []
        self.clients: list[MCPStreamableHTTPTool] = []
        self.containers: list[str] = []
        self.observations: dict[str, Any] = {}

    def task(self, coroutine):
        task = asyncio.create_task(coroutine)
        self.tasks.append(task)
        return task

    async def owned(self, *, running: bool = False) -> list[str]:
        return (
            await docker(
                "ps",
                "-q" if running else "-aq",
                "--no-trunc",
                "--filter",
                f"label=maf-sandbox.scope={self.scope}",
            )
        ).split()

    async def initialize(self) -> str:
        r = await self.client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": "ownership-check", "version": "1"},
                },
            },
        )
        require(r.status_code == 200, "Initialization refused")
        sid = r.headers["mcp-session-id"]
        r = await self.client.post(
            "/mcp",
            headers={"mcp-session-id": sid},
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        )
        require(r.status_code == 202, "Initialization not acknowledged")
        return sid

    @staticmethod
    def payload(args: Any) -> dict[str, Any]:
        return {
            "jsonrpc": "2.0",
            "id": 42,
            "method": "tools/call",
            "params": {"name": "bicep_validate", "arguments": args},
        }

    async def call(self, sid: str, args: Any = None):
        return await self.client.post(
            "/mcp",
            headers={"mcp-session-id": sid},
            json=self.payload(SIMPLE if args is None else args),
        )

    async def cancel(self, sid: str) -> None:
        r = await self.client.post(
            "/mcp",
            headers={"mcp-session-id": sid},
            json={
                "jsonrpc": "2.0",
                "method": "notifications/cancelled",
                "params": {"requestId": 42},
            },
        )
        require(r.status_code == 202, "Cancellation not acknowledged")

    async def busy(self, sid: str, container: str) -> None:
        r = await self.call(sid)
        require(
            r.status_code == 200
            and r.json()["result"].get("isError") is True
            and "busy" in r.json()["result"]["content"][0]["text"].lower(),
            "Admission released before cleanup",
        )
        require(
            await self.owned() == [container], "Busy call allocated or lost the active container"
        )
        require(
            self.app.service.active is not None and not self.app.service.active[1].done(),
            "Active supervision did not survive",
        )

    async def compiler(self) -> str:
        async with asyncio.timeout(45):
            while True:
                owned = await self.owned(running=True)
                require(len(owned) <= 1, "More than one owned container")
                if owned and compiler_running(await docker("top", owned[0], "-eo", "pid,args")):
                    container = owned[0]
                    await docker("pause", container)
                    info = json.loads(await docker("inspect", container))[0]
                    require(
                        info["State"]["Paused"] is True
                        and info["HostConfig"]["NetworkMode"] == "none"
                        and info["Image"] == self.image,
                        "Compiler identity or confinement mismatch",
                    )
                    require(
                        compiler_running(await docker("top", container, "-eo", "pid,args")),
                        "Compiler vanished before pause",
                    )
                    self.containers.append(container)
                    return container
                await asyncio.sleep(0.025)

    async def settled(self, container: str) -> None:
        await until(lambda: self.app.service.active is None, "Active slot did not settle")
        require(not await self.owned(), "Owned container survived cleanup")
        require(
            not (await docker("ps", "-aq", "--no-trunc", "--filter", f"id={container}")).split(),
            "Exact container survived",
        )
        require(not self.app.service.poisoned, "Cleanup poisoned the service")

    async def valid(self, client: str | MCPStreamableHTTPTool) -> dict[str, Any]:
        if isinstance(client, str):
            r = await self.call(client)
            require(r.status_code == 200, "Surviving session not usable")
            result = r.json()["result"]
            require(
                len(result["content"]) == 1 and not result.get("isError"),
                "Invalid raw result projection",
            )
            value = result["structuredContent"]
        else:
            result = await client.call_tool("bicep_validate", **SIMPLE)
            require(
                isinstance(result, list) and len(result) == 1 and result[0].text,
                "Invalid MAF projection",
            )
            assert isinstance(result, list) and result[0].text is not None
            value = json.loads(result[0].text)
        return verify_result(value, self.prototype, self.image, self.config)

    def events(self) -> list[dict[str, Any]]:
        return records(self.observer.evidence)

    async def retire_raw(self, *sessions: str) -> None:
        for sid in sessions:
            r = await self.client.delete("/mcp", headers={"mcp-session-id": sid})
            require(r.status_code == 200, "Raw session did not acknowledge retirement")
        await until(lambda: not self.app.sessions, "Transport sessions remain")

    async def raw_cancel(self) -> None:
        a, b = await self.initialize(), await self.initialize()
        start = len(self.events())
        active = self.task(self.call(a, LARGE))
        container = await self.compiler()
        self.gate.arm(container)
        await self.busy(b, container)
        await self.cancel(b)
        await self.busy(b, container)
        require(
            self.app.service.active[0].session_id == a and not self.gate.entered.is_set(),
            "Other session cancelled A",
        )
        await self.cancel(a)
        await asyncio.wait_for(self.gate.entered.wait(), 30)
        for _ in range(3):
            await self.cancel(a)
            await self.busy(b, container)
            require(not self.gate.release.is_set(), "Disposal barrier released early")
        self.gate.release.set()
        await active
        await self.settled(container)
        self.gate.reset()
        value = await self.valid(b)
        self.observations["equal_ids_repeated_cancel"] = {
            "container": digest(container),
            "matching_cancels": 4,
            "other_session_cancels": 1,
            "survivor": value,
        }
        await self.retire_raw(a, b)
        verify_controls(self.events()[start:], a, cancels=4, deleted=True)
        verify_controls(self.events()[start:], b, cancels=1, deleted=True)

    async def response_loss(self) -> None:
        a, b = await self.initialize(), await self.initialize()
        _, writer = await asyncio.open_connection("127.0.0.1", self.app.port)
        try:
            body = json.dumps(self.payload(LARGE)).encode()
            headers = {
                **dict(self.client.headers),
                "host": f"127.0.0.1:{self.app.port}",
                "mcp-session-id": a,
                "content-type": "application/json",
                "content-length": str(len(body)),
            }
            request = (
                "POST /mcp HTTP/1.1\r\n"
                + "".join(f"{k}: {v}\r\n" for k, v in headers.items())
                + "\r\n"
            )
            writer.write(request.encode() + body)
            await writer.drain()
            container = await self.compiler()
            before = len(self.events())
            writer.close()
            await writer.wait_closed()
            await self.busy(b, container)
            require(self.app.service.active[0].session_id == a, "Response loss lost its call owner")
            require(
                not any(
                    e.get("method") == "notifications/cancelled" for e in self.events()[before:]
                ),
                "Response-only loss sent cancellation",
            )
            await docker("unpause", container)
            await self.settled(container)
            results = [
                e
                for e in self.events()[before:]
                if e.get("event") == "binding_result"
                and e.get("session") == digest(a.encode().hex())
            ]
            require(
                len(results) == 1
                and results[0].get("completed") is True
                and results[0].get("cleanup") == "confirmed"
                and results[0].get("source_sha256") == self.prototype.snapshot(LARGE).digest,
                "Lost response call did not complete normally",
            )
            require(
                not any(
                    e.get("event") == "binding_cancelled"
                    or e.get("method") == "notifications/cancelled"
                    for e in self.events()[before:]
                ),
                "Lost response work was cancelled",
            )
            self.observations["response_loss"] = {
                "container": digest(container),
                "socket_closed": writer.is_closing(),
                "survivor": await self.valid(b),
            }
        finally:
            writer.close()
            await writer.wait_closed()
        await self.retire_raw(a, b)

    async def maf(self) -> tuple[MCPStreamableHTTPTool, str]:
        before = set(self.app.sessions)
        client = MCPStreamableHTTPTool(
            name="bicep",
            url=str(self.client.base_url) + "/mcp",
            static_headers={"Authorization": self.client.headers["Authorization"]},
            load_prompts=False,
            request_timeout=240,
        )
        self.clients.append(client)
        await client.connect()
        sessions = set(self.app.sessions) - before
        require(
            len(sessions) == 1 and client.session is not None,
            "MAF did not establish one transport session",
        )
        assert client.session is not None
        tools = await client.session.list_tools()
        expected = await self.prototype.make_server(None).list_tools()
        require(tools.tools == expected, "MAF discovery differs from the Bicep contract")
        return client, sessions.pop()

    async def maf_close(self, active_close: bool) -> None:
        a, a_sid = await self.maf()
        b, b_sid = await self.maf()
        running = a if active_close else b
        active = self.task(running.call_tool("bicep_validate", **LARGE))
        container = await self.compiler()
        if active_close:
            self.gate.arm(container)
        before = len(self.events())
        closing = self.task(a.close())
        await until(
            lambda: any(
                e.get("event") == "delete_requested"
                and e.get("session") == digest(a_sid.encode().hex())
                for e in self.events()[before:]
            ),
            "MAF close sent no DELETE",
        )
        if active_close:
            await asyncio.wait_for(self.gate.entered.wait(), 30)
            await self.busy(b_sid, container)
            require(
                b_sid in self.app.sessions and a_sid in self.app.sessions,
                "Active close released transport ownership too early",
            )
            self.gate.release.set()
        else:
            await closing
            await until(lambda: a_sid not in self.app.sessions, "Idle MAF session did not retire")
            await self.busy(b_sid, container)
            require(self.app.service.active[0].session_id == b_sid, "Idle MAF close affected B")
            await docker("unpause", container)
        outcome = (await asyncio.gather(active, return_exceptions=True))[0]
        await closing
        await self.settled(container)
        self.gate.reset()
        await until(lambda: a_sid not in self.app.sessions, "MAF retirement incomplete")
        require(
            b_sid in self.app.sessions and not self.app.sessions[b_sid].closing,
            "Surviving MAF session was closed",
        )
        verify_controls(self.events()[before:], a_sid, deleted=True)
        self.observations["maf_active_close" if active_close else "maf_idle_close"] = {
            "container": digest(container),
            "delete_observed": True,
            "call_raised": isinstance(outcome, BaseException),
            "survivor": await self.valid(b),
        }
        await b.close()
        await until(lambda: not self.app.sessions, "MAF sessions did not drain")

    async def ordinary_close(self) -> None:
        a, a_sid = await self.maf()
        b, b_sid = await self.maf()
        await self.valid(a)
        await self.valid(b)
        await a.close()
        await until(lambda: a_sid not in self.app.sessions, "Ordinary MAF close did not retire")
        require(b_sid in self.app.sessions, "Ordinary close lost B")
        self.observations["maf_ordinary_close"] = {"survivor": await self.valid(b)}
        await b.close()
        await until(lambda: not self.app.sessions, "Ordinary MAF sessions did not drain")

    async def finish(self) -> None:
        self.gate.release.set()
        for container in await self.owned():
            info = json.loads(await docker("inspect", container))[0]
            if info["State"]["Paused"]:
                await docker("unpause", container)
        for client in self.clients:
            await client.close()
        for sid in list(self.app.sessions):
            self.app.retire(sid)
        await asyncio.wait_for(asyncio.gather(*self.tasks, return_exceptions=True), 60)


async def check(args: argparse.Namespace) -> dict[str, Any]:
    versions = environment()
    sys.path.insert(0, str(args.prototype.resolve().parent))
    prototype = importlib.import_module("server")
    transport = importlib.import_module("workload_http")
    image = resolve_image(args.image)
    config = args.config.read_text(encoding="utf-8")
    token = secrets.token_hex(32)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sentinel = ""
    report: dict[str, Any] = {
        "versions": versions,
        "python": platform.python_version(),
        "platform": platform.system(),
        "image": image,
        "image_reference": args.image,
        "config_sha256": hashlib.sha256(config.encode()).hexdigest(),
    }
    with prototype.ownership(args.root / "owner") as scope:
        app = await prototype.http_application(image, config, scope, token, port)
        observer = ObserveHTTP(app, args.root / "transport.jsonl", owned_scope=scope)
        observe_binding(observer)
        gate = RemovalGate(prototype.DockerSandboxBackend, scope)
        gate.install()
        host = transport.server(app)
        host.config.app = observer
        serving = asyncio.create_task(host.serve(sockets=[sock]))
        try:
            await until(lambda: host.started or serving.done(), "Service did not start")
            require(host.started and not serving.done(), "Service startup failed")
            sentinel = await docker(
                "run",
                "-d",
                "--network",
                "none",
                "--label",
                "maf-sandbox.scope=qualification-unrelated-" + secrets.token_hex(16),
                image,
                "sleep",
                "infinity",
            )
            async with httpx.AsyncClient(
                base_url=f"http://127.0.0.1:{port}",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/json, text/event-stream",
                    "mcp-protocol-version": "2025-11-25",
                },
                timeout=240,
            ) as client:
                probe = Check(prototype, app, observer, client, image, config, scope, gate)
                try:
                    for scenario in (
                        probe.raw_cancel,
                        probe.response_loss,
                        lambda: probe.maf_close(False),
                        lambda: probe.maf_close(True),
                        probe.ordinary_close,
                    ):
                        await scenario()
                        print(
                            json.dumps({"completed_case": list(probe.observations)[-1]}), flush=True
                        )
                        require(
                            (await docker("inspect", "--format", "{{.State.Running}}", sentinel))
                            == "true",
                            "Unrelated owner was affected",
                        )
                        require(not await probe.owned(), "Scenario left an owned container")
                    report["cases"] = probe.observations
                    report["removals"] = gate.removals
                finally:
                    await probe.finish()
            host.should_exit = True
            await asyncio.wait_for(serving, 60)
            require(
                not app.sessions and app.service.active is None and not app.service.poisoned,
                "Service did not shut down cleanly",
            )
            require(not await probe.owned(), "Owned resources survived shutdown")
            report["shutdown"] = {
                "sessions": len(app.sessions),
                "active": app.service.active is not None,
                "poisoned": app.service.poisoned,
                "owned_empty": True,
                "unrelated_preserved": True,
            }
            report["transport"] = records(observer.evidence)
        finally:
            gate.release.set()
            host.should_exit = True
            try:
                await asyncio.wait_for(serving, 90)
            finally:
                gate.restore()
                sock.close()
                if sentinel:
                    await docker("rm", "-f", sentinel)
    report["sentinel_removed"] = not bool(await docker("ps", "-aq", "--filter", f"id={sentinel}"))
    require(report["sentinel_removed"], "Qualification sentinel survived")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("prototype", "config", "root"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--image", required=True)
    args = parser.parse_args()
    args.root.mkdir(parents=True, exist_ok=True)
    output = args.root / "ownership-report.json"
    output.unlink(missing_ok=True)
    require(not (args.root / "transport.jsonl").exists(), "Use a fresh private evidence directory")
    repo = args.prototype.resolve().parents[3]
    candidate = subprocess.check_output(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], text=True
    ).strip()
    require(
        not subprocess.check_output(
            ["git", "-C", str(repo), "status", "--porcelain"], text=True
        ).strip(),
        "Qualification requires a clean candidate",
    )
    report = asyncio.run(check(args))
    report.update(
        candidate=candidate,
        source_hashes={
            str(p.resolve().relative_to(repo)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in [*args.prototype.parent.glob("*.py"), Path(__file__)]
        },
        passed=True,
    )
    output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"passed": True, "cases": list(report["cases"]), "candidate": candidate}))


if __name__ == "__main__":
    main()
