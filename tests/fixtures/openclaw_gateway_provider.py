"""Deterministic loopback provider for manual Gateway qualification, not an LLM."""

from __future__ import annotations

import argparse
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

SOURCES = {
    "valid": "output greeting string = 'hello'",
    "invalid": "output greeting int = 'wrong'",
    "incomplete": "module absent 'br/public:avm/res/storage/storage-account:0.0.0' = { name: 'test' }",
    "cancel": "\n".join(f"var v{i} = range(0, 1000)" for i in range(1500)),
}


def handler(evidence: Path) -> type[BaseHTTPRequestHandler]:
    """Record tool results and names without retaining the host's system prompt."""

    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            size = int(self.headers.get("Content-Length", "0"))
            if self.path != "/v1/chat/completions" or not 0 < size <= 4 * 1024 * 1024:
                self.send_error(400)
                return
            body = json.loads(self.rfile.read(size))
            messages = body["messages"]
            user = next(item for item in reversed(messages) if item["role"] == "user")
            match = re.search(
                r"qualification (valid|invalid|incomplete|cancel|denied|helper)",
                str(user["content"]),
            )
            if match is None:
                self.send_error(400, "Expected a qualification scenario")
                return
            case = match[1]
            turn = re.search(r"qualification-turn=([0-9a-f]{32})(?![0-9a-f])", str(user["content"]))
            record: dict[str, Any] = {
                "scenario": case,
                "turn": turn[1] if turn else None,
                "advertised_tools": [tool["function"]["name"] for tool in body.get("tools", [])],
            }
            if messages[-1]["role"] == "tool":
                record["tool_result"] = messages[-1]["content"]
                delta: dict[str, Any] = {
                    "role": "assistant",
                    "content": "QUALIFICATION_TOOL_RESULT_RECEIVED",
                }
                finish = "stop"
            else:
                name = {
                    "denied": "exec",
                    "helper": "bicep__resources_list",
                }.get(case, "bicep__bicep_validate")
                arguments = {
                    "id": name,
                    "args": {
                        "files": [
                            {"path": "main.bicep", "content": SOURCES.get(case, SOURCES["valid"])}
                        ]
                    },
                }
                delta = {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "call_" + (turn[1] if turn else "qualification"),
                            "type": "function",
                            "function": {"name": "tool_call", "arguments": json.dumps(arguments)},
                        }
                    ],
                }
                finish = "tool_calls"
            with lock, evidence.open("a", encoding="utf-8") as output:
                output.write(json.dumps(record) + "\n")
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for value, reason in [(delta, None), ({}, finish)]:
                chunk = {
                    "id": "qualification",
                    "object": "chat.completion.chunk",
                    "created": 1,
                    "model": "fixture",
                    "choices": [{"index": 0, "delta": value, "finish_reason": reason}],
                }
                self.wfile.write(("data: " + json.dumps(chunk) + "\n\n").encode())
            self.wfile.write(b"data: [DONE]\n\n")

        def log_message(self, format: str, *args: Any) -> None:
            pass

    return Handler


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=19762)
    parser.add_argument("--evidence", type=Path, required=True)
    args = parser.parse_args()
    with ThreadingHTTPServer(("127.0.0.1", args.port), handler(args.evidence)) as server:
        server.serve_forever()
