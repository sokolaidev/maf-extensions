"""Select fixed native smoke calls without supplying their results."""

from __future__ import annotations

import json
import secrets
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

from .evidence import decode, require

LIMIT = 4 * 1024 * 1024


class Provider:
    """Serve one selected call per turn and retain the Gateway's matching result."""

    def __init__(self) -> None:
        self.token = secrets.token_hex(32)
        self.turn = ""
        self.tool = ""
        self.arguments: dict[str, Any] = {}
        self.calls = 0
        self.results: list[Any] = []
        self.errors: list[str] = []
        self.requests = 0
        self.emitted: dict[str, Any] = {}
        provider = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                self.connection.settimeout(20)
                try:
                    require(self.path == "/v1/chat/completions", "provider_route")
                    require(
                        self.headers.get("Authorization") == "Bearer " + provider.token,
                        "provider_auth",
                    )
                    size = int(self.headers.get("Content-Length", "0"))
                    require(0 < size <= LIMIT, "provider_request_size")
                    raw = self.rfile.read(size)
                    require(len(raw) == size, "provider_request_incomplete")
                    payload = provider.reply(decode(raw))
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                except Exception as exc:
                    provider.errors.append(type(exc).__name__)
                    self.send_error(502, "Smoke provider refused the request")

            def log_message(self, format: str, *args: Any) -> None:
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}/v1"

    def select(self, turn: str, tool: str, arguments: dict[str, Any]) -> None:
        self.turn, self.tool, self.arguments = turn, tool, arguments
        self.calls, self.requests = 0, 0
        self.results.clear()
        self.errors.clear()
        self.emitted.clear()

    def reply(self, body: dict[str, Any]) -> bytes:
        self.requests += 1
        require(self.requests <= 2, "provider_request_budget")
        messages = body["messages"]
        last_user = next(m for m in reversed(messages) if m["role"] == "user")
        require("smoke-turn=" + self.turn in str(last_user["content"]), "provider_turn")
        call_id = "call" + self.turn
        if self.calls:
            matches = [
                m for m in messages if m.get("role") == "tool" and m.get("tool_call_id") == call_id
            ]
            require(len(matches) == 1, "provider_result_identity")
            calls = [
                c
                for m in messages
                if m.get("role") == "assistant"
                for c in m.get("tool_calls", [])
                if c.get("id") == call_id
            ]
            require(len(calls) == 1, "provider_call_identity")
            require(
                calls[0]["function"]["name"] == self.emitted["name"]
                and json.loads(calls[0]["function"]["arguments"]) == self.emitted["arguments"],
                "provider_call_changed",
            )
            self.results.append(matches[0]["content"])
            delta: dict[str, Any] = {"role": "assistant", "content": "Smoke call settled."}
            reason = "stop"
        else:
            names = {t.get("function", {}).get("name") for t in body.get("tools", [])}
            name, arguments = self.tool, self.arguments
            if name not in names and "tool_call" in names:
                name, arguments = "tool_call", {"id": self.tool, "args": arguments}
            require(name in names, "provider_tool_unavailable")
            self.calls += 1
            self.emitted = {"name": name, "arguments": arguments}
            delta = {
                "role": "assistant",
                "tool_calls": [
                    {
                        "index": 0,
                        "id": call_id,
                        "type": "function",
                        "function": {"name": name, "arguments": json.dumps(arguments)},
                    }
                ],
            }
            reason = "tool_calls"
        chunks = []
        for content, finish in ((delta, None), ({}, reason)):
            chunks.append(
                "data: "
                + json.dumps(
                    {
                        "id": self.turn,
                        "object": "chat.completion.chunk",
                        "created": 1,
                        "model": "smoke",
                        "choices": [{"index": 0, "delta": content, "finish_reason": finish}],
                    }
                )
                + "\n\n"
            )
        return ("".join(chunks) + "data: [DONE]\n\n").encode()

    def start(self) -> None:
        self.thread.start()

    def close(self) -> None:
        try:
            if self.thread.is_alive():
                self.server.shutdown()
                self.thread.join(timeout=25)
        finally:
            self.server.server_close()
        require(not self.thread.is_alive(), "provider_cleanup")
