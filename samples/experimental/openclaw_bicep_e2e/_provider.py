"""Local deterministic provider or bounded relay to an operator-selected model."""

from __future__ import annotations

import hashlib
import http.client
import json
import re
import secrets
import threading
from collections import Counter
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from _evidence import INVALID, TOOL, VALID, require

LIMIT = 4 * 1024 * 1024
MAX_REQUESTS = 32
MAX_COMPLETION_TOKENS = 4096


class Provider:
    def __init__(
        self,
        evidence: Path,
        upstream: str,
        model: str,
        key: str,
        reasoning_effort: str = "provider-default",
    ) -> None:
        require(reasoning_effort in {"provider-default", "none"}, "Unknown reasoning profile")
        self.reasoning_effort = reasoning_effort
        self.evidence, self.upstream, self.model, self.key = evidence, upstream, model, key
        self.token = secrets.token_hex(32)
        self.records: list[dict[str, Any]] = []
        self.seen: dict[str, str] = {}
        self.function_calls: dict[str, str] = {}
        self.bicep_identifiers: dict[str, str] = {}
        self.requests = 0
        self.forwarded = 0
        self.response_models: set[str] = set()
        self.upstream_failures: list[dict[str, Any]] = []
        self.errors: list[str] = []
        provider = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                self.connection.settimeout(240)
                try:
                    require(self.path == "/v1/chat/completions", "Unknown provider route")
                    require(
                        self.headers.get("Authorization") == "Bearer " + provider.token,
                        "Provider authorization failed",
                    )
                    size = int(self.headers.get("Content-Length", "0"))
                    require(0 < size <= LIMIT, "Provider request exceeds bounds")
                    raw = self.rfile.read(size)
                    require(len(raw) == size, "Incomplete provider request")
                    provider.requests += 1
                    require(provider.requests <= MAX_REQUESTS, "Model request budget exhausted")
                    body = json.loads(raw)
                    turn, repair = provider.observe(body)
                    payload = (
                        provider.forward(body)
                        if provider.upstream
                        else provider.smoke(body, turn, repair)
                    )
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                except Exception as exc:
                    provider.errors.append(type(exc).__name__)
                    self.send_error(502, "Sample provider failed; no acceptance recorded")

            def log_message(self, format: str, *args: Any) -> None:
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}/v1"

    def start(self) -> None:
        self.thread.start()

    def close(self) -> None:
        try:
            if self.thread.is_alive():
                self.server.shutdown()
                self.thread.join()
        finally:
            self.server.server_close()

    def observe(self, body: dict[str, Any]) -> tuple[str, bool]:
        messages = body["messages"]
        user = next(message for message in reversed(messages) if message["role"] == "user")
        text = str(user["content"])
        marker = re.search(r"sample-turn=([0-9a-f]{32})(?![0-9a-f])", text)
        require(marker is not None, "Missing turn identity")
        assert marker is not None
        turn = marker[1]
        calls: dict[str, Any] = {}
        call_turns: dict[str, str] = {}
        current_turn = ""
        for message in messages:
            if message["role"] == "user":
                marker = re.search(
                    r"sample-turn=([0-9a-f]{32})(?![0-9a-f])", str(message["content"])
                )
                current_turn = marker[1] if marker else ""
            if message["role"] == "assistant":
                for call in message.get("tool_calls", []):
                    function = call["function"]
                    name = function["name"]
                    self.function_calls[call["id"]] = (
                        name
                        if name in {"tool_call", "tool_search", "tool_describe", TOOL}
                        else "other"
                    )
                    if function["name"] == "tool_call":
                        args = json.loads(function["arguments"])
                        if args.get("id") in {TOOL, "mcp:bicep:" + TOOL}:
                            self.bicep_identifiers[call["id"]] = (
                                "short" if args["id"] == TOOL else "canonical"
                            )
                            calls[call["id"]] = args["args"]
                            call_turns[call["id"]] = current_turn
            if message["role"] == "tool" and message.get("tool_call_id") in calls:
                call_id = message["tool_call_id"]
                text = message["content"]
                projected = json.loads(text[text.index("{") : text.rindex("}") + 1])
                fingerprint = json.dumps([calls[call_id], projected], sort_keys=True)
                if call_id in self.seen:
                    require(self.seen[call_id] == fingerprint, "Tool identity was reused")
                    continue
                self.seen[call_id] = fingerprint
                require(call_turns[call_id], "Tool call has no turn identity")
                record = {
                    "turn": call_turns[call_id],
                    "arguments": calls[call_id],
                    "projected": projected,
                }
                self.records.append(record)
                with self.evidence.open("a", encoding="utf-8") as output:
                    output.write(json.dumps(record) + "\n")
        return turn, "Repair the type error" in str(user["content"])

    def smoke(self, body: dict[str, Any], turn: str, repair: bool) -> bytes:
        if body["messages"][-1]["role"] == "tool":
            delta: dict[str, Any] = {"role": "assistant", "content": "Compiler result received."}
            reason = "stop"
        else:
            args = {
                "id": TOOL,
                "args": {
                    "files": [{"path": "main.bicep", "content": VALID if repair else INVALID}]
                },
            }
            delta = {
                "role": "assistant",
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "call_" + turn,
                        "type": "function",
                        "function": {"name": "tool_call", "arguments": json.dumps(args)},
                    }
                ],
            }
            reason = "tool_calls"
        chunks = []
        for content, finish in [(delta, None), ({}, reason)]:
            chunks.append(
                "data: "
                + json.dumps(
                    {
                        "id": "sample",
                        "object": "chat.completion.chunk",
                        "created": 1,
                        "model": "sample",
                        "choices": [{"index": 0, "delta": content, "finish_reason": finish}],
                    }
                )
                + "\n\n"
            )
        return ("".join(chunks) + "data: [DONE]\n\n").encode()

    def forward(self, body: dict[str, Any]) -> bytes:
        url = urlsplit(self.upstream)
        factory = (
            http.client.HTTPSConnection if url.scheme == "https" else http.client.HTTPConnection
        )
        connection = factory(url.hostname or "", url.port, timeout=240)
        body["model"] = self.model
        body.pop("max_tokens", None)
        body.pop("reasoning_effort", None)
        if self.reasoning_effort != "provider-default":
            body["reasoning_effort"] = self.reasoning_effort
        body["max_completion_tokens"] = MAX_COMPLETION_TOKENS
        try:
            connection.request(
                "POST",
                url.path.rstrip("/") + "/chat/completions",
                json.dumps(body),
                {"Content-Type": "application/json", "Authorization": "Bearer " + self.key},
            )
            response = connection.getresponse()
            if response.status != 200:
                self.record_failure(response.status, response.read(LIMIT + 1))
            require(response.status == 200, "Upstream model request failed")
            require(
                response.getheader("Content-Type", "").startswith("text/event-stream"),
                "Model must support streaming chat completions",
            )
            payload = response.read(LIMIT + 1)
            require(len(payload) <= LIMIT, "Model response exceeds bounds")
            self.observe_response(payload)
            self.forwarded += 1
            return payload
        finally:
            connection.close()

    def record_failure(self, status: int, payload: bytes) -> None:
        """Keep fixed API error identifiers, never provider messages or arbitrary fields."""
        failure: dict[str, Any] = {"status": status}
        try:
            error = json.loads(payload)["error"]
            allowed = {
                "code": {
                    "unsupported_value",
                    "unsupported_parameter",
                    "invalid_parameter",
                    "invalid_request_error",
                    "DeploymentNotFound",
                    "PermissionDenied",
                    "Unauthorized",
                    "rate_limit_exceeded",
                    "content_filter",
                },
                "param": {
                    "temperature",
                    "max_tokens",
                    "max_completion_tokens",
                    "stream",
                    "stream_options",
                    "tools",
                    "tool_choice",
                    "model",
                    "messages",
                    "parallel_tool_calls",
                    "reasoning_effort",
                },
            }
            for key, values in allowed.items():
                value = error.get(key)
                failure[key] = value if isinstance(value, str) and value in values else "other"
        except (ValueError, KeyError, TypeError, AttributeError):
            failure["code"] = "unrecognized"
        self.upstream_failures.append(failure)

    def observe_response(self, payload: bytes) -> None:
        """Require a completed stream and retain only hashed model identities."""
        done = finished = False
        models = set()
        for line in payload.decode("utf-8").splitlines():
            if not line.startswith("data:"):
                continue
            require(not done, "Model sent data after stream completion")
            data = line[5:].strip()
            if data == "[DONE]":
                done = True
                continue
            chunk = json.loads(data)
            require(not chunk.get("error"), "Model stream returned an error")
            model = chunk.get("model")
            if isinstance(model, str) and model:
                models.add(hashlib.sha256(model.encode()).hexdigest())
            finished |= any(choice.get("finish_reason") for choice in chunk.get("choices", []))
        require(done and finished and models, "Model stream did not complete")
        self.response_models.update(models)

    def observations(self) -> dict[str, Any]:
        """Describe actual forwarding without disclosing the endpoint or deployment name."""
        return {
            "requests": self.requests,
            "forwarded_responses": self.forwarded,
            "error_count": len(self.errors),
            "upstream_failures": self.upstream_failures,
            "endpoint_sha256": hashlib.sha256(self.upstream.encode()).hexdigest(),
            "requested_model_sha256": hashlib.sha256(self.model.encode()).hexdigest(),
            "response_model_sha256": sorted(self.response_models),
            "max_requests": MAX_REQUESTS,
            "max_completion_tokens": MAX_COMPLETION_TOKENS,
            "reasoning_effort": self.reasoning_effort,
            "function_calls": dict(Counter(self.function_calls.values())),
            "bicep_identifiers": dict(Counter(self.bicep_identifiers.values())),
            "observed_bicep_results": len(self.records),
        }
