"""Real HTTP and host-lifecycle acceptance for the reusable experimental MCP service."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import socket
import sys
import uuid
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
from agent_framework import MCPStreamableHTTPTool
from mcp import types

ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "samples/experimental/openclaw_bicep"


def load(name, filename=None):
    spec = importlib.util.spec_from_file_location(name, SAMPLE / f"{filename or name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


core = load("workload_service")
http = load("workload_http")
TOKEN = "a" * 64
HEADERS = {
    "Authorization": f"Bearer {TOKEN}",
    "Accept": "application/json, text/event-stream",
    "mcp-protocol-version": "2025-11-25",
}


def result(value):
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=json.dumps(value, indent=2))],
        structuredContent=value,
    )


class Harness:
    def __init__(self):
        self.starts = 0
        self.cleans = 0
        self.closes = 0
        self.calls = []
        self.hold = asyncio.Event()
        self.entered = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.cleanup_hold = asyncio.Event()
        self.cleanup_hold.set()
        self.clean = True

    async def start(self):
        self.starts += 1

    async def cleanup(self):
        self.cleans += 1
        await self.cleanup_hold.wait()
        return self.clean

    async def close(self):
        self.closes += 1

    async def execute(self, args, context):
        self.calls.append(context)
        if args.get("hold"):
            self.entered.set()
            try:
                await self.hold.wait()
            except asyncio.CancelledError:
                self.cancelled.set()
                raise
        return result({"answer": args.get("value", "done")})

    async def second(self, args, context):
        self.calls.append(context)
        return result({"total": args["number"] + 1})

    def service(self):
        resource = core.Resource(
            "fake", self.start, self.cleanup, self.close, frozenset({"isolated"})
        )
        a = core.Binding(
            types.Tool(
                name="echo",
                description="Bounded test workload",
                inputSchema={
                    "type": "object",
                    "properties": {"value": {"type": "string"}, "hold": {"type": "boolean"}},
                    "additionalProperties": False,
                },
                outputSchema={
                    "type": "object",
                    "properties": {"answer": {"type": "string"}},
                    "required": ["answer"],
                },
            ),
            self.execute,
            ("fake",),
            frozenset({"isolated"}),
            1024,
            2048,
            30,
        )
        b = replace(
            a,
            tool=types.Tool(
                name="increment",
                inputSchema={
                    "type": "object",
                    "properties": {"number": {"type": "integer"}},
                    "required": ["number"],
                    "additionalProperties": False,
                },
                outputSchema={
                    "type": "object",
                    "properties": {"total": {"type": "integer"}},
                    "required": ["total"],
                },
            ),
            execute=self.second,
        )
        return core.WorkloadService([a, b], [resource])


@asynccontextmanager
async def running(service, shutdown_seconds=5):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    app = http.WorkloadHTTP(service, TOKEN, port)
    server = http.server(app)
    task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        async with asyncio.timeout(5):
            while not server.started:
                if task.done():
                    task.result()
                    raise AssertionError("Server stopped before readiness")
                await asyncio.sleep(0.01)
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}", headers=HEADERS, timeout=5
        ) as client:
            yield app, client, server
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, shutdown_seconds)
        sock.close()


async def initialize(client):
    response = await client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "acceptance", "version": "1"},
            },
        },
    )
    assert response.status_code == 200, response.text
    sid = response.headers["mcp-session-id"]
    response = await client.post(
        "/mcp",
        headers={"mcp-session-id": sid},
        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
    )
    assert response.status_code == 202, response.text
    return sid


async def call(client, sid, name="echo", args=None, request_id=42):
    return await client.post(
        "/mcp",
        headers={"mcp-session-id": sid},
        json={
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "tools/call",
            "params": {"name": name, "arguments": args or {}},
        },
    )


async def cancel(client, sid, request_id=42):
    response = await client.post(
        "/mcp",
        headers={"mcp-session-id": sid},
        json={
            "jsonrpc": "2.0",
            "method": "notifications/cancelled",
            "params": {"requestId": request_id},
        },
    )
    assert response.status_code == 202


async def until(predicate):
    async with asyncio.timeout(5):
        while not predicate():
            await asyncio.sleep(0.01)


def test_registration_policy_and_schema_contract():
    h = Harness()
    service = h.service()
    bindings = list(service.bindings.values())
    resources = list(service.resources.values())
    with pytest.raises(ValueError, match="Duplicate"):
        core.WorkloadService([bindings[0], bindings[0]], resources)
    with pytest.raises(ValueError, match="Unsupported"):
        core.WorkloadService(
            [replace(bindings[0], required_capabilities=frozenset({"missing"}))], resources
        )
    with pytest.raises(ValueError, match="limits"):
        core.WorkloadService([replace(bindings[0], deadline_seconds=float("inf"))], resources)
    for file in ("workload_service.py", "workload_http.py"):
        source = (SAMPLE / file).read_text(encoding="utf-8-sig")
        assert "from maf_sandbox" not in source
        assert "import maf_sandbox" not in source


def test_two_maf_clients_discover_distinct_bindings_and_close_independently():
    async def scenario():
        h = Harness()
        async with running(h.service()) as (app, client, server):
            url = str(client.base_url) + "/mcp"
            a = MCPStreamableHTTPTool(
                name="a",
                url=url,
                static_headers={"Authorization": f"Bearer {TOKEN}"},
                load_prompts=False,
            )
            b = MCPStreamableHTTPTool(
                name="b",
                url=url,
                static_headers={"Authorization": f"Bearer {TOKEN}"},
                load_prompts=False,
            )
            async with b:
                async with a:
                    assert a.session is not None
                    assert {t.name for t in (await a.session.list_tools()).tools} == {
                        "echo",
                        "increment",
                    }
                    response = await a.call_tool("echo", value="hello")
                    assert isinstance(response, list) and len(response) == 1
                    assert response[0].text is not None
                    assert json.loads(response[0].text) == {"answer": "hello"}
                    response = await b.call_tool("increment", number=4)
                    assert isinstance(response, list) and len(response) == 1
                    assert response[0].text is not None
                    assert json.loads(response[0].text) == {"total": 5}
                    assert h.starts == 1
                assert len(await b.call_tool("echo", value="still connected")) == 1
            await until(lambda: not app.sessions)
        assert h.closes == 1

    asyncio.run(scenario())


def test_shared_admission_equal_ids_cancel_and_delete_drain():
    async def scenario():
        h = Harness()
        service = h.service()
        async with running(service) as (app, client, server):
            a, b = await initialize(client), await initialize(client)
            active = asyncio.create_task(call(client, a, args={"hold": True}))
            await h.entered.wait()
            busy = await call(client, b, "increment", {"number": 4})
            assert busy.json()["result"]["isError"] and len(h.calls) == 1
            duplicate = await call(client, a)
            assert duplicate.status_code == 409
            await cancel(client, b)
            assert not h.cancelled.is_set()
            h.cleanup_hold.clear()
            await cancel(client, a)
            await h.cancelled.wait()
            await cancel(client, a)
            assert service.active is not None
            deleted = await client.delete("/mcp", headers={"mcp-session-id": a})
            assert deleted.status_code == 200 and a in app.sessions
            assert (await call(client, b)).json()["result"]["isError"]
            h.cleanup_hold.set()
            await active
            await until(lambda: a not in app.sessions)
            assert (await call(client, b, "increment", {"number": 4})).json()["result"][
                "structuredContent"
            ] == {"total": 5}
            assert (await call(client, a)).status_code == 404
            await client.delete("/mcp", headers={"mcp-session-id": b})

    asyncio.run(scenario())


def test_registry_saturation_churn_and_idle_expiry():
    async def scenario():
        h = Harness()
        async with running(h.service()) as (app, client, server):
            ids = [await initialize(client) for _ in range(8)]
            response = await client.post(
                "/mcp",
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-11-25",
                        "capabilities": {},
                        "clientInfo": {"name": "ninth", "version": "1"},
                    },
                },
            )
            assert response.status_code == 503 and len(app.sessions) == 8
            for sid in ids:
                await client.delete("/mcp", headers={"mcp-session-id": sid})
            await until(lambda: not app.sessions)
            for _ in range(100):
                sid = await initialize(client)
                await client.delete("/mcp", headers={"mcp-session-id": sid})
                await until(lambda: not app.sessions)
            assert app.service._session_manager is None
            app.idle_seconds = 0.04
            await initialize(client)
            await until(lambda: not app.sessions)

    asyncio.run(scenario())


@pytest.mark.parametrize("method", ["GET", "POST", "DELETE"])
def test_authentication_and_host_refusal_precede_allocation(method):
    async def scenario():
        async with running(Harness().service()) as (app, client, server):
            for headers, status in [
                ({"Authorization": ""}, 401),
                ({"Authorization": "Bearer wrong"}, 401),
                ({"Host": "evil.example"}, 403),
                ({"Origin": "http://127.0.0.1"}, 403),
            ]:
                response = await client.request(
                    method, "/mcp", headers=headers, content=b"secret-invalid-json"
                )
                assert response.status_code == status and not app.sessions
                assert "secret" not in response.text

    asyncio.run(scenario())


def test_bad_initialization_input_and_output_bounds():
    async def scenario():
        h = Harness()
        async with running(h.service()) as (app, client, server):
            for body in (
                {"jsonrpc": "2.0", "id": 1, "method": "initialize"},
                {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                [{"jsonrpc": "2.0"}],
            ):
                response = await client.post("/mcp", json=body)
                assert response.status_code == 400 and not app.sessions
            response = await client.post("/mcp", content=b"x" * (http.BODY_BYTES + 1))
            assert response.status_code == 413 and not app.sessions
            sid = await initialize(client)
            response = await call(client, sid, "increment", {"number": "4"})
            assert response.json()["result"]["isError"] and not h.calls
            response = await call(client, sid, args={"value": "x" * 1100})
            assert response.json()["result"]["isError"] and not h.calls
            app.service.bindings["echo"] = replace(
                app.service.bindings["echo"], max_output_bytes=256
            )
            response = await call(client, sid, args={"value": "x" * 500})
            assert response.json()["result"]["isError"]

    asyncio.run(scenario())


def test_cleanup_failure_poison_and_startup_rollback():
    async def scenario():
        h = Harness()
        async with running(h.service()) as (app, client, server):
            sid = await initialize(client)
            h.clean = False
            response = await call(client, sid)
            assert response.json()["result"]["isError"] and app.service.poisoned
            assert (await client.get("/ready")).status_code == 503
            assert (await call(client, sid)).json()["result"]["isError"]
            assert len(h.calls) == 1
            h.clean = True
        h = Harness()
        s = h.service()

        async def fail():
            raise RuntimeError("failed start")

        s.resources["bad"] = core.Resource("bad", fail, h.cleanup, h.close)
        with pytest.raises(RuntimeError, match="failed start"):
            async with s.lifespan():
                pytest.fail("became ready")
        assert not s.ready and h.closes == 2

    asyncio.run(scenario())


def test_disconnect_does_not_cancel_and_idle_close_preserves_other_call():
    async def scenario():
        h = Harness()
        service = h.service()
        async with running(service) as (app, client, server):
            a, b = await initialize(client), await initialize(client)

            async def disconnected_call():
                with pytest.raises(httpx.ReadTimeout):
                    await client.post(
                        "/mcp",
                        headers={"mcp-session-id": b},
                        timeout=0.1,
                        json={
                            "jsonrpc": "2.0",
                            "id": 42,
                            "method": "tools/call",
                            "params": {"name": "echo", "arguments": {"hold": True}},
                        },
                    )

            lost = asyncio.create_task(disconnected_call())
            await h.entered.wait()
            await lost
            assert service.active is not None and not h.cancelled.is_set()
            await client.delete("/mcp", headers={"mcp-session-id": a})
            await until(lambda: a not in app.sessions)
            assert service.active is not None and not h.cancelled.is_set()
            h.hold.set()
            await until(lambda: service.active is None and not app.sessions[b].ids)
            assert not (await call(client, b)).json()["result"]["isError"]

    asyncio.run(scenario())


def test_get_stream_limit_and_shutdown_retirement():
    async def scenario():
        h = Harness()
        async with running(h.service()) as (app, client, server):
            sid = await initialize(client)
            async with client.stream("GET", "/mcp", headers={"mcp-session-id": sid}) as response:
                assert response.status_code == 200
                duplicate = await client.get("/mcp", headers={"mcp-session-id": sid})
                assert duplicate.status_code == 409
                server.should_exit = True
                await until(lambda: not app.sessions)
        assert h.closes == 1

    asyncio.run(scenario())


def test_actual_chunked_body_header_and_slow_body_limits():
    async def scenario():
        async with running(Harness().service()) as (app, client, server):

            async def oversized():
                for _ in range(33):
                    yield b"x" * 65536

            response = await client.post(
                "/mcp", content=oversized(), headers={"Content-Type": "application/json"}
            )
            assert response.status_code == 413 and not app.sessions
            response = await client.post(
                "/mcp", headers={"large": "x" * http.HEADER_BYTES}, content=b"{}"
            )
            assert response.status_code in {400, 431} and not app.sessions
            app.body_seconds = 0.02

            async def slow():
                yield b"{"
                await asyncio.sleep(0.1)
                yield b"}"

            response = await client.post(
                "/mcp", content=slow(), headers={"Content-Type": "application/json"}
            )
            assert response.status_code == 408 and not app.sessions and app.readers == 0

    asyncio.run(scenario())


def test_actual_connection_cap_includes_incomplete_requests():
    async def scenario():
        async with running(Harness().service()) as (app, client, server):
            opened = []
            try:
                for _ in range(http.CONNECTIONS):
                    opened.append(await asyncio.open_connection("127.0.0.1", app.port))
                await until(lambda: len(server.server_state.connections) == http.CONNECTIONS)
                reader, writer = await asyncio.open_connection("127.0.0.1", app.port)
                opened.append((reader, writer))
                assert await asyncio.wait_for(reader.read(), 1) == b""
                assert len(server.server_state.connections) == http.CONNECTIONS
            finally:
                for _, writer in opened:
                    writer.close()
                await asyncio.gather(*(w.wait_closed() for _, w in opened), return_exceptions=True)

    asyncio.run(scenario())


def test_body_reader_budget_released_after_rejection():
    async def scenario():
        async with running(Harness().service()) as (app, client, server):
            release = asyncio.Event()

            async def receive():
                await release.wait()
                return {"type": "http.request", "body": b"invalid", "more_body": False}

            statuses = []

            async def send(message):
                if message["type"] == "http.response.start":
                    statuses.append(message["status"])

            scope = {
                "type": "http",
                "path": "/mcp",
                "method": "POST",
                "headers": [(b"authorization", f"Bearer {TOKEN}".encode()), (b"host", app.host)],
            }
            tasks = [
                asyncio.create_task(app(scope, receive, send)) for _ in range(http.BODY_READERS)
            ]
            await until(lambda: app.readers == http.BODY_READERS)
            await app(scope, receive, send)
            assert statuses == [503]
            release.set()
            await asyncio.gather(*tasks)
            assert app.readers == 0 and not app.sessions

    asyncio.run(scenario())


def test_request_deadline_and_repeated_direct_cancellation_hold_admission():
    async def scenario(deadline):
        h = Harness()
        service = h.service()
        if deadline:
            service.bindings["echo"] = replace(service.bindings["echo"], deadline_seconds=0.02)
        service.session_open = lambda _: True
        async with service.lifespan():
            task = asyncio.create_task(
                service.invoke("echo", {"hold": True}, core.CallContext("a", "1"))
            )
            await h.entered.wait()
            h.cleanup_hold.clear()
            if not deadline:
                task.cancel()
            await h.cancelled.wait()
            for _ in range(3):
                task.cancel()
                await asyncio.sleep(0)
                assert service.active is not None and not task.done()
            assert (
                await service.invoke("increment", {"number": 1}, core.CallContext("b", "1"))
            ).isError
            h.cleanup_hold.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert service.active is None
            assert not (
                await service.invoke("increment", {"number": 1}, core.CallContext("b", "1"))
            ).isError

    asyncio.run(scenario(False))
    asyncio.run(scenario(True))


@pytest.mark.skipif(
    not os.environ.get("MAF_OPENCLAW_BICEP_IMAGE"), reason="needs prepared Docker image"
)
def test_live_http_bicep_maf_cancel_and_owned_recovery(tmp_path):
    from maf_sandbox import SandboxKey, ScopePurge
    from maf_sandbox_bicep import bicep_sandbox_spec

    prototype = load("http_bicep_prototype", "server")

    async def docker(*args):
        proc = await asyncio.create_subprocess_exec(
            "docker", *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        out, err = await asyncio.wait_for(proc.communicate(), 15)
        assert proc.returncode == 0, err.decode(errors="replace")
        return out.decode()

    async def scenario():
        image = os.environ["MAF_OPENCLAW_BICEP_IMAGE"]
        config = (ROOT / "images/bicep-sandbox/prepared.bicepconfig.json").read_text()
        with prototype.ownership(tmp_path) as scope:
            backend = await prototype.DockerSandboxBackend.create(prototype.DockerSandboxConfig())
            other = "http-unrelated-" + uuid.uuid4().hex
            spec = bicep_sandbox_spec(image=image, egress=prototype.Egress.CLOSED)
            try:
                owned = await backend.acquire(
                    SandboxKey(scope, prototype.THREAD, "validator", uuid.uuid4().hex), spec
                )
                unrelated = await backend.acquire(
                    SandboxKey(other, prototype.THREAD, "validator", uuid.uuid4().hex), spec
                )
                composition = await prototype.http_application(image, config, scope, TOKEN, 8765)
                async with running(composition.service, shutdown_seconds=60) as (
                    app,
                    client,
                    server,
                ):
                    client.timeout = httpx.Timeout(240)
                    assert not (
                        await docker("ps", "-aq", "--filter", f"id={owned.instance_id}")
                    ).strip()
                    assert (
                        await docker("ps", "-q", "--filter", f"id={unrelated.instance_id}")
                    ).strip()
                    maf = MCPStreamableHTTPTool(
                        name="bicep",
                        url=str(client.base_url) + "/mcp",
                        static_headers={"Authorization": f"Bearer {TOKEN}"},
                        load_prompts=False,
                        request_timeout=240,
                    )
                    async with maf:
                        assert maf.session is not None
                        tools = await maf.session.list_tools()
                        original = await prototype.make_server(None).list_tools()
                        assert tools.tools == original
                        for source, verdict in [
                            ("output greeting string = 'hello'", "valid"),
                            ("output greeting int = 'wrong'", "invalid"),
                            (
                                "module missing 'br/public:avm/res/storage/storage-account:0.0.0' = { name: 'test' }",
                                None,
                            ),
                            (
                                "module network 'br/public:avm/res/network/virtual-network:0.7.2' = {\n name: 'network'\n params: { name: 'example-network'\n addressPrefixes: ['10.0.0.0/16'] }\n}",
                                "valid",
                            ),
                        ]:
                            response = await maf.call_tool(
                                "bicep_validate", files=[{"path": "main.bicep", "content": source}]
                            )
                            assert isinstance(response, list) and len(response) == 1
                            assert response[0].text is not None
                            answer = json.loads(response[0].text)
                            assert (
                                answer["verdict"] == verdict and answer["cleanup"] == "confirmed"
                            ), answer
                            assert answer["completed"] == (verdict is not None)
                            assert (
                                answer["source_sha256"]
                                == prototype.snapshot(
                                    {"files": [{"path": "main.bicep", "content": source}]}
                                ).digest
                            )
                    sid = await initialize(client)
                    source = "\n".join(f"var v{i} = range(0, 1000)" for i in range(1500))
                    active = asyncio.create_task(
                        call(
                            client,
                            sid,
                            "bicep_validate",
                            {"files": [{"path": "main.bicep", "content": source}]},
                        )
                    )
                    container = ""
                    async with asyncio.timeout(40):
                        while True:
                            containers = (
                                await docker(
                                    "ps", "-q", "--filter", f"label=maf-sandbox.scope={scope}"
                                )
                            ).split()
                            if containers:
                                container = containers[0]
                                if "bicep" in await docker("top", container, "-eo", "pid,comm"):
                                    break
                            assert not active.done(), (
                                "Compiler finished before cancellation could be observed"
                            )
                            await asyncio.sleep(0.05)
                    inspection = json.loads(await docker("inspect", container))[0]
                    assert inspection["HostConfig"]["NetworkMode"] == "none"
                    assert inspection["HostConfig"]["Memory"] == 1024**3
                    await cancel(client, sid)
                    await active
                    async with asyncio.timeout(45):
                        while app.service.active is not None:
                            await asyncio.sleep(0.05)
                    assert not (await docker("ps", "-aq", "--filter", f"id={container}")).strip()
                    assert (
                        await docker("ps", "-q", "--filter", f"id={unrelated.instance_id}")
                    ).strip()
            finally:
                reports = await asyncio.gather(
                    backend.dispose_scope(scope, prototype.THREAD),
                    backend.dispose_scope(other, prototype.THREAD),
                    return_exceptions=True,
                )
                assert all(
                    isinstance(report, ScopePurge) and report.undisposed is None
                    for report in reports
                )

    asyncio.run(scenario())


@pytest.mark.skipif(
    not os.environ.get("MAF_OPENCLAW_BICEP_IMAGE"), reason="needs prepared Docker image"
)
def test_live_published_dependencies_http_smoke(tmp_path):
    import subprocess
    import textwrap

    block = (
        (SAMPLE / "server.py")
        .read_text(encoding="utf-8")
        .split("# /// script", 1)[1]
        .split("# ///", 1)[0]
    )
    script = tmp_path / "published_http_probe.py"
    code = """
import asyncio
import importlib.metadata
import json
import socket
import sys
from pathlib import Path
sys.path.insert(0, SAMPLE)
from server import ownership, http_application
from workload_http import server
from agent_framework import MCPStreamableHTTPTool

async def main():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    with ownership(Path(OWNER)) as scope:
        app = await http_application(IMAGE, Path(CONFIG).read_text(), scope, TOKEN, port)
        host = server(app)
        task = asyncio.create_task(host.serve(sockets=[sock]))
        try:
            async with asyncio.timeout(30):
                while not host.started:
                    if task.done():
                        task.result()
                        raise RuntimeError("Startup failed")
                    await asyncio.sleep(0.02)
            clients = [MCPStreamableHTTPTool(name=name, url=f"http://127.0.0.1:{port}/mcp", static_headers={"Authorization": f"Bearer {TOKEN}"}, load_prompts=False, request_timeout=240) for name in ("first", "second")]
            async with clients[0], clients[1]:
                for client in clients:
                    result = await client.call_tool("bicep_validate", files=[{"path": "main.bicep", "content": "output greeting string = 'hello'"}])
                    assert len(result) == 1
                    answer = json.loads(result[0].text)
                    assert answer["verdict"] == "valid" and answer["cleanup"] == "confirmed"
            print(json.dumps({"passed": True, "versions": {p: importlib.metadata.version(p) for p in ("maf-sandbox", "maf-sandbox-bicep", "maf-sandbox-docker", "mcp", "agent-framework-core", "uvicorn")}}))
        finally:
            host.should_exit = True
            await task
            sock.close()
asyncio.run(main())
"""
    settings = {
        "SAMPLE": str(SAMPLE),
        "OWNER": str(tmp_path / "owner"),
        "IMAGE": os.environ["MAF_OPENCLAW_BICEP_IMAGE"],
        "CONFIG": str(ROOT / "images/bicep-sandbox/prepared.bicepconfig.json"),
        "TOKEN": TOKEN,
    }
    assignments = "\n".join(f"{key} = {value!r}" for key, value in settings.items())
    script.write_text(
        "# /// script" + block + "# ///\n" + assignments + "\n" + textwrap.dedent(code),
        encoding="utf-8",
    )
    result = subprocess.run(
        ["uv", "run", "--script", str(script)],
        capture_output=True,
        text=True,
        timeout=240,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout.strip().splitlines()[-1])
    assert report["passed"]
    assert report["versions"]["maf-sandbox"] == "0.46.0"
    assert report["versions"]["mcp"] == "1.26.0"
    print(json.dumps(report))


def test_initialization_reservations_are_atomic_and_count_toward_cap(monkeypatch):
    async def scenario():
        async with running(Harness().service()) as (app, client, server):
            release = asyncio.Event()
            original = app._start

            async def held(sid, record):
                await release.wait()
                await original(sid, record)

            monkeypatch.setattr(app, "_start", held)
            body = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": "parallel", "version": "1"},
                },
            }
            tasks = [asyncio.create_task(client.post("/mcp", json=body)) for _ in range(9)]
            await until(lambda: len(app.sessions) == 8 and any(t.done() for t in tasks))
            assert sum(t.done() for t in tasks) == 1
            assert next(t.result().status_code for t in tasks if t.done()) == 503
            release.set()
            responses = await asyncio.gather(*tasks)
            assert sum(r.status_code == 200 for r in responses) == 8
            assert len(app.sessions) == 8

    asyncio.run(scenario())


def test_idle_expiry_does_not_evict_active_call_and_restart_rejects_old_id():
    async def scenario():
        h = Harness()
        async with running(h.service()) as (app, client, server):
            app.idle_seconds = 0.03
            sid = await initialize(client)
            active = asyncio.create_task(call(client, sid, args={"hold": True}))
            await h.entered.wait()
            await asyncio.sleep(0.1)
            assert sid in app.sessions and not h.cancelled.is_set()
            h.hold.set()
            assert (await active).status_code == 200
        async with running(h.service()) as (app, client, server):
            assert (await call(client, sid)).status_code == 404
            assert not (await call(client, await initialize(client))).json()["result"]["isError"]

    asyncio.run(scenario())


def test_incomplete_headers_have_a_real_connection_deadline(monkeypatch):
    async def scenario():
        async with running(Harness().service()) as (app, client, server):
            monkeypatch.setattr(http, "BODY_SECONDS", 0.05)
            reader, writer = await asyncio.open_connection("127.0.0.1", app.port)
            try:
                writer.write(b"POST /mcp HTTP/1.1\r\nHost: ")
                await writer.drain()
                assert await asyncio.wait_for(reader.read(), 1) == b""
                assert not app.sessions
            finally:
                writer.close()
                await writer.wait_closed()

    asyncio.run(scenario())


def test_delete_can_retire_session_before_initialized_notification():
    async def scenario():
        async with running(Harness().service()) as (app, client, server):
            response = await client.post(
                "/mcp",
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-11-25",
                        "capabilities": {},
                        "clientInfo": {"name": "incomplete-client", "version": "1"},
                    },
                },
            )
            sid = response.headers["mcp-session-id"]
            assert (await client.delete("/mcp", headers={"mcp-session-id": sid})).status_code == 200
            await until(lambda: not app.sessions)

    asyncio.run(scenario())
