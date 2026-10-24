"""Installed worker and external Gateway caller used only by the owner-loss profile."""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import hashlib
import http.client
import importlib
import json
import os
import time
from pathlib import Path
from typing import Any


def record(path: Path, event: str, **fields: Any) -> None:
    with path.open("a", encoding="utf-8") as stream:
        stream.write(
            json.dumps(
                {
                    "event": event,
                    "mono_ns": time.monotonic_ns(),
                    "time_ns": time.time_ns(),
                    **fields,
                },
                allow_nan=False,
            )
            + "\n"
        )


async def worker(root: Path) -> None:
    server = importlib.import_module("maf_sandbox_mcp.server")
    workload_http = importlib.import_module("maf_sandbox_mcp.workload_http")
    strict_json = importlib.import_module("openclaw_gateway_http_check").strict_json
    workload_result = importlib.import_module("openclaw_gateway_http_check").workload_result
    ObserveHTTP = importlib.import_module("openclaw_http_observer").ObserveHTTP
    digest = importlib.import_module("openclaw_http_observer").digest

    settings = strict_json((root / "worker.json").read_bytes())
    events = root / "transport.jsonl"
    record(root / "worker-pid.jsonl", "actor_started", pid=os.getpid())

    class Observer(ObserveHTTP):
        def record(self, **fields: Any) -> None:
            record(self.evidence, fields.pop("event"), boot=self.boot, **fields)

    with server.ownership(root / "owner") as owner:
        app = await server.http_application(
            settings["image"], settings["config"], owner, settings["token"], settings["port"]
        )
        observer = Observer(app, events, owned_scope=owner)
        observer.record(event="owner_locked", owner=digest(owner))
        binding = app.service.bindings["bicep_validate"]
        target_identity = {}

        async def execute(arguments, context):
            fields = {
                "session": digest(context.session_id.encode().hex()),
                "request": digest(context.request_id),
            }
            observer.record(
                event="binding_started", source_sha256=server.snapshot(arguments).digest, **fields
            )
            try:
                result = await binding.execute(arguments, context)
                observer.record(
                    event="binding_result",
                    result=result.model_dump(mode="json", by_alias=True),
                    **fields,
                )
                return result
            except BaseException as exc:
                observer.record(event="binding_exception", error=type(exc).__name__, **fields)
                raise

        app.service.bindings["bicep_validate"] = dataclasses.replace(binding, execute=execute)

        async def barrier(phase: str, **fields):
            observer.record(event="barrier", phase=phase, **target_identity, **fields)
            try:
                async with asyncio.timeout(90):
                    while not (root / "release").exists():
                        await asyncio.sleep(0.02)
                observer.record(event="barrier_released", phase=phase)
            finally:
                observer.record(event="barrier_ended", phase=phase, **target_identity)

        original = server.DockerSandboxBackend._invoke
        cleanup_held = False

        async def invoke(backend, *args, **kwargs):
            nonlocal cleanup_held
            if len(args) == 3 and args[:2] == ("rm", "-f"):
                # The backend supplies this target; the fault cannot accept a caller-selected ID.
                inspected = await original(backend, "inspect", args[2])
                if inspected.returncode == 0:
                    info = strict_json(inspected.stdout)[0]
                    if info["Config"]["Labels"].get("maf-sandbox.scope") == owner:
                        if (
                            (root / "arm").exists()
                            and settings["phase"] == "cleanup"
                            and not cleanup_held
                        ):
                            cleanup_held = True
                            await barrier("cleanup", container=info["Id"], target=args[2])
                        result = await original(backend, *args, **kwargs)
                        observer.record(
                            event="docker_removal",
                            container=info["Id"],
                            target=args[2],
                            returncode=result.returncode,
                            stderr_sha256=hashlib.sha256(result.stderr.encode()).hexdigest(),
                        )
                        return result
            return await original(backend, *args, **kwargs)

        server.DockerSandboxBackend._invoke = invoke

        class Gate:
            async def __call__(self, scope, receive, send):
                if scope["type"] != "http":
                    return await observer(scope, receive, send)
                body = bytearray()
                armed = False
                response = []
                request_id = None

                async def read():
                    nonlocal armed, request_id
                    message = await receive()
                    if message["type"] == "http.request":
                        body.extend(message.get("body", b""))
                        if len(body) > 2 * 1024 * 1024:
                            raise RuntimeError("fault_request_limit")
                        if not message.get("more_body", False) and body:
                            value = strict_json(body)
                            if value.get("method") == "tools/call" and (root / "arm").exists():
                                armed = True
                                request_id = value.get("id")
                                (root / "arm").unlink()
                                target_identity.update(
                                    request=digest(request_id),
                                    session=digest(
                                        dict(scope.get("headers", []))
                                        .get(b"mcp-session-id", b"")
                                        .hex()
                                    ),
                                )
                                observer.record(event="target_request", **target_identity)
                                if settings["phase"] == "pre_admission":
                                    await barrier("pre_admission")
                                if settings["phase"] == "cleanup":
                                    (root / "arm").touch()
                    return message

                async def write(message):
                    if armed and settings["phase"] == "response":
                        if message["type"] == "http.response.start" and message["status"] != 200:
                            raise RuntimeError("response_barrier_requires_http_success")
                        response.append(message)
                        if sum(len(part.get("body", b"")) for part in response) > 2 * 1024 * 1024:
                            raise RuntimeError("fault_response_limit")
                        if message["type"] == "http.response.body" and not message.get(
                            "more_body", False
                        ):
                            rpc_result = importlib.import_module(
                                "openclaw_gateway_http_check"
                            ).rpc_result

                            raw = b"".join(part.get("body", b"") for part in response)
                            value = workload_result(rpc_result(raw, request_id))
                            if value["completed"] is not True or value["cleanup"] != "confirmed":
                                raise RuntimeError("response_barrier_requires_completion")
                            await barrier(
                                "response",
                                result=value,
                                body_sha256=hashlib.sha256(raw).hexdigest(),
                            )
                            for part in response:
                                await send(part)
                        return
                    await send(message)

                await observer(scope, read, write)

        host = workload_http.server(app)
        host.config.app = Gate()
        await host.serve()
        observer.record(event="shutdown", ready=app.service.ready, poisoned=app.service.poisoned)
        if not host.started or app.service.poisoned:
            raise RuntimeError("worker_not_ready_or_cleanup_unconfirmed")


def client(root: Path) -> None:
    read_response = importlib.import_module("openclaw_gateway_http_check").read_response
    strict_json = importlib.import_module("openclaw_gateway_http_check").strict_json

    settings = strict_json((root / "client.json").read_bytes())
    target = root / "client.jsonl"
    record(target, "actor_started", pid=os.getpid())
    connection = http.client.HTTPConnection("127.0.0.1", settings["port"], timeout=270)
    try:
        connection.request(
            "POST",
            "/v1/chat/completions",
            json.dumps(settings["body"]),
            {"Authorization": "Bearer " + settings["token"], "Content-Type": "application/json"},
        )
        record(target, "request_sent", turn=settings["turn"])
        response = connection.getresponse()
        raw = read_response(response)
        record(
            target,
            "client_outcome",
            status=response.status,
            body_sha256=hashlib.sha256(raw).hexdigest(),
            error=None,
        )
    except Exception as exc:
        record(target, "client_outcome", status=None, body_sha256=None, error=type(exc).__name__)
        raise
    finally:
        connection.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("role", choices=("worker", "client"))
    parser.add_argument("root", type=Path)
    args = parser.parse_args()
    if args.role == "worker":
        asyncio.run(worker(args.root))
    else:
        client(args.root)
