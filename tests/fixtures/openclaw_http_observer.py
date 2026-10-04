"""Observe MCP control messages in a dedicated qualification service without retaining payloads."""

# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "agent-framework-core==1.19.0",
#     "anyio>=4.5,<5",
#     "maf-sandbox==0.46.0",
#     "maf-sandbox-bicep==0.22.0",
#     "maf-sandbox-docker==0.24.4",
#     "mcp==1.26.0",
#     "pydantic>=2.11,<3",
#     "jsonschema>=4.26,<5",
#     "starlette==1.7.0",
#     "uvicorn==0.54.0",
# ]
# ///

from __future__ import annotations

import hashlib
import json
import time
import uuid
from pathlib import Path
from typing import Any


def digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()


class ObserveHTTP:
    """Transparent ASGI wrapper; write only allowlisted, pseudonymous transport observations."""

    def __init__(self, app: Any, evidence: Path) -> None:
        self.app = app
        self.evidence = evidence

    def record(self, **fields: Any) -> None:
        with self.evidence.open("a", encoding="utf-8") as output:
            output.write(json.dumps({"time_ns": time.time_ns(), **fields}) + "\n")

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers", []))
        session = headers.get(b"mcp-session-id")
        sid = digest(session.hex()) if session else None
        exchange = uuid.uuid4().hex
        body = bytearray()
        observed = False
        method = scope["method"]

        async def read():
            nonlocal observed
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
            return message

        async def write(message):
            if message["type"] == "http.response.start":
                response_session = dict(message.get("headers", [])).get(b"mcp-session-id")
                self.record(
                    event="response",
                    exchange=exchange,
                    method=method,
                    status=message["status"],
                    session=digest(response_session.hex()) if response_session else sid,
                )
            await send(message)

        try:
            await self.app(scope, read, write)
        finally:
            self.record(
                event="settled",
                exchange=exchange,
                method=method,
                session=sid,
                sessions=len(self.app.sessions),
                active=self.app.service.active is not None,
            )


if __name__ == "__main__":
    import argparse
    import asyncio
    import importlib
    import importlib.metadata
    import sys

    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("prototype", "config", "state-dir", "token-file", "evidence"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--port", type=int, default=19763)
    args = parser.parse_args()
    sys.path.insert(0, str(args.prototype.resolve().parent))
    prototype = importlib.import_module("server")
    transport = importlib.import_module("workload_http")

    async def main():
        token = args.token_file.read_text(encoding="ascii").strip()
        config = args.config.read_text(encoding="utf-8")
        with prototype.ownership(args.state_dir) as owner:
            app = await prototype.http_application(args.image, config, owner, token, args.port)
            observer = ObserveHTTP(app, args.evidence)
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
            host = transport.server(app)
            host.config.app = observer
            await host.serve()
            observer.record(
                event="shutdown",
                sessions=len(app.sessions),
                active=app.service.active is not None,
                poisoned=app.service.poisoned,
            )
            if not host.started or app.service.poisoned:
                raise RuntimeError("Service did not establish readiness and clean shutdown")

    asyncio.run(main())
