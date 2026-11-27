"""Bound one pre-admission worker outage without changing model messages."""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Callable
from typing import Any

from _evidence import INVALID, TOOL, require
from _provider import conversation_messages


def fingerprint(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def selected_calls(payload: bytes) -> list[dict[str, Any]]:
    """Reassemble streamed function calls before releasing the response to Gateway."""
    calls: dict[int, dict[str, str]] = {}
    for line in payload.decode("utf-8").splitlines():
        if not line.startswith("data:") or line[5:].strip() == "[DONE]":
            continue
        chunk = json.loads(line[5:])
        choices = chunk.get("choices", [])
        require(len(choices) <= 1, "Recovery requires one model choice")
        for choice in choices:
            for delta in choice.get("delta", {}).get("tool_calls", []):
                call = calls.setdefault(delta["index"], {"id": "", "name": "", "arguments": ""})
                call["id"] += delta.get("id", "")
                for field in ("name", "arguments"):
                    call[field] += delta.get("function", {}).get(field, "")
    selected = []
    for call in calls.values():
        if call["name"] not in {"tool_call", "dispatch_action"}:
            require(call["name"] != TOOL, "Recovery requires the Gateway dispatcher")
            continue
        args = json.loads(call["arguments"])
        if args.get("id") in {TOOL, "mcp:bicep:" + TOOL}:
            require(bool(call["id"]), "Missing model tool-call identity")
            selected.append({"id": call["id"], "arguments": args["args"]})
    require(len(selected) <= 1, "Parallel Bicep calls exceed the recovery budget")
    return selected


def current_turn(body: dict[str, Any]) -> str:
    messages = conversation_messages(body["messages"])
    user = next(message for message in reversed(messages) if message["role"] == "user")
    marker = re.search(r"sample-turn=([0-9a-f]{32})(?![0-9a-f])", str(user["content"]))
    require(marker is not None, "Missing recovery turn identity")
    assert marker is not None
    return marker[1]


class WorkerRestart:
    """Restart only after the failed tool message reaches the real-model relay."""

    def __init__(
        self,
        stop: Callable[[], None],
        restart: Callable[[], None],
        turn_identity: Callable[[dict[str, Any]], str] = current_turn,
    ) -> None:
        self.stop = stop
        self.restart = restart
        self.turn_identity = turn_identity
        self.attempts: list[dict[str, Any]] = []
        self.events: list[dict[str, Any]] = []
        self.failed_id = ""
        self.failed_turn = ""
        self.failure: dict[str, Any] | None = None
        self.ignored_calls: set[str] = set()

    def event(self, name: str, **fields: Any) -> None:
        self.events.append({"event": name, "time_ns": time.time_ns(), **fields})

    def response(self, body: dict[str, Any], payload: bytes) -> None:
        for call in selected_calls(payload):
            require(len(self.attempts) < 3, "Bicep recovery attempt budget exhausted")
            require(
                all(item["id"] != call["id"] for item in self.attempts),
                "Model reused a tool-call identity",
            )
            turn = self.turn_identity(body)
            call["turn"] = turn
            if not self.attempts:
                require(
                    call["arguments"] == {"files": [{"path": "main.bicep", "content": INVALID}]},
                    "Fault requires the exact invalid source",
                )
                self.failed_id, self.failed_turn = call["id"], turn
                self.stop()
                self.event("worker_stopped", call=fingerprint(call["id"]), turn=fingerprint(turn))
            elif len(self.attempts) == 1:
                require(self.failure is not None, "Retry selected before the observed failure")
                require(turn == self.failed_turn, "Retry moved to another user turn")
                require(
                    call["arguments"] == self.attempts[0]["arguments"],
                    "Retry changed the interrupted source",
                )
                self.event("retry_selected", call=fingerprint(call["id"]), turn=fingerprint(turn))
            else:
                require(turn != self.failed_turn, "Unexpected extra call in the recovery turn")
                self.event("repair_selected", call=fingerprint(call["id"]), turn=fingerprint(turn))
            self.attempts.append(call)

    def request(self, body: dict[str, Any]) -> None:
        if not self.failed_id or self.failure is not None:
            return
        messages = conversation_messages(body["messages"])
        found = [
            message
            for message in messages
            if message.get("role") == "tool" and message.get("tool_call_id") == self.failed_id
        ]
        if not found:
            return
        require(len(found) == 1, "Repeated interrupted tool result")
        require(self.turn_identity(body) == self.failed_turn, "Failure moved to another turn")
        raw = found[0]["content"]
        require(isinstance(raw, str), "Missing failed tool message")
        projected = json.loads(raw[raw.index("{") : raw.rindex("}") + 1])
        require(
            isinstance(projected, dict)
            and (
                bool(projected.get("error")) or projected.get("result", {}).get("isError") is True
            ),
            "Interrupted call did not produce an explicit tool error",
        )
        self.failure = {
            "call": fingerprint(self.failed_id),
            "turn": fingerprint(self.failed_turn),
            "message_sha256": fingerprint(raw),
        }
        self.event("failure_observed", **self.failure)
        self.restart()
        self.event("worker_ready")
        self.ignored_calls.add(self.failed_id)

    def result(self) -> dict[str, Any]:
        require(len(self.attempts) == 3 and self.failure is not None, "Incomplete model recovery")
        require(
            [item["event"] for item in self.events]
            == [
                "worker_stopped",
                "failure_observed",
                "worker_ready",
                "retry_selected",
                "repair_selected",
            ],
            "Invalid worker recovery order",
        )
        return {
            "profile": "pre-admission-worker-restart-v1",
            "attempts": len(self.attempts),
            "failed_attempts": 1,
            "same_turn_retry": True,
            "events": self.events,
        }


def check_container_events(
    events: list[dict[str, Any]], scope: str, ready_ns: int
) -> dict[str, int]:
    """Require exactly two distinct executions, both created after restart and removed."""
    creates: dict[str, int] = {}
    destroys: dict[str, int] = {}
    for event in events:
        if event.get("Action") not in {"create", "destroy"}:
            continue
        require(event.get("Type") == "container", "Unexpected Docker event type")
        require(
            event.get("Actor", {}).get("Attributes", {}).get("maf-sandbox.scope") == scope,
            "Docker event belongs to another owner",
        )
        target = event.get("Actor", {}).get("ID")
        stamp = event.get("timeNano")
        require(isinstance(target, str) and len(target) == 64, "Missing container identity")
        if type(stamp) is not int or stamp <= ready_ns:
            raise RuntimeError("Execution predates worker readiness")
        collection = creates if event["Action"] == "create" else destroys
        require(target not in collection, "Repeated container lifecycle event")
        collection[target] = stamp
    require(len(creates) == 2 and creates.keys() == destroys.keys(), "Unexpected execution count")
    require(all(creates[key] < destroys[key] for key in creates), "Invalid disposal order")
    return {"created": len(creates), "destroyed": len(destroys)}
