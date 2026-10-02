"""Boundaries and lifecycle of the experimental OpenClaw Bicep service."""

from __future__ import annotations

import asyncio
import importlib.util
import io
import json
import os
import subprocess
import sys
import time
import uuid
from functools import partial
from pathlib import Path
from types import SimpleNamespace

import anyio
import jsonschema
import pytest
from agent_framework import Content, MCPStdioTool
from maf_sandbox import DisposalFailure, Isolation, SandboxKey, SandboxSpec, ScopePurge
from maf_sandbox.testing import InProcessSandboxBackend
from mcp import ClientSession, StdioServerParameters, types
from mcp.client.stdio import stdio_client
from mcp.server.fastmcp import FastMCP
from mcp.shared.exceptions import McpError
from mcp.shared.memory import create_connected_server_and_client_session

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "samples/experimental/openclaw_bicep/server.py"
SPEC = importlib.util.spec_from_file_location("openclaw_bicep_prototype", SCRIPT)
assert SPEC and SPEC.loader
prototype = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = prototype
SPEC.loader.exec_module(prototype)
IMAGE = "sha256:" + "a" * 64
CONFIG = (ROOT / "images/bicep-sandbox/prepared.bicepconfig.json").read_text()


def arguments(content="output greeting string = 'hello'", name="main.bicep"):
    return {"files": [{"path": name, "content": content}]}


def result(completed=True, verdict="valid", diagnostic="compiler output"):
    items = [
        Content.from_text(prototype.COMPLETED_TEXT if completed else prototype.NOT_COMPLETED_TEXT)
    ]
    if completed:
        items.append(Content.from_text("Result: " + verdict))
    items.append(
        Content.from_text(
            diagnostic, additional_properties={"security_label": {"integrity": "untrusted"}}
        )
    )
    return items


def response_text(response: types.CallToolResult) -> str:
    assert len(response.content) == 1
    item = response.content[0]
    assert isinstance(item, types.TextContent)
    return item.text


@pytest.mark.parametrize(
    "name",
    [
        "../a.bicep",
        "/a.bicep",
        "C:/a.bicep",
        "a\\b.bicep",
        "a//b.bicep",
        "a/./b.bicep",
        "a/../b.bicep",
        "NUL.bicep",
        "a/COM1.bicep",
        "a.json",
        "bicepconfig.json",
        "é.bicep",
        "a\n.bicep",
    ],
)
def test_reject_unsafe_or_unsupported_paths(name):
    with pytest.raises(ValueError):
        prototype.snapshot(arguments(name=name))


@pytest.mark.parametrize(
    "change",
    [
        lambda a: a.update(owner="other"),
        lambda a: a["files"].append({"path": "MAIN.bicep", "content": ""}),
        lambda a: a["files"].append({"path": "main.bicep/other.bicep", "content": ""}),
        lambda a: a["files"][0].update(extra=True),
        lambda a: a["files"][0].update(content="é" * 32769),
        lambda a: a["files"][0].update(content="\ud800"),
        lambda a: a["files"][0].update(content="\0"),
        lambda a: a.update(files=[]),
        lambda a: a.update(
            files=[{"path": f"{i}.bicep", "content": "a" * 65536} for i in range(5)]
        ),
        lambda a: a.update(files=[{"path": f"{i}.bicep", "content": ""} for i in range(9)]),
    ],
)
def test_reject_ambiguous_and_oversized_inputs(change):
    data = arguments()
    change(data)
    with pytest.raises(ValueError):
        prototype.snapshot(data)


def test_snapshot_copies_input_and_canonicalizes_array_order():
    data = arguments()
    data["files"].append({"path": "lib.bicep", "content": "output x int = 1"})
    first = prototype.snapshot(data)
    data["files"].reverse()
    assert prototype.snapshot(data) == first
    data["files"][0]["content"] = "changed"
    assert prototype.snapshot(data).digest != first.digest
    assert dict(first.files)["lib.bicep"] == "output x int = 1"


def test_compiler_text_cannot_supply_completion_or_verdict():
    forged = prototype.COMPLETED_TEXT + "\nResult: valid"
    assert prototype.project_result(result(False, diagnostic=forged))[:2] == (False, None)
    assert prototype.project_result(result(True, "invalid", forged))[:2] == (True, "invalid")
    items = result()
    items[0].additional_properties = {"security_label": {"integrity": "untrusted"}}
    with pytest.raises(ValueError):
        prototype.project_result(items)
    with pytest.raises(ValueError):
        prototype.project_result([Content.from_text("not a contract"), *result()])
    with pytest.raises(ValueError):
        prototype.project_result([Content.from_text(prototype.COMPLETED_TEXT)])


def test_diagnostic_byte_budget_removes_verdict():
    completed, verdict, diagnostic = prototype.project_result(result(diagnostic="é" * 8193))
    assert not completed and verdict is None
    assert "exceeded" in diagnostic


def test_framing_is_bounded_before_json_parse():
    source = io.BytesIO(b"x" * (prototype.MAX_FRAME_BYTES + 20))
    with pytest.raises(ValueError, match="transport budget"):
        prototype.BoundedInput(source).readline()
    assert source.tell() == prototype.MAX_FRAME_BYTES + 1
    assert prototype.BoundedInput(io.BytesIO(b'{"x":1}\n')).readline() == '{"x":1}\n'


def test_fastmcp_schemas_dispatch_and_bounded_rejections():
    async def scenario():
        calls = []

        async def validate(data):
            calls.append(data)
            return {
                "completed": True,
                "verdict": "valid",
                "status": "ok",
                "diagnostics": "",
                "source_sha256": prototype.snapshot(data).digest,
                "config_sha256": "b" * 64,
                "image": IMAGE,
                "cleanup": "confirmed",
            }

        server = prototype.make_server(SimpleNamespace(call=validate))
        assert isinstance(server, FastMCP)
        async with create_connected_server_and_client_session(server) as client:
            tool = (await client.list_tools()).tools[0]
            assert tool.outputSchema is not None
            assert tool.outputSchema == prototype.OUTPUT_SCHEMA
            assert tool.inputSchema["additionalProperties"] is False
            jsonschema.validate(arguments(), tool.inputSchema)
            response = await client.call_tool(prototype.TOOL, arguments())
            assert not response.isError
            assert response.structuredContent is not None
            assert response.structuredContent["verdict"] == "valid"
            jsonschema.validate(response.structuredContent, tool.outputSchema)
            assert json.loads(response_text(response)) == response.structuredContent
            assert calls == [arguments()]

            marker = "source-must-not-appear"
            rejected = [
                {**arguments(), "owner": marker},
                {"files": json.dumps(arguments()["files"])},
                {"files": [{"path": "main.bicep", "content": 123}]},
                {"files": [{"path": "main.bicep", "content": marker, "extra": True}]},
                arguments(marker, name="../escape.bicep"),
                arguments(marker * prototype.MAX_FILE_BYTES),
                {"files": []},
            ]
            for data in rejected:
                if data != rejected[4]:
                    with pytest.raises(jsonschema.ValidationError):
                        jsonschema.validate(data, tool.inputSchema)
                response = await client.call_tool(prototype.TOOL, data)
                assert response.isError and response.structuredContent is None
                assert len(response_text(response)) < 256
                assert marker not in response_text(response)
            unknown = await client.call_tool(marker, arguments())
            assert unknown.isError and marker not in response_text(unknown)
            assert calls == [arguments()]
            assert not (await client.list_resources()).resources
            assert not (await client.list_prompts()).prompts

    asyncio.run(scenario())


def test_fastmcp_stdio_rejects_oversized_frame_before_parsing(monkeypatch):
    async def scenario():
        source = io.BytesIO(b"x" * (prototype.MAX_FRAME_BYTES + 20))
        monkeypatch.setattr(sys, "stdin", SimpleNamespace(buffer=source))
        monkeypatch.setattr(
            prototype,
            "stdio_server",
            partial(prototype.stdio_server, stdout=anyio.wrap_file(io.StringIO())),
        )
        server = prototype.make_server(SimpleNamespace())
        with pytest.raises(ExceptionGroup) as rejected:
            await server.run_stdio_async()
        assert "MCP frame exceeds the transport budget" in repr(rejected.value)
        assert source.tell() == prototype.MAX_FRAME_BYTES + 1

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["busy", "schema", "cleanup_failed"])
def test_fastmcp_preserves_error_semantics_without_echoing_results(failure):
    async def scenario():
        async def validate(data):
            if failure == "busy":
                raise prototype.CallRefused("Validator is busy.")
            return {
                "completed": False,
                "verdict": None,
                "status": failure,
                "diagnostics": "private-source-marker",
                "source_sha256": prototype.snapshot(data).digest,
                "config_sha256": "b" * 64,
                "image": IMAGE,
                "cleanup": "failed",
            }

        server = prototype.make_server(SimpleNamespace(call=validate))
        async with create_connected_server_and_client_session(server) as client:
            response = await client.call_tool(prototype.TOOL, arguments())
            assert response.isError
            if failure == "cleanup_failed":
                assert response.structuredContent is not None
                assert response.structuredContent["status"] == "cleanup_failed"
                assert response.structuredContent["verdict"] is None
            else:
                assert response.structuredContent is None
                assert "private-source-marker" not in response_text(response)
                assert len(response_text(response)) < 256
            if failure == "busy":
                assert "busy" in response_text(response)

    asyncio.run(scenario())


def test_owner_is_stable_exclusive_and_corruption_fails_closed(tmp_path):
    with prototype.ownership(tmp_path) as first:
        with pytest.raises(OSError), prototype.ownership(tmp_path):
            pass
    with prototype.ownership(tmp_path) as second:
        assert second == first
    (tmp_path / "owner").write_text("corrupt")
    with pytest.raises(ValueError, match="operator reconciliation"), prototype.ownership(tmp_path):
        pass
    (tmp_path / "owner").unlink()
    with pytest.raises(ValueError, match="Missing owner"), prototype.ownership(tmp_path):
        pass


def test_reported_purge_failure_also_poisons_admission():
    async def scenario():
        backend = InProcessSandboxBackend(
            isolation=Isolation.CONTAINER,
            purge_failure=DisposalFailure("unknown", "daemon failure"),
        )
        validator = prototype.Validator(backend, "owner", IMAGE, CONFIG)
        assert not await validator.recover()
        assert validator.poisoned

    asyncio.run(scenario())


def test_call_scope_policy_and_snapshot_wiring(monkeypatch):
    async def scenario():
        backend = InProcessSandboxBackend(isolation=Isolation.CONTAINER)
        validator = prototype.Validator(backend, "test-owner", IMAGE, CONFIG)
        seen = {}

        def factory(router, store, agent_id, context, **kwargs):
            seen.update(kwargs)
            assert (
                router.effective_isolation_scope(SandboxSpec(kind="bicep"))
                == prototype.IsolationScope.CALL
            )

            async def execute(files):
                assert await store.read(files[0]) == "output greeting string = 'hello'"
                return result()

            return [SimpleNamespace(func=execute)]

        monkeypatch.setattr(prototype, "make_bicep_tools", factory)
        answer = await validator.call(arguments())
        assert answer["completed"] and answer["verdict"] == "valid"
        assert answer["cleanup"] == "confirmed"
        assert seen["egress"] == prototype.Egress.CLOSED
        assert seen["exec_timeout_seconds"] == prototype.PHASE_SECONDS
        jsonschema.validate(answer, prototype.OUTPUT_SCHEMA)

    asyncio.run(scenario())


def test_cleanup_failure_suppresses_verdict_and_poisons_admission(monkeypatch):
    async def scenario():
        backend = InProcessSandboxBackend(isolation=Isolation.CONTAINER)
        validator = prototype.Validator(backend, "test-owner", IMAGE, CONFIG)

        async def execute(**kwargs):
            return result()

        async def fail(*args):
            raise RuntimeError("daemon unavailable")

        monkeypatch.setattr(
            prototype, "make_bicep_tools", lambda *a, **k: [SimpleNamespace(func=execute)]
        )
        monkeypatch.setattr(backend, "dispose_scope", fail)
        answer = await validator.call(arguments())
        assert answer["status"] == "cleanup_failed"
        assert not answer["completed"] and answer["verdict"] is None
        with pytest.raises(ValueError, match="recovery"):
            await validator.call(arguments())

    asyncio.run(scenario())


@pytest.mark.parametrize("timeout", [False, True])
def test_cancel_or_deadline_drains_before_cleanup_and_refuses_overlap(monkeypatch, timeout):
    async def scenario():
        started, cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        order = []
        backend = InProcessSandboxBackend(isolation=Isolation.CONTAINER)
        validator = prototype.Validator(backend, "test-owner", IMAGE, CONFIG)

        async def execute(**kwargs):
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                await release.wait()
                order.append("settled")
                raise

        async def purge(scope, thread):
            assert (scope, thread) == ("test-owner", prototype.THREAD)
            order.append("purged")
            return ScopePurge()

        monkeypatch.setattr(
            prototype, "make_bicep_tools", lambda *a, **k: [SimpleNamespace(func=execute)]
        )
        monkeypatch.setattr(backend, "dispose_scope", purge)
        if timeout:
            monkeypatch.setattr(prototype, "REQUEST_SECONDS", 0.02)
        task = asyncio.create_task(validator.call(arguments()))
        await started.wait()
        if not timeout:
            task.cancel()
        await cancelled.wait()
        with pytest.raises(ValueError, match="busy"):
            await validator.call(arguments())
        assert not task.done() and order == []
        release.set()
        if timeout:
            answer = await task
            assert answer["status"] == "timeout" and answer["verdict"] is None
        else:
            with pytest.raises(asyncio.CancelledError):
                await task
        assert order == ["settled", "purged"] and validator.active is None

    asyncio.run(scenario())


def test_anyio_level_cancellation_waits_for_cleanup(monkeypatch):
    async def scenario():
        started, released, cleaned = asyncio.Event(), asyncio.Event(), asyncio.Event()
        validator = prototype.Validator(
            InProcessSandboxBackend(isolation=Isolation.CONTAINER), "owner", IMAGE, CONFIG
        )

        async def execute(**kwargs):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                await released.wait()

        async def purge(*args):
            cleaned.set()
            return ScopePurge()

        monkeypatch.setattr(
            prototype, "make_bicep_tools", lambda *a, **k: [SimpleNamespace(func=execute)]
        )
        monkeypatch.setattr(validator.backend, "dispose_scope", purge)
        with anyio.CancelScope() as cancellation:
            async with anyio.create_task_group() as group:
                group.start_soon(validator.call, arguments())
                await started.wait()
                cancellation.cancel()
                released.set()
        assert cleaned.is_set() and validator.active is None

    asyncio.run(scenario())


@pytest.mark.parametrize("timeout", [False, True])
@pytest.mark.parametrize("failure", [False, True])
def test_cancel_during_purge_waits_and_never_claims_failed_cleanup(monkeypatch, timeout, failure):
    async def scenario():
        cleaning, release = asyncio.Event(), asyncio.Event()
        validator = prototype.Validator(
            InProcessSandboxBackend(isolation=Isolation.CONTAINER), "owner", IMAGE, CONFIG
        )

        async def execute(**kwargs):
            return result()

        async def purge(*args):
            cleaning.set()
            await release.wait()
            return ScopePurge(undisposed=DisposalFailure("unknown", "failed") if failure else None)

        monkeypatch.setattr(
            prototype, "make_bicep_tools", lambda *a, **k: [SimpleNamespace(func=execute)]
        )
        monkeypatch.setattr(validator.backend, "dispose_scope", purge)
        if timeout:
            monkeypatch.setattr(prototype, "REQUEST_SECONDS", 0.01)
        task = asyncio.create_task(validator.call(arguments()))
        await cleaning.wait()
        if not timeout:
            task.cancel()
        else:
            await asyncio.sleep(0.03)
        await asyncio.sleep(0)
        assert not task.done()
        with pytest.raises(ValueError, match="busy"):
            await validator.call(arguments())
        release.set()
        if timeout:
            answer = await task
            assert answer["cleanup"] == ("failed" if failure else "confirmed")
            assert answer["status"] == ("cleanup_failed" if failure else "timeout")
        else:
            with pytest.raises(asyncio.CancelledError):
                await task
        assert validator.poisoned == failure

    asyncio.run(scenario())


@pytest.mark.parametrize("stage", ["worker", "cleanup"])
@pytest.mark.parametrize("timeout", [False, True])
@pytest.mark.parametrize("failure", [False, True])
def test_repeated_request_cancellation_retains_admission(monkeypatch, stage, timeout, failure):
    async def scenario():
        started, draining, cleaning, release = (asyncio.Event() for _ in range(4))
        order = []
        validator = prototype.Validator(
            InProcessSandboxBackend(isolation=Isolation.CONTAINER), "owner", IMAGE, CONFIG
        )

        async def execute(**kwargs):
            started.set()
            if stage == "worker":
                try:
                    await asyncio.Event().wait()
                finally:
                    draining.set()
                    await release.wait()
                    order.append("worker settled")
            return result()

        async def purge(*args):
            cleaning.set()
            if stage == "cleanup":
                await release.wait()
            order.append("cleanup settled")
            return ScopePurge(undisposed=DisposalFailure("unknown", "failed") if failure else None)

        monkeypatch.setattr(
            prototype, "make_bicep_tools", lambda *a, **k: [SimpleNamespace(func=execute)]
        )
        monkeypatch.setattr(validator.backend, "dispose_scope", purge)
        if timeout:
            monkeypatch.setattr(prototype, "REQUEST_SECONDS", 0.01)
        request = asyncio.create_task(validator.call(arguments()))
        try:
            await (started if stage == "worker" else cleaning).wait()
            if not timeout:
                request.cancel()
            if stage == "worker":
                await draining.wait()
            else:
                await asyncio.sleep(0.03)
            worker = validator.active
            for _ in range(3):
                request.cancel()
                for _ in range(5):
                    await asyncio.sleep(0)
                assert not request.done() and validator.active is worker
                assert order == []
                with pytest.raises(ValueError, match="busy"):
                    await validator.call(arguments())
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await request
            assert order == (
                ["worker settled", "cleanup settled"] if stage == "worker" else ["cleanup settled"]
            )
            assert validator.active is None and validator.poisoned == failure
        finally:
            release.set()
            await asyncio.gather(request, return_exceptions=True)

    asyncio.run(scenario())


@pytest.mark.parametrize("target", ["worker", "shutdown"])
def test_repeated_cancellation_drains_cleanup_and_holds_owner_lock(monkeypatch, tmp_path, target):
    async def scenario():
        cleaning, release, locked = (asyncio.Event() for _ in range(3))
        cleaned = False
        validator = prototype.Validator(
            InProcessSandboxBackend(isolation=Isolation.CONTAINER), "owner", IMAGE, CONFIG
        )

        async def execute(**kwargs):
            return result()

        async def purge(*args):
            nonlocal cleaned
            cleaning.set()
            await release.wait()
            cleaned = True
            return ScopePurge()

        async def shutdown():
            with prototype.ownership(tmp_path):
                locked.set()
                await validator.close()

        monkeypatch.setattr(
            prototype, "make_bicep_tools", lambda *a, **k: [SimpleNamespace(func=execute)]
        )
        monkeypatch.setattr(validator.backend, "dispose_scope", purge)
        request = asyncio.create_task(validator.call(arguments()))
        await cleaning.wait()
        worker = validator.active
        assert worker is not None
        cancelled = worker if target == "worker" else asyncio.create_task(shutdown())
        try:
            if target == "shutdown":
                await locked.wait()
            for _ in range(3):
                cancelled.cancel()
                for _ in range(5):
                    await asyncio.sleep(0)
                assert not cancelled.done() and validator.active is worker and not cleaned
                with pytest.raises(ValueError, match="busy"):
                    await validator.call(arguments())
                if target == "shutdown":
                    with pytest.raises(OSError), prototype.ownership(tmp_path):
                        pass
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await cancelled
            with pytest.raises(asyncio.CancelledError):
                await request
            assert cleaned and validator.active is None
            with prototype.ownership(tmp_path):
                pass
        finally:
            release.set()
            await asyncio.gather(request, cancelled, return_exceptions=True)

    asyncio.run(scenario())


@pytest.mark.skipif(
    not os.environ.get("MAF_OPENCLAW_BICEP_IMAGE"), reason="needs prepared Docker image"
)
def test_live_stdio_compiler_outcomes_and_cleanup(tmp_path):
    async def scenario():
        image = os.environ["MAF_OPENCLAW_BICEP_IMAGE"]
        client = MCPStdioTool(
            name="bicep",
            command=sys.executable,
            load_prompts=False,
            request_timeout=240,
            args=[
                str(SCRIPT),
                "--image",
                image,
                "--config",
                str(ROOT / "images/bicep-sandbox/prepared.bicepconfig.json"),
                "--state-dir",
                str(tmp_path),
            ],
        )
        async with client:
            session = client.session
            assert session is not None
            tools = await session.list_tools()
            assert [t.name for t in tools.tools] == [prototype.TOOL]
            assert tools.tools[0].outputSchema == prototype.OUTPUT_SCHEMA
            for source, expected in [
                ("output greeting string = 'hello'", "valid"),
                ("output greeting int = 'wrong'", "invalid"),
                (
                    "module absent 'br/public:avm/res/storage/storage-account:0.0.0' = { name: 'test' }",
                    None,
                ),
                (
                    "module network 'br/public:avm/res/network/virtual-network:0.7.2' = {\n name: 'network'\n params: { name: 'example-network'\n addressPrefixes: ['10.0.0.0/16'] }\n}",
                    "valid",
                ),
            ]:
                response = await client.call_tool(prototype.TOOL, **arguments(source))
                assert isinstance(response, list) and len(response) == 1, response
                assert response[0].text is not None
                answer = json.loads(response[0].text)
                jsonschema.validate(answer, prototype.OUTPUT_SCHEMA)
                assert answer["verdict"] == expected, answer
                assert answer["completed"] == (expected is not None), answer
                assert answer["cleanup"] == "confirmed", answer
            rejected = await session.call_tool(prototype.TOOL, arguments(name="../escape.bicep"))
            assert rejected.isError
        scope = "openclaw-bicep-" + (tmp_path / "owner").read_text()
        containers = subprocess.run(
            ["docker", "ps", "-aq", "--filter", f"label=maf-sandbox.scope={scope}"],
            capture_output=True,
            check=True,
            timeout=10,
        )
        assert not containers.stdout.strip()

    asyncio.run(scenario())


@pytest.mark.skipif(
    not os.environ.get("MAF_OPENCLAW_BICEP_IMAGE"), reason="needs prepared Docker image"
)
def test_live_cancel_active_compiler_and_recover_only_owned_scope(tmp_path):
    async def docker(*args):
        proc = await asyncio.create_subprocess_exec(
            "docker", *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        out, err = await asyncio.wait_for(proc.communicate(), timeout=10)
        assert proc.returncode == 0, err.decode(errors="replace")
        return out.decode()

    async def scenario():
        image = os.environ["MAF_OPENCLAW_BICEP_IMAGE"]
        with prototype.ownership(tmp_path) as scope:
            pass
        backend = await prototype.DockerSandboxBackend.create(prototype.DockerSandboxConfig())
        other = "other-owner-" + uuid.uuid4().hex
        from maf_sandbox_bicep import bicep_sandbox_spec

        spec = bicep_sandbox_spec(image=image, egress=prototype.Egress.CLOSED)
        owned = await backend.acquire(
            SandboxKey(scope, prototype.THREAD, "validator", uuid.uuid4().hex), spec
        )
        unrelated = await backend.acquire(
            SandboxKey(other, prototype.THREAD, "validator", uuid.uuid4().hex), spec
        )
        parameters = StdioServerParameters(
            command=sys.executable,
            args=[
                str(SCRIPT),
                "--image",
                image,
                "--config",
                str(ROOT / "images/bicep-sandbox/prepared.bicepconfig.json"),
                "--state-dir",
                str(tmp_path),
            ],
        )
        try:
            async with stdio_client(parameters) as streams, ClientSession(*streams) as session:
                await session.initialize()
                assert not (
                    await docker("ps", "-aq", "--filter", f"id={owned.instance_id}")
                ).strip()
                assert (await docker("ps", "-q", "--filter", f"id={unrelated.instance_id}")).strip()
                # The pinned SDK assigns this ID to the next outgoing request.
                request_id = session._request_id
                task = asyncio.create_task(session.call_tool(prototype.TOOL, arguments()))
                deadline = time.monotonic() + 40
                container = ""
                while time.monotonic() < deadline:
                    containers = (
                        await docker("ps", "-q", "--filter", f"label=maf-sandbox.scope={scope}")
                    ).split()
                    if containers:
                        container = containers[0]
                        processes = await docker("top", container, "-eo", "pid,comm")
                        if "bicep" in processes:
                            break
                    await asyncio.sleep(0.05)
                else:
                    pytest.fail("No active Bicep compiler observed before cancellation")
                inspection = json.loads(await docker("inspect", container))[0]
                limits = inspection["HostConfig"]
                assert limits["NetworkMode"] == "none"
                assert limits["Memory"] == 1024**3 and limits["NanoCpus"] == 10**9
                assert limits["PidsLimit"] == 128 and "ALL" in limits["CapDrop"]
                assert inspection["Config"]["Labels"]["maf-sandbox.call"]
                await session.send_notification(
                    prototype.types.ClientNotification(
                        prototype.types.CancelledNotification(
                            method="notifications/cancelled",
                            params=prototype.types.CancelledNotificationParams(
                                requestId=request_id
                            ),
                        )
                    )
                )
                with pytest.raises(McpError, match="cancelled"):
                    await task
                deadline = time.monotonic() + 45
                while (await docker("ps", "-aq", "--filter", f"id={container}")).strip():
                    assert time.monotonic() < deadline, "Cancelled compiler's container survived"
                    await asyncio.sleep(0.1)
                assert (await docker("ps", "-q", "--filter", f"id={unrelated.instance_id}")).strip()
        finally:
            await backend.dispose_scope(scope, prototype.THREAD)
            await backend.dispose_scope(other, prototype.THREAD)

    asyncio.run(scenario())
