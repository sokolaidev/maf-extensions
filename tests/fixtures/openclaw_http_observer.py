"""Observe MCP control messages in a dedicated qualification service without retaining payloads."""

# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "agent-framework-core==1.19.0",
#     "anyio>=4.5,<5",
#     "maf-sandbox==0.46.0",
#     "maf-sandbox-bicep==0.22.0",
#     "maf-sandbox-docker==0.24.4",
#     "mcp==1.28.1",
#     "pydantic>=2.11,<3",
#     "jsonschema>=4.26,<5",
#     "starlette==1.7.0",
#     "uvicorn==0.54.0",
# ]
# ///

from __future__ import annotations

import hashlib
import inspect
import json
import os
import re
import subprocess
import time
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Any


def resolve_image(reference: str) -> str:
    """Resolve a local image reference before a service can execute it."""
    try:
        image = subprocess.check_output(
            ["docker", "image", "inspect", "--format", "{{.Id}}", reference],
            text=True,
            encoding="utf-8",
            stderr=subprocess.PIPE,
            timeout=15,
        ).strip()
    except subprocess.CalledProcessError as error:
        detail = (error.stderr or str(error)).strip()[:4096]
        raise RuntimeError(f"Docker image inspection failed: {detail}") from error
    if re.fullmatch(r"sha256:[0-9a-f]{64}", image) is None:
        raise RuntimeError("Docker did not return an immutable image ID")
    return image


def decode_withheld(body: bytes | bytearray) -> dict[str, Any]:
    """Reject malformed workload responses without exposing their contents."""
    identity = hashlib.sha256(body).hexdigest()
    try:
        value = json.loads(body)
    except (ValueError, UnicodeError):
        raise RuntimeError(f"Withheld response is not valid JSON (sha256:{identity})") from None
    result = value.get("result") if isinstance(value, dict) else None
    structured = result.get("structuredContent") if isinstance(result, dict) else None
    if not isinstance(structured, dict):
        raise RuntimeError(
            f"Withheld response lacks a structured workload result (sha256:{identity})"
        )
    return structured


def digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()


class ObserveHTTP:
    """Observe allowlisted transport fields; optionally withhold one result for qualification."""

    def __init__(
        self,
        app: Any,
        evidence: Path,
        drop_result: Path | None = None,
        owned_scope: str | None = None,
    ) -> None:
        self.app = app
        self.evidence = evidence
        self.drop_result = drop_result
        self.owned_scope = owned_scope
        self.boot = uuid.uuid4().hex

    def record(self, **fields: Any) -> None:
        with self.evidence.open("a", encoding="utf-8") as output:
            output.write(
                json.dumps({"time_ns": time.time_ns(), "boot": self.boot, **fields}) + "\n"
            )

    def registry_snapshot(self) -> dict[str, Any]:
        """Observe adapter ownership and pinned SDK bookkeeping without retaining session IDs."""
        return {
            "sessions": [
                {
                    "session": digest(sid.encode().hex()),
                    "initialized": record.initialized,
                    "closing": record.closing,
                    "sdk_running": record.task is not None and not record.task.done(),
                    "sdk_terminated": record.transport.is_terminated,
                    "sdk_streams": len(record.transport._request_streams),
                    "request_ids": len(record.ids),
                    "requests": record.requests,
                    "get_active": record.get_active,
                }
                for sid, record in self.app.sessions.items()
            ],
            "readers": self.app.readers,
            "active": self.app.service.active is not None,
            "poisoned": self.app.service.poisoned,
        }

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":

            async def lifetime(message):
                if message["type"] == "lifespan.startup.complete" and self.owned_scope is not None:
                    owned = subprocess.check_output(
                        [
                            "docker",
                            "ps",
                            "-aq",
                            "--filter",
                            f"label=maf-sandbox.scope={self.owned_scope}",
                        ],
                        text=True,
                        encoding="utf-8",
                        stderr=subprocess.PIPE,
                        timeout=15,
                    ).split()
                    self.record(event="startup_ready", owner_empty=not owned)
                if message["type"] == "lifespan.startup.failed":
                    self.record(
                        event="startup_failed",
                        ready=self.app.service.ready,
                        poisoned=self.app.service.poisoned,
                        active=self.app.service.active is not None,
                        sessions=len(self.app.sessions),
                    )
                await send(message)

            await self.app(scope, receive, lifetime)
            return
        headers = dict(scope.get("headers", []))
        session = headers.get(b"mcp-session-id")
        sid = digest(session.hex()) if session else None
        exchange = uuid.uuid4().hex
        body = bytearray()
        observed = False
        method = scope["method"]
        drop = False
        withheld = bytearray()
        original_status = None
        retiring_record = (
            self.app.sessions.get(session.decode("ascii", errors="replace"))
            if method == "DELETE" and session is not None
            else None
        )
        if method == "DELETE":
            self.record(event="delete_requested", exchange=exchange, session=sid)

        async def read():
            nonlocal observed, drop
            message = await receive()
            if message["type"] == "http.request" and not observed:
                body.extend(message.get("body", b""))
                if len(body) > 2 * 1024 * 1024:
                    observed = True
                    body.clear()
                elif not message.get("more_body", False):
                    observed = True
                    try:
                        value = json.loads(body)
                    except (ValueError, UnicodeError):
                        value = None
                    body.clear()
                    if isinstance(value, dict):
                        rpc = value.get("method")
                        if isinstance(rpc, str) and rpc in {
                            "initialize",
                            "notifications/initialized",
                            "tools/list",
                            "tools/call",
                            "notifications/cancelled",
                        }:
                            fields = {
                                "event": "request",
                                "method": rpc,
                                "session": sid,
                                "exchange": exchange,
                            }
                            if "id" in value:
                                fields["request"] = digest(value["id"])
                            if rpc == "notifications/cancelled":
                                params = value.get("params")
                                if (
                                    isinstance(params, dict)
                                    and isinstance(params.get("requestId"), (int, str))
                                    and not isinstance(params["requestId"], bool)
                                ):
                                    fields["target"] = digest(params["requestId"])
                            self.record(**fields)
                            if rpc == "tools/call" and self.drop_result is not None:
                                try:
                                    self.drop_result.unlink()
                                except FileNotFoundError:
                                    pass
                                else:
                                    drop = True
                                    self.record(event="fault_armed", exchange=exchange, session=sid)
            return message

        async def write(message):
            nonlocal original_status
            if message["type"] == "http.response.start":
                original_status = message["status"]
                response_session = dict(message.get("headers", [])).get(b"mcp-session-id")
                self.record(
                    event="withheld_response" if drop else "response",
                    exchange=exchange,
                    method=method,
                    status=message["status"],
                    session=digest(response_session.hex()) if response_session else sid,
                )
            if drop:
                if message["type"] == "http.response.body":
                    withheld.extend(message.get("body", b""))
                    if len(withheld) > 2 * 1024 * 1024:
                        raise RuntimeError("Qualification response exceeds fault buffer")
                    if not message.get("more_body", False):
                        result = decode_withheld(withheld)
                        self.record(
                            event="result_withheld",
                            exchange=exchange,
                            session=sid,
                            status=original_status,
                            completed=result.get("completed"),
                            cleanup=result.get("cleanup"),
                            body_sha256=hashlib.sha256(withheld).hexdigest(),
                        )
                return
            await send(message)

        try:
            await self.app(scope, read, write)
            if drop:
                # Raise outside the SDK handler so its error response is withheld too.
                raise RuntimeError("Qualification intentionally withheld the tool result")
        finally:
            if retiring_record is not None and retiring_record.retiring is not None:
                assert session is not None

                def retired(task):
                    active = self.app.service.active
                    self.record(
                        event="retired",
                        exchange=exchange,
                        session=sid,
                        completed=not task.cancelled() and task.exception() is None,
                        sdk_finished=retiring_record.task is not None
                        and retiring_record.task.done()
                        and not retiring_record.task.cancelled()
                        and retiring_record.task.exception() is None,
                        sdk_terminated=retiring_record.transport.is_terminated,
                        sdk_streams=len(retiring_record.transport._request_streams),
                        request_ids=len(retiring_record.ids),
                        requests=retiring_record.requests,
                        session_registered=session.decode("ascii", errors="replace")
                        in self.app.sessions,
                        sessions=len(self.app.sessions),
                        active=active is not None,
                        active_session=digest(active[0].session_id.encode().hex())
                        if active
                        else None,
                        poisoned=self.app.service.poisoned,
                    )

                retiring_record.retiring.add_done_callback(retired)
            if scope.get("path") == "/ready" and original_status == 200:
                self.record(
                    event="registry_snapshot", exchange=exchange, **self.registry_snapshot()
                )
            self.record(
                event="settled",
                exchange=exchange,
                method=method,
                session=sid,
                sessions=len(self.app.sessions),
                active=self.app.service.active is not None,
                poisoned=self.app.service.poisoned,
            )


def refuse_owned_cleanup(observer: ObserveHTTP, fault: Path) -> None:
    """Inject unconfirmed cleanup for one exact retained orphan in this fixture only."""
    resource = observer.app.service.resources["bicep-docker"]

    async def cleanup() -> bool:
        if not fault.exists():
            return await resource.cleanup()
        container = fault.read_text(encoding="ascii").strip()
        if re.fullmatch(r"[0-9a-f]{12}(?:[0-9a-f]{52})?", container) is None:
            raise RuntimeError("Cleanup fault requires an exact container identity")
        owned = subprocess.check_output(
            ["docker", "ps", "-aq", "--filter", f"label=maf-sandbox.scope={observer.owned_scope}"],
            text=True,
            encoding="utf-8",
            stderr=subprocess.PIPE,
            timeout=15,
        ).split()
        if owned != [container]:
            raise RuntimeError("Cleanup fault does not match the retained orphan")
        observer.record(event="cleanup_refused", resource=resource.name, container=container)
        return False

    observer.app.service.resources[resource.name] = replace(resource, cleanup=cleanup)


def refuse_completed_cleanup(observer: ObserveHTTP, fault: Path) -> None:
    """Refuse the resource check only after the real binding returned a completed result."""
    service = observer.app.service
    binding = service.bindings["bicep_validate"]
    resource = service.resources["bicep-docker"]
    completed_context = None

    async def execute(arguments, context):
        nonlocal completed_context
        observer.record(event="binding_started", session=digest(context.session_id.encode().hex()))
        result = await binding.execute(arguments, context)
        if fault.exists():
            structured = result.structuredContent or {}
            if (
                result.isError
                or structured.get("completed") is not True
                or structured.get("cleanup") != "confirmed"
                or structured.get("status") != "ok"
                or structured.get("verdict") != "valid"
            ):
                raise RuntimeError("Cleanup fault requires a completed valid binding result")
            completed_context = context
            observer.record(
                event="binding_completed",
                session=digest(context.session_id.encode().hex()),
                **{
                    key: structured[key]
                    for key in (
                        "completed",
                        "verdict",
                        "status",
                        "cleanup",
                        "source_sha256",
                        "config_sha256",
                        "image",
                    )
                },
            )
        return result

    async def cleanup() -> bool:
        if not fault.exists():
            return await resource.cleanup()
        if (
            completed_context is None
            or service.active is None
            or service.active[0] != completed_context
            or not service.ready
        ):
            raise RuntimeError("Cleanup fault requires the completed call to retain admission")
        owned = subprocess.check_output(
            ["docker", "ps", "-aq", "--filter", f"label=maf-sandbox.scope={observer.owned_scope}"],
            text=True,
            encoding="utf-8",
            stderr=subprocess.PIPE,
            timeout=15,
        ).split()
        observer.record(
            event="completed_cleanup_refused",
            session=digest(completed_context.session_id.encode().hex()),
            resource=resource.name,
            owner_empty=not owned,
        )
        return False

    service.bindings[binding.tool.name] = replace(binding, execute=execute)
    service.resources[resource.name] = replace(resource, cleanup=cleanup)


def arm_idle_expiry(observer: ObserveHTTP, *, accelerated: bool = True) -> None:
    """Observe one sweeper retirement, optionally shortening the fixture idle threshold."""
    app = observer.app
    original_retire = app.retire
    original_seconds = app.idle_seconds
    seconds = 2 if accelerated else original_seconds
    if (
        original_seconds != 900
        or len(app.sessions) != 2
        or (accelerated and app.service.active is None)
    ):
        raise RuntimeError(
            "Idle expiry requires the default policy, two sessions and active work when accelerated"
        )
    observer.record(
        event="idle_expiry_armed",
        default_seconds=original_seconds,
        seconds=seconds,
        accelerated=accelerated,
        monotonic_ns=time.monotonic_ns(),
        idle_ages={
            digest(sid.encode().hex()): time.monotonic() - record.touched
            for sid, record in app.sessions.items()
        },
    )

    def retire(sid):
        record = app.sessions[sid]
        active = app.service.active
        active_record = app.sessions[active[0].session_id] if active else None
        now = time.monotonic()
        frame = inspect.currentframe()
        from_sweeper = (
            frame is not None
            and frame.f_back is not None
            and frame.f_back.f_code is app._expire.__func__.__code__
        )
        del frame
        observer.record(
            event="idle_expiry_started",
            from_sweeper=from_sweeper,
            effective_seconds=app.idle_seconds,
            monotonic_ns=time.monotonic_ns(),
            session=digest(sid.encode().hex()),
            idle_seconds=now - record.touched,
            request_ids=len(record.ids),
            requests=record.requests,
            get_active=record.get_active,
            sdk_running=record.task is not None and not record.task.done(),
            active_session=digest(active[0].session_id.encode().hex()) if active else None,
            active_idle_seconds=now - active_record.touched if active_record else None,
        )
        task = original_retire(sid)

        def finished(task):
            if accelerated:
                app.idle_seconds = original_seconds
            app.retire = original_retire
            active = app.service.active
            observer.record(
                event="idle_expiry_finished",
                session=digest(sid.encode().hex()),
                completed=not task.cancelled() and task.exception() is None,
                sdk_finished=record.task is not None
                and record.task.done()
                and not record.task.cancelled()
                and record.task.exception() is None,
                sdk_terminated=record.transport.is_terminated,
                sdk_streams=len(record.transport._request_streams),
                request_ids=len(record.ids),
                requests=record.requests,
                session_registered=sid in app.sessions,
                active_session=digest(active[0].session_id.encode().hex()) if active else None,
                restored_seconds=app.idle_seconds,
                **observer.registry_snapshot(),
            )

        task.add_done_callback(finished)
        return task

    app.retire = retire
    if accelerated:
        app.idle_seconds = seconds


def crash_active_service(observer: ObserveHTTP) -> None:
    """Exit without cleanup only while this fixture supervises an unfinished call."""
    active = observer.app.service.active
    if active is None or active[1].done():
        raise RuntimeError("Crash fault requires unfinished active work")
    observer.record(
        event="abrupt_exit",
        session=digest(active[0].session_id.encode().hex()),
        active=True,
    )
    os._exit(86)


if __name__ == "__main__":
    import argparse
    import asyncio
    import importlib
    import importlib.metadata
    import sys

    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("prototype", "config", "state-dir", "token-file", "evidence"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--stop-file", type=Path)
    parser.add_argument("--crash-file", type=Path)
    expiry = parser.add_mutually_exclusive_group()
    expiry.add_argument("--idle-expiry-file", type=Path)
    expiry.add_argument("--default-idle-expiry-file", type=Path)
    parser.add_argument("--refuse-cleanup-file", type=Path)
    parser.add_argument("--refuse-completed-cleanup-file", type=Path)
    parser.add_argument("--drop-result-file", type=Path)
    parser.add_argument("--image", required=True)
    parser.add_argument("--port", type=int, default=19763)
    args = parser.parse_args()
    args.image = resolve_image(args.image)
    sys.path.insert(0, str(args.prototype.resolve().parent))
    prototype = importlib.import_module("server")
    transport = importlib.import_module("workload_http")

    async def main():
        token = args.token_file.read_text(encoding="ascii").strip()
        config = args.config.read_text(encoding="utf-8")
        with prototype.ownership(args.state_dir) as owner:
            app = await prototype.http_application(args.image, config, owner, token, args.port)
            observer = ObserveHTTP(app, args.evidence, args.drop_result_file, owner)
            observer.record(
                event="startup",
                source_hashes={
                    path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                    for path in [
                        *sorted(args.prototype.resolve().parent.glob("*.py")),
                        Path(__file__),
                    ]
                },
                versions={
                    name: importlib.metadata.version(name)
                    for name in (
                        "maf-sandbox",
                        "maf-sandbox-bicep",
                        "maf-sandbox-docker",
                        "mcp",
                        "agent-framework-core",
                        "uvicorn",
                    )
                },
            )
            if args.refuse_cleanup_file is not None:
                refuse_owned_cleanup(observer, args.refuse_cleanup_file)
            if args.refuse_completed_cleanup_file is not None:
                refuse_completed_cleanup(observer, args.refuse_completed_cleanup_file)
            host = transport.server(app)
            host.config.app = observer

            async def stop_requested():
                while args.stop_file is not None and not args.stop_file.exists():
                    if args.idle_expiry_file is not None and args.idle_expiry_file.exists():
                        args.idle_expiry_file.unlink()
                        arm_idle_expiry(observer)
                    if (
                        args.default_idle_expiry_file is not None
                        and args.default_idle_expiry_file.exists()
                    ):
                        args.default_idle_expiry_file.unlink()
                        arm_idle_expiry(observer, accelerated=False)
                    if args.crash_file is not None and args.crash_file.exists():
                        args.crash_file.unlink()
                        crash_active_service(observer)
                    await asyncio.sleep(0.1)
                if args.stop_file is not None:
                    host.should_exit = True

            watcher = asyncio.create_task(stop_requested())
            try:
                await host.serve()
            finally:
                watcher.cancel()
                await asyncio.gather(watcher, return_exceptions=True)
            observer.record(
                event="shutdown",
                sessions=len(app.sessions),
                active=app.service.active is not None,
                poisoned=app.service.poisoned,
            )
            if not host.started or app.service.poisoned:
                raise RuntimeError("Service did not establish readiness and clean shutdown")

    asyncio.run(main())
