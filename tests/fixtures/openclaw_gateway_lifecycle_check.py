"""Supervise a dedicated Gateway and qualify bounded HTTP restart and result-loss behavior."""

from __future__ import annotations

import argparse
import errno
import hashlib
import http.client
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import uuid
from contextlib import ExitStack
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from openclaw_gateway_check import docker
from openclaw_gateway_http_check import (
    BASELINE_DISPATCHES,
    check,
    matching_cancel,
    projected_result,
    records,
    require,
    verify_busy,
    verify_dispatch_count,
    verify_environment,
    verify_outcome,
)
from openclaw_http_observer import resolve_image

REGISTRY_LIMIT = 8
CHURN_CYCLES = 3
DISCOVERY_COOLDOWN_SECONDS = 35
UNAVAILABLE_DISPATCHES = 4
IDLE_DISPATCHES = 6
DOCKER_DISCONNECT_DISPATCHES = 7
DEFAULT_IDLE_KEEPALIVES = 7
DEFAULT_IDLE_DISPATCHES = 2 + DEFAULT_IDLE_KEEPALIVES + 3
REGISTRY_DISPATCHES = REGISTRY_LIMIT + CHURN_CYCLES + (REGISTRY_LIMIT - 1) + REGISTRY_LIMIT
LIFECYCLE_DISPATCHES = (
    30 + (REGISTRY_LIMIT - 2) + CHURN_CYCLES + (REGISTRY_LIMIT - 1) + REGISTRY_LIMIT
)


class DockerConnectionFault:
    """Disconnect only the fixture's private CLI context from a local Docker engine."""

    def __init__(self, root: Path, stack: ExitStack):
        current = json.loads(docker("context", "inspect", "--format", "{{json .}}"))
        self.endpoint = current["Endpoints"]["docker"]["Host"]
        require(
            self.endpoint.startswith(("npipe:////./pipe/", "unix:///")),
            "Connection-loss qualification requires a local pipe/socket Docker endpoint",
        )
        self.directory = root / "docker-config"
        self.directory.mkdir()
        self.name = "qualification-" + uuid.uuid4().hex
        self.reserved = stack.enter_context(socket.socket())
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.reserved.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        self.reserved.bind(("127.0.0.1", 0))
        self.unreachable = f"tcp://127.0.0.1:{self.reserved.getsockname()[1]}"
        self.command("create", self.name, "--docker", "host=" + self.endpoint)
        stack.callback(self.restore)
        self.environment = {k: v for k, v in os.environ.items() if not k.startswith("DOCKER_")}
        self.environment.update(DOCKER_CONFIG=str(self.directory), DOCKER_CONTEXT=self.name)

    def command(self, *args: str) -> None:
        result = subprocess.run(
            ["docker", "--config", str(self.directory), "context", *args],
            capture_output=True,
            timeout=15,
        )
        require(result.returncode == 0, "Private Docker context configuration failed")

    def disconnect(self) -> None:
        self.command("update", self.name, "--docker", "host=" + self.unreachable)

    def restore(self) -> None:
        self.command("update", self.name, "--docker", "host=" + self.endpoint)


def wait_for(predicate, message: str, seconds: float = 90) -> Any:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(0.2)
    raise RuntimeError(message)


def requests(evidence: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [r for r in evidence if r.get("event") == "request" and r.get("method") == "tools/call"]


def verify_loss(evidence: list[dict[str, Any]], projected: dict[str, Any]) -> dict[str, Any]:
    calls = requests(evidence)
    require(len(calls) == 1, "Unknown-outcome work was missing or replayed")
    lost = [r for r in evidence if r.get("event") == "result_withheld"]
    require(len(lost) == 1, "Missing unique withheld result")
    result = lost[0]
    require(
        result.get("exchange") == calls[0].get("exchange")
        and result.get("session") == calls[0].get("session")
        and bool(result.get("session"))
        and bool(result.get("exchange"))
        and bool(result.get("boot"))
        and result.get("boot") == calls[0].get("boot")
        and result.get("time_ns", 0) > calls[0].get("time_ns", 0)
        and result.get("completed") is True
        and result.get("cleanup") == "confirmed"
        and result.get("status") == 200,
        "Withheld result does not prove completed work with confirmed cleanup",
    )
    require(
        "result" not in projected and "structuredContent" not in projected,
        "Lost result was projected as an outcome",
    )
    require(
        projected.get("status") == "error"
        and "Streamable HTTP error:" in projected.get("error", "")
        and "Internal Server Error" in projected.get("error", ""),
        "Missing transport error",
    )
    return {"completed_result_withheld": True, "dispatches": 1, "transport_error": True}


def verify_crash(evidence: list[dict[str, Any]], projected: dict[str, Any]) -> dict[str, Any]:
    """Require correlated process death with an unknown outcome and no repeated dispatch."""
    calls = requests(evidence)
    faults = [r for r in evidence if r.get("event") == "abrupt_exit"]
    require(
        len(calls) == len(faults) == 1, "Crash work or process-exit evidence missing or repeated"
    )
    call, fault = calls[0], faults[0]
    require(
        bool(call.get("boot"))
        and bool(call.get("session"))
        and bool(call.get("exchange"))
        and fault.get("boot") == call["boot"]
        and fault.get("session") == call["session"]
        and fault.get("active") is True
        and fault.get("time_ns", 0) > call.get("time_ns", 0),
        "Process exit does not match the active call",
    )
    require(
        not any(
            r.get("boot") == call["boot"]
            and (
                r.get("event") == "shutdown"
                or (
                    r.get("exchange") == call["exchange"]
                    and r.get("event") in {"response", "settled", "result_withheld"}
                )
            )
            for r in evidence
        ),
        "Crashed call completed or service shut down gracefully",
    )
    require(
        projected.get("status") == "error"
        and isinstance(projected.get("error"), str)
        and bool(projected["error"])
        and "result" not in projected
        and "structuredContent" not in projected,
        "Crash was projected as a workload outcome",
    )
    return {"dispatches": 1, "unknown_outcome": True, "ungraceful_exit": True}


def verify_recovery(evidence: list[dict[str, Any]], boot: str, container: str) -> None:
    """Accept removal only when it precedes the replacement service's first ready response."""
    started = [r for r in evidence if r.get("event") == "startup_ready"]
    removed = [r for r in evidence if r.get("event") == "recovery_observed"]
    ready = [r for r in evidence if r.get("event") == "ready_observed"]
    require(
        len(started) == len(removed) == len(ready) == 1,
        "Missing unique recovery/readiness observations",
    )
    require(
        started[0].get("boot") == removed[0].get("boot") == ready[0].get("boot") == boot
        and started[0].get("owner_empty") is True
        and bool(boot)
        and removed[0].get("container") == container
        and bool(container)
        and removed[0].get("absent") is True
        and removed[0].get("owner_empty") is True
        and ready[0].get("status") == 200
        and 0
        < started[0].get("time_ns", 0)
        <= removed[0].get("time_ns", 0)
        < ready[0].get("time_ns", 0),
        "Startup cleanup was not established before readiness",
    )


def verify_cleanup_refusal(
    evidence: list[dict[str, Any]],
    boot: str,
    container: str,
    observed: dict[str, Any],
    projections: list[dict[str, Any]],
) -> dict[str, Any]:
    """Require failed startup, retained ownership and two refused Gateway turns."""
    starts = [r for r in evidence if r.get("event") == "startup"]
    faults = [r for r in evidence if r.get("event") == "cleanup_refused"]
    failures = [r for r in evidence if r.get("event") == "startup_failed"]
    stops = [r for r in evidence if r.get("event") == "shutdown"]
    require(
        len(starts) == len(failures) == 1 and len(faults) == 2 and not stops,
        "Missing failed startup or both refused cleanup attempts; unexpected shutdown",
    )
    require(
        bool(boot)
        and bool(container)
        and all(r.get("boot") == boot for r in [*starts, *faults, *failures])
        and all(
            r.get("container") == container and r.get("resource") == "bicep-docker" for r in faults
        )
        and failures[0].get("ready") is False
        and all(
            r.get("poisoned") is True and r.get("active") is False and r.get("sessions") == 0
            for r in failures
        )
        and 0
        < starts[0].get("time_ns", 0)
        < faults[0].get("time_ns", 0)
        < faults[1].get("time_ns", 0)
        < failures[0].get("time_ns", 0)
        < observed.get("time_ns", 0),
        "Cleanup refusal is not correlated with failed, poisoned startup",
    )
    require(
        not any(r.get("event") in {"startup_ready", "request", "response"} for r in evidence),
        "Failed startup admitted transport work or advertised readiness",
    )
    require(
        observed.get("boot") == boot
        and observed.get("container") == container
        and observed.get("exit_code") == 3
        and all(
            observed.get(k) is True
            for k in (
                "paused",
                "sole_owned",
                "owner_unchanged",
                "listener_closed",
                "sentinel_preserved",
            )
        ),
        "Failed startup did not retain the exact orphan and refuse its listener",
    )
    require(
        len(projections) == 2
        and all(
            p.get("status") == "error"
            and isinstance(p.get("error"), str)
            and 'bundle-mcp server "bicep" is not connected' in p["error"]
            and "result" not in p
            and "structuredContent" not in p
            for p in projections
        ),
        "Unavailable service produced a workload result or did not refuse both turns",
    )
    return {
        "startup_refused": True,
        "cleanup_attempts_refused": 2,
        "gateway_turns_refused": 2,
        "dispatches": 0,
        "retained_orphan": True,
        "listener_closed": True,
    }


def verify_completed_cleanup(
    evidence: list[dict[str, Any]],
    projected: dict[str, Any],
    config_digest: str,
    image: str,
) -> dict[str, Any]:
    """Require completed binding work followed by refusal and a failed Gateway result."""
    events = [
        "request",
        "binding_started",
        "binding_completed",
        "completed_cleanup_refused",
        "response",
        "settled",
    ]
    calls = requests(evidence)
    require(len(calls) == 1, "Completed cleanup call missing or replayed")
    call = calls[0]
    selected = [call]
    for event in events[1:]:
        rows = [
            r
            for r in evidence
            if r.get("event") == event
            and (event not in {"response", "settled"} or r.get("exchange") == call.get("exchange"))
        ]
        require(len(rows) == 1, "Missing unique completed-cleanup evidence")
        selected.extend(rows)
    require(
        bool(call.get("boot"))
        and bool(call.get("session"))
        and bool(call.get("exchange"))
        and all(
            r.get("boot") == call["boot"] and r.get("session") == call["session"] for r in selected
        )
        and all(
            0 < a.get("time_ns", 0) < b.get("time_ns", 0) for a, b in zip(selected, selected[1:])
        ),
        "Completed cleanup evidence has wrong identity or ordering",
    )
    completed, fault, response, settled = selected[2:]
    verify_outcome(
        {"result": {"content": [{}], "details": {"structuredContent": completed}}},
        "valid",
        config_digest,
        image,
    )
    require(
        fault.get("resource") == "bicep-docker"
        and fault.get("owner_empty") is True
        and response.get("status") == 200
        and settled.get("active") is False
        and settled.get("poisoned") is True,
        "Completed cleanup did not fail closed after the binding sweep",
    )
    result = projected.get("result", {})
    details = result.get("details", {})
    structured = details.get("structuredContent", {})
    require(
        details.get("status") == "error"
        and len(result.get("content", [])) == 1
        and structured.get("completed") is False
        and structured.get("verdict", "missing") is None
        and structured.get("status") == "cleanup_failed"
        and structured.get("cleanup") == "failed"
        and all(
            structured.get(k) == completed[k] for k in ("source_sha256", "config_sha256", "image")
        ),
        "Gateway cleanup failure preserved success or lost the input identity",
    )
    return {
        "binding_completed": True,
        "success_suppressed": True,
        "cleanup": "failed",
        "poisoned": True,
        "dispatches": 1,
        "owner_empty_after_binding": True,
    }


def verify_poisoned_turns(
    evidence: list[dict[str, Any]],
    projections: list[dict[str, Any]],
    sessions: list[str],
    boot: str,
) -> None:
    """Require both existing sessions to be refused without executing either binding."""
    calls = requests(evidence)
    require(
        len(sessions) == len(set(sessions)) == len(calls) == len(projections) == 2,
        "Missing both poisoned-session probes",
    )
    require(
        [r.get("session") for r in calls] == sessions
        and all(r.get("boot") == boot for r in calls)
        and not any(
            r.get("event") in {"binding_started", "binding_completed", "completed_cleanup_refused"}
            for r in evidence
        ),
        "Poisoned service executed work or changed session identity",
    )
    for call, projection in zip(calls, projections):
        result = projection.get("result", {})
        require(
            result.get("details", {}).get("status") == "error"
            and "structuredContent" not in result.get("details", {})
            and result.get("content")
            == [{"type": "text", "text": "Service or session is unavailable."}],
            "Poisoned session returned a workload outcome",
        )
        settled = [
            r
            for r in evidence
            if r.get("event") == "settled" and r.get("exchange") == call.get("exchange")
        ]
        require(
            len(settled) == 1
            and settled[0].get("boot") == boot
            and settled[0].get("session") == call.get("session")
            and settled[0].get("active") is False
            and settled[0].get("poisoned") is True
            and settled[0].get("time_ns", 0) > call.get("time_ns", 0),
            "Poisoned probe did not settle without active work",
        )


def verify_docker_disconnect(evidence, call, observed) -> dict[str, Any]:
    """Require a real removal refusal, accepted cancellation and independently retained work."""
    boot = call.get("boot")
    require(
        bool(boot)
        and all(r.get("boot") == boot for r in evidence)
        and bool(call.get("session"))
        and bool(call.get("request"))
        and bool(call.get("exchange")),
        "Docker disconnect lacks an identified active call",
    )
    require(
        call.get("time_ns", 0)
        < observed.get("disconnected_ns", 0)
        < observed.get("abort_ns", 0)
        < observed.get("observed_ns", 0)
        and matching_cancel(evidence, call, after_ns=observed["abort_ns"]),
        "Docker disconnect lacks ordered accepted cancellation",
    )
    require(
        isinstance(observed.get("container"), str)
        and len(observed["container"]) == 64
        and all(c in "0123456789abcdef" for c in observed["container"])
        and bool(observed.get("target"))
        and observed.get("owned") == [observed["container"]]
        and observed.get("readiness_status") == 503
        and all(
            observed.get(key) is True
            for key in (
                "compiler_before",
                "paused",
                "same_gateway",
                "same_service",
                "owner_unchanged",
                "sentinel_preserved",
            )
        ),
        "Docker disconnect lacks independent retained-resource or poisoned-readiness evidence",
    )
    removals = [
        r
        for r in evidence
        if r.get("event") == "docker_removal" and r.get("target") == observed["target"]
    ]
    require(bool(removals), "No real removal command targeted the retained compiler")
    require(
        all(
            r.get("boot") == boot
            and observed["abort_ns"]
            < r.get("started_ns", 0)
            <= r.get("time_ns", 0)
            <= observed["observed_ns"]
            and isinstance(r.get("returncode"), int)
            and not isinstance(r["returncode"], bool)
            and r["returncode"] != 0
            and r.get("connection_refused") is True
            and isinstance(r.get("stderr_sha256"), str)
            and len(r["stderr_sha256"]) == 64
            for r in removals
        ),
        "Docker removal did not fail through the real disconnected command path",
    )
    settled = [
        r for r in evidence if r.get("event") == "settled" and r.get("exchange") == call["exchange"]
    ]
    require(
        len(settled) == 1
        and settled[0].get("boot") == boot
        and settled[0].get("session") == call["session"]
        and settled[0].get("active") is False
        and settled[0].get("poisoned") is True
        and max(r["time_ns"] for r in removals)
        <= settled[0].get("time_ns", 0)
        <= observed["observed_ns"],
        "Disconnected call did not settle with admission poisoned",
    )
    return {
        "failed_removal_commands": len(removals),
        "cleanup": "unconfirmed",
        "retained_container": True,
        "readiness_status": 503,
        "poisoned": True,
    }


def verify_docker_recovery(evidence, startup, target: str) -> str:
    """Require the retained compiler's real removal before replacement readiness."""
    starts = [r for r in evidence if r.get("event") == "startup"]
    ready = [r for r in evidence if r.get("event") == "startup_ready"]
    removals = [
        r for r in evidence if r.get("event") == "docker_removal" and r.get("target") == target
    ]
    require(
        bool(target)
        and len(starts) == len(ready) == 1
        and bool(removals)
        and bool(starts[0].get("boot"))
        and starts[0]["boot"] != startup.get("boot")
        and ready[0].get("boot") == starts[0]["boot"]
        and ready[0].get("owner_empty") is True
        and all(
            r.get("boot") == starts[0]["boot"]
            and r.get("returncode") == 0
            and r.get("connection_refused") is False
            and starts[0].get("time_ns", 0)
            < r.get("started_ns", 0)
            <= r.get("time_ns", 0)
            < ready[0].get("time_ns", 0)
            for r in removals
        )
        and bool(startup.get("source_hashes"))
        and bool(startup.get("versions"))
        and starts[0].get("source_hashes") == startup["source_hashes"]
        and starts[0].get("versions") == startup["versions"],
        "Replacement did not remove the retained compiler before readiness",
    )
    return starts[0]["boot"]


def retirement_target(value: dict[str, Any], name: str) -> dict[str, Any]:
    """Select only the exact isolated HTTP session and pin deletion to its current identity."""
    matches = [
        row
        for row in value.get("sessions", [])
        if isinstance(row, dict) and row.get("key") == "agent:main:openai-user:" + name
    ]
    require(
        len(matches) == 1
        and isinstance(matches[0].get("sessionId"), str)
        and bool(matches[0]["sessionId"]),
        "Missing unique fixture Gateway session",
    )
    require(
        name.startswith("lifecycle-")
        and len(name) == 42
        and all(c in "0123456789abcdef" for c in name[10:]),
        "Not a fixture-owned session",
    )
    return {
        "key": matches[0]["key"],
        "expectedSessionId": matches[0]["sessionId"],
        "deleteTranscript": False,
    }


def verify_retirement(
    evidence: list[dict[str, Any]],
    target: str,
    other: str,
    active_call: dict[str, Any],
    observed: dict[str, Any],
    *,
    active: bool,
) -> dict[str, Any]:
    """Require acknowledged deletion of only the selected MCP session and correlated settlement."""
    boot = active_call.get("boot")
    require(
        bool(boot)
        and bool(target)
        and bool(other)
        and target != other
        and active_call.get("session") == (target if active else other),
        "Retirement does not identify both sessions and the active call",
    )
    deletes = [r for r in evidence if r.get("event") == "delete_requested"]
    responses = [
        r for r in evidence if r.get("event") == "response" and r.get("method") == "DELETE"
    ]
    settled = [r for r in evidence if r.get("event") == "retired"]
    require(
        len(deletes) == len(responses) == len(settled) == 1,
        "Missing unique targeted DELETE request, acceptance or settlement",
    )
    deletion, response, end = deletes[0], responses[0], settled[0]
    require(
        bool(deletion.get("exchange"))
        and all(
            r.get("boot") == boot
            and r.get("session") == target
            and r.get("exchange") == deletion["exchange"]
            for r in [deletion, response, end]
        )
        and 0
        < active_call.get("time_ns", 0)
        < observed.get("requested_ns", 0)
        <= deletion.get("time_ns", 0)
        < response.get("time_ns", 0)
        < observed.get("acknowledged_ns", 0)
        <= observed.get("observed_ns", 0)
        and response.get("time_ns", 0) <= end.get("time_ns", 0) < observed.get("observed_ns", 0)
        and end.get("completed") is True
        and response.get("status") == 200
        and end.get("session_registered") is False
        and end.get("sessions") == 1
        and end.get("poisoned") is False
        and end.get("active") is (not active)
        and end.get("active_session") == (None if active else other),
        "Retirement was not selective, settled or acknowledged in order",
    )
    cancellations = [r for r in evidence if r.get("method") == "notifications/cancelled"]
    if active:
        require(
            len(cancellations) == 1
            and cancellations[0].get("boot") == boot
            and matching_cancel(
                [r for r in evidence if r.get("boot") == boot],
                active_call,
                after_ns=observed["requested_ns"],
            ),
            "Active retirement lacks the matching accepted MCP cancellation",
        )
    else:
        require(not cancellations, "Idle retirement cancelled active work")
    require(
        observed.get("deleted") is True
        and observed.get("same_gateway") is True
        and observed.get("same_service") is True
        and observed.get("owner_unchanged") is True
        and observed.get("sentinel_preserved") is True
        and observed.get("exact_container_absent") is active
        and observed.get("compiler_survived") is (not active),
        "Retirement lacks independent resource or process isolation evidence",
    )
    return {
        "deleted_mcp_session": True,
        "remaining_sessions": 1,
        "accepted_cancellation": active,
        "other_session_preserved": True,
        "exact_container_removed": active,
        "same_gateway_and_service": True,
    }


def verify_deleted_turn(status: int, payload: Any) -> int:
    """Pin the Gateway's non-streaming response to a session-deletion abort."""
    require(
        status == 500 and payload == {"error": {"message": "internal error", "type": "api_error"}},
        "Deleted active turn did not return the pinned Gateway abort error",
    )
    return status


def verify_registry(snapshot, expected: set[str], boot: str, after_ns: int) -> dict[str, int]:
    """Require an exact idle registry with only the SDK streams owned by live GET handlers."""
    sessions = snapshot.get("sessions", [])
    require(
        bool(boot)
        and all(expected)
        and len(expected) <= REGISTRY_LIMIT
        and snapshot.get("boot") == boot
        and snapshot.get("time_ns", 0) > after_ns
        and snapshot.get("event") == "registry_snapshot"
        and len(sessions) == len(expected)
        and {r.get("session") for r in sessions} == expected
        and snapshot.get("readers") == 0
        and snapshot.get("active") is False
        and snapshot.get("poisoned") is False,
        "Registry does not preserve the exact idle session set",
    )
    for record in sessions:
        require(
            record.get("initialized") is True
            and record.get("closing") is False
            and record.get("sdk_running") is True
            and record.get("sdk_terminated") is False
            and record.get("request_ids") == 0
            and isinstance(record.get("get_active"), bool)
            and record.get("requests") == int(record["get_active"])
            and record.get("sdk_streams") == int(record["get_active"]),
            "Registry retains unfinished requests or inconsistent SDK ownership",
        )
    return {"adapter_records": len(sessions), "sdk_tasks": len(sessions)}


def verify_registry_refusal(evidence, boot: str) -> dict[str, int]:
    """Require capacity rejection before any tool dispatch, binding or session retirement."""
    messages = [r for r in evidence if r.get("event") == "request"]
    attempts = [r for r in messages if r.get("method") == "initialize"]
    require(
        bool(attempts)
        and all(r.get("method") in {"initialize", "notifications/cancelled"} for r in messages)
        and not any(
            r.get("event") in {"binding_started", "retired", "delete_requested"} for r in evidence
        ),
        "Capacity refusal dispatched work or disturbed registered sessions",
    )
    exchanges = set()
    for attempt in attempts:
        exchange = attempt.get("exchange")
        require(
            bool(exchange)
            and exchange not in exchanges
            and attempt.get("session") is None
            and attempt.get("boot") == boot,
            "Capacity refusal lacks a fresh initialization exchange",
        )
        exchanges.add(exchange)
        responses = [
            r for r in evidence if r.get("event") == "response" and r.get("exchange") == exchange
        ]
        ends = [
            r for r in evidence if r.get("event") == "settled" and r.get("exchange") == exchange
        ]
        require(len(responses) == len(ends) == 1, "Missing unique capacity response and settlement")
        response, end = responses[0], ends[0]
        require(
            all(
                r.get("boot") == boot and r.get("session") is None and r.get("method") == "POST"
                for r in [response, end]
            )
            and 0 < attempt.get("time_ns", 0) < response.get("time_ns", 0) <= end.get("time_ns", 0)
            and response.get("status") == 503
            and end.get("sessions") == REGISTRY_LIMIT
            and end.get("active") is False
            and end.get("poisoned") is False,
            "Initialization was not refused at the unchanged registry limit",
        )
    return {"initialization_attempts": len(attempts), "status": 503}


def verify_churn_retirement(evidence, boot: str, target: str, requested_ns: int) -> None:
    """Require one idle deletion to finish draining SDK and adapter state before reuse."""
    deletes = [r for r in evidence if r.get("event") == "delete_requested"]
    ends = [r for r in evidence if r.get("event") == "retired"]
    require(len(deletes) == len(ends) == 1, "Missing unique churn retirement")
    request, end = deletes[0], ends[0]
    responses = [
        r
        for r in evidence
        if r.get("event") == "response" and r.get("exchange") == request.get("exchange")
    ]
    require(len(responses) == 1, "Missing churn DELETE acceptance")
    response = responses[0]
    require(
        bool(target)
        and bool(boot)
        and bool(request.get("exchange"))
        and all(
            r.get("session") == target
            and r.get("boot") == boot
            and r.get("exchange") == request["exchange"]
            for r in [request, response, end]
        )
        and requested_ns
        <= request.get("time_ns", 0)
        < response.get("time_ns", 0)
        <= end.get("time_ns", 0)
        and response.get("method") == "DELETE"
        and response.get("status") == 200
        and end.get("completed") is True
        and end.get("session_registered") is False
        and end.get("sessions") == REGISTRY_LIMIT - 1
        and end.get("active") is False
        and end.get("active_session") is None
        and end.get("poisoned") is False
        and end.get("sdk_finished") is True
        and end.get("sdk_terminated") is True
        and end.get("sdk_streams") == end.get("request_ids") == end.get("requests") == 0
        and not any(
            r.get("event") == "binding_started"
            or (r.get("event") == "request" and r.get("method") != "notifications/cancelled")
            for r in evidence
        ),
        "Churn retirement did not drain only the selected idle session",
    )


def verify_catalog_refusal(status, payload, evidence, observed) -> dict[str, Any]:
    """Identify a pre-provider catalog refusal separately from service admission failures."""
    require(
        status == 500
        and payload == {"error": {"message": "internal error", "type": "api_error"}}
        and observed.get("boot")
        and observed.get("provider_seen") is False
        and observed.get("gateway_catalog_error") is True
        and all(r.get("boot") == observed["boot"] for r in evidence)
        and not any(
            r.get("event") in {"binding_started", "delete_requested", "retired"}
            or (
                r.get("event") == "request"
                and r.get("method") in {"initialize", "tools/list", "tools/call"}
            )
            for r in evidence
        ),
        "Free-slot probe does not establish Gateway catalog refusal before MCP initialization",
    )
    return {
        "reconnected": False,
        "gateway_status": 500,
        "reason": "no_callable_tools",
        "free_slots": 1,
        "mcp_initialization_attempts": 0,
    }


def verify_discovery_refresh(
    evidence, boot: str, previous: set[str], after_ns: int, *, expected_count: int = REGISTRY_LIMIT
) -> str:
    """Require one fresh MCP connection and tool listing without replaying workload calls."""
    messages = [r for r in evidence if r.get("event") == "request"]
    require(
        bool(boot)
        and 0 < expected_count <= REGISTRY_LIMIT
        and all(r.get("boot") == boot for r in evidence)
        and all(
            r.get("method")
            in {"initialize", "tools/list", "notifications/initialized", "notifications/cancelled"}
            for r in messages
        )
        and not any(
            r.get("event") in {"binding_started", "retired", "delete_requested"} for r in evidence
        ),
        "Discovery refresh dispatched work or changed unrelated sessions",
    )
    fresh = None
    preceding_ns = after_ns
    exchanges = set()
    for method in ("initialize", "tools/list"):
        matched = [r for r in messages if r.get("method") == method]
        require(len(matched) == 1, "Missing unique discovery request")
        request = matched[0]
        exchange = request.get("exchange")
        require(bool(exchange) and exchange not in exchanges, "Discovery reused an exchange")
        exchanges.add(exchange)
        responses = [
            r for r in evidence if r.get("event") == "response" and r.get("exchange") == exchange
        ]
        settled = [
            r for r in evidence if r.get("event") == "settled" and r.get("exchange") == exchange
        ]
        require(len(responses) == len(settled) == 1, "Missing unique discovery response/settlement")
        response, end = responses[0], settled[0]
        require(
            preceding_ns
            <= request.get("time_ns", 0)
            < response.get("time_ns", 0)
            <= end.get("time_ns", 0)
            and response.get("method") == end.get("method") == "POST"
            and response.get("status") == 200
            and end.get("sessions") == expected_count
            and end.get("active") is False
            and end.get("poisoned") is False,
            "Discovery response is not a successful idle exchange",
        )
        if method == "initialize":
            fresh = response.get("session")
            require(
                isinstance(fresh, str)
                and bool(fresh)
                and fresh not in previous
                and request.get("session") is None
                and end.get("session") is None,
                "Discovery did not establish a fresh MCP identity",
            )
        else:
            require(
                request.get("session") == response.get("session") == end.get("session") == fresh,
                "Tool listing is not owned by the fresh MCP session",
            )
        preceding_ns = end["time_ns"]
    if not isinstance(fresh, str):
        raise RuntimeError("Discovery has no session identity")
    return fresh


def gateway_source_hashes(openclaw: Path) -> dict[str, str]:
    """Record the pinned host sources that govern discovery and allowlist refusal."""
    return {
        name: hashlib.sha256((openclaw / "dist" / name).read_bytes()).hexdigest()
        for name in (
            "agents/agent-bundle-mcp-runtime.js",
            "agent-bundle-mcp-manager-api-CCU0OWCp.mjs",
            "builtin-openclaw-dCw2mRD9.mjs",
        )
    }


def verify_idle_expiry(evidence, boot, expired, survivor, observed) -> dict[str, Any]:
    """Require timer retirement to drain only the idle session while active work survives."""
    stages = []
    for event in ("idle_expiry_armed", "idle_expiry_started", "idle_expiry_finished"):
        matches = [r for r in evidence if r.get("event") == event]
        require(len(matches) == 1, "Missing unique idle expiry stage")
        stages.append(matches[0])
    armed, start, end = stages
    require(
        bool(boot)
        and bool(expired)
        and bool(survivor)
        and expired != survivor
        and all(r.get("boot") == boot for r in evidence)
        and observed.get("requested_ns", 0) > 0
        and observed["requested_ns"]
        <= armed.get("time_ns", 0)
        < start.get("time_ns", 0)
        <= end.get("time_ns", 0)
        <= observed.get("observed_ns", 0)
        and armed.get("default_seconds") == end.get("restored_seconds") == 900
        and armed.get("seconds") == 2
        and start.get("session") == end.get("session") == expired
        and start.get("from_sweeper") is True
        and start.get("idle_seconds", 0) >= 2
        and isinstance(start.get("active_idle_seconds"), (int, float))
        and start["active_idle_seconds"] >= 2
        and start.get("active_session") == end.get("active_session") == survivor
        and start.get("request_ids") == 0
        and isinstance(start.get("get_active"), bool)
        and start.get("requests") == int(start["get_active"])
        and start.get("sdk_running") is True
        and end.get("completed") is True
        and end.get("sdk_finished") is True
        and end.get("sdk_terminated") is True
        and end.get("session_registered") is False
        and end.get("sdk_streams") == end.get("request_ids") == end.get("requests") == 0
        and end.get("active") is True
        and end.get("poisoned") is False
        and len(end.get("sessions", [])) == 1,
        "Idle expiry did not preserve the active owner and drain the expired registration",
    )
    live = end["sessions"][0]
    require(
        live.get("session") == survivor
        and live.get("initialized") is True
        and live.get("closing") is False
        and live.get("sdk_running") is True
        and live.get("sdk_terminated") is False
        and live.get("request_ids") == 1
        and all(
            observed.get(k) is True
            for k in (
                "same_gateway",
                "same_service",
                "owner_unchanged",
                "compiler_survived",
                "sentinel_preserved",
            )
        )
        and not any(
            r.get("event") in {"request", "delete_requested", "retired", "binding_started"}
            for r in evidence
        ),
        "Idle expiry disturbed active work or required client-directed retirement",
    )
    return {
        "default_idle_seconds": 900,
        "fixture_idle_seconds": 2,
        "expired_adapter_records": 0,
        "remaining_adapter_records": 1,
        "expired_sdk_task_finished": True,
        "active_session_preserved": True,
    }


def verify_default_idle_expiry(evidence, boot, expired, survivor) -> dict[str, Any]:
    """Require the unchanged idle policy to expire one session after actual elapsed time."""
    stages = []
    for event in ("idle_expiry_armed", "idle_expiry_started", "idle_expiry_finished"):
        matches = [r for r in evidence if r.get("event") == event]
        require(len(matches) == 1, "Missing unique default idle expiry stage")
        stages.append(matches[0])
    armed, start, end = stages
    elapsed = (start.get("monotonic_ns", 0) - armed.get("monotonic_ns", 0)) / 1e9
    initial_age = armed.get("idle_ages", {}).get(expired, -1)
    require(
        bool(boot)
        and bool(expired)
        and bool(survivor)
        and expired != survivor
        and all(r.get("boot") == boot for r in evidence)
        and armed.get("accelerated") is False
        and armed.get("default_seconds")
        == armed.get("seconds")
        == start.get("effective_seconds")
        == end.get("restored_seconds")
        == 900
        and 0 <= initial_age < 60
        and elapsed >= 900 - initial_age - 0.1
        and 0 < armed.get("time_ns", 0) < start.get("time_ns", 0) <= end.get("time_ns", 0)
        and start.get("from_sweeper") is True
        and start.get("idle_seconds", 0) >= 900
        and start.get("session") == end.get("session") == expired
        and start.get("active_session") is None
        and end.get("active_session") is None
        and start.get("request_ids") == 0
        and isinstance(start.get("get_active"), bool)
        and start.get("requests") == int(start["get_active"])
        and start.get("sdk_running") is True
        and end.get("completed") is True
        and end.get("sdk_finished") is True
        and end.get("sdk_terminated") is True
        and end.get("session_registered") is False
        and end.get("sdk_streams") == end.get("request_ids") == end.get("requests") == 0,
        "Default idle retirement lacks unchanged-policy elapsed-time or SDK-drain evidence",
    )
    verify_registry({**end, "event": "registry_snapshot"}, {survivor}, boot, start["time_ns"])
    messages = [r for r in evidence if r.get("event") == "request"]
    calls = [r for r in messages if r.get("method") == "tools/call"]
    bindings = [r for r in evidence if r.get("event") == "binding_started"]
    controls = [r for r in messages if r.get("method") == "notifications/cancelled"]
    for control in controls:
        replies = [
            r
            for r in evidence
            if r.get("event") == "response" and r.get("exchange") == control.get("exchange")
        ]
        ends = [
            r
            for r in evidence
            if r.get("event") == "settled" and r.get("exchange") == control.get("exchange")
        ]
        require(len(replies) == len(ends) == 1, "Idle control notification lacks unique settlement")
        reply, settled = replies[0], ends[0]
        require(
            bool(control.get("exchange"))
            and bool(control.get("target"))
            and control.get("session") == reply.get("session") == settled.get("session")
            and control.get("session") in {expired, survivor}
            and not any(
                call.get("session") == control.get("session")
                and call.get("request") == control.get("target")
                for call in calls
            )
            and reply.get("method") == settled.get("method") == "POST"
            and reply.get("status") == 202
            and armed["time_ns"]
            < control.get("time_ns", 0)
            < reply.get("time_ns", 0)
            <= settled.get("time_ns", 0)
            < start["time_ns"],
            "Control notification was not accepted or targeted an observed workload call",
        )
    require(
        len(calls) == len(bindings) == DEFAULT_IDLE_KEEPALIVES
        and len(messages) == len(calls) + len(controls)
        and len({r.get("exchange") for r in calls}) == DEFAULT_IDLE_KEEPALIVES
        and all(
            r.get("session") == survivor
            and armed["time_ns"] < r.get("time_ns", 0) < start["time_ns"]
            for r in [*calls, *bindings]
        )
        and not any(r.get("event") in {"delete_requested", "retired"} for r in evidence),
        "Default idle wait dispatched unexpected work or client-directed retirement",
    )
    return {
        "idle_seconds": 900,
        "observed_idle_seconds": start["idle_seconds"],
        "observation_seconds": elapsed,
        "threshold_modified": False,
        "keepalive_calls": len(calls),
        "control_notifications": len(controls),
        "expired_sdk_task_finished": True,
        "remaining_adapter_records": 1,
    }


def verify_unavailable_turn(status: int, payload, observed) -> None:
    """Require a catalog refusal while no service process or listener can execute work."""
    require(
        status == 500
        and payload == {"error": {"message": "internal error", "type": "api_error"}}
        and observed.get("service_not_started") is True
        and observed.get("port_closed_before") is True
        and observed.get("port_closed_after") is True
        and observed.get("owner_absent") is True
        and observed.get("transport_absent") is True
        and observed.get("provider_seen") is False
        and observed.get("catalog_error") is True,
        "Turn does not establish refusal before the service is available",
    )


def verify_final_shutdown(evidence, case: str, exit_code: int | None) -> None:
    """Require drained service state and successful process exit before reporting cleanup."""
    final = [r for r in evidence if r.get("event") == "shutdown"]
    require(
        exit_code == 0
        and [r.get("poisoned") for r in final]
        == (
            [False, True, False]
            if case == "lifecycle"
            else [True, False]
            if case == "docker-disconnect"
            else [False]
        )
        and all(r.get("sessions") == 0 and r.get("active") is False for r in final),
        "Final service shutdown was not clean",
    )


def qualify(args: argparse.Namespace) -> dict[str, Any]:
    case = getattr(args, "case", "lifecycle")
    counts = {
        "lifecycle": LIFECYCLE_DISPATCHES,
        "registry": REGISTRY_DISPATCHES,
        "unavailable": UNAVAILABLE_DISPATCHES,
        "idle": IDLE_DISPATCHES,
        "idle-default": DEFAULT_IDLE_DISPATCHES,
        "docker-disconnect": DOCKER_DISCONNECT_DISPATCHES,
    }
    require(case in counts, "Unknown qualification case")
    dispatches = counts[case]
    root = args.root.resolve()
    root.mkdir(parents=True, exist_ok=False)
    repo = Path(__file__).resolve().parents[2]
    config = json.loads(args.config.read_text(encoding="utf-8-sig"))
    require(config["gateway"]["bind"] == "loopback", "Expected a loopback template")
    require(config["tools"]["allow"] == ["bicep__bicep_validate"], "Unexpected tool policy")
    endpoint = config["mcp"]["servers"]["bicep"]
    require(endpoint["transport"] == "streamable-http", "Expected HTTP service")
    provider_url = urlparse(config["models"]["providers"]["qualification"]["baseUrl"])
    service_url = urlparse(endpoint["url"])
    require(
        provider_url.hostname == service_url.hostname == "127.0.0.1"
        and provider_url.scheme == service_url.scheme == "http",
        "All fixture endpoints must be loopback HTTP",
    )
    ports = [config["gateway"]["port"], provider_url.port, service_url.port]
    require(len(set(ports)) == 3 and all(ports), "Expected three distinct explicit ports")
    for port in ports:
        with socket.socket() as probe:
            require(probe.connect_ex(("127.0.0.1", port)) != 0, "Fixture port already in use")
    for name in ("home", "state", "workspace", "temp", "owner"):
        (root / name).mkdir()
    config["agents"]["defaults"]["workspace"] = str(root / "workspace")
    config["gateway"]["auth"]["token"] = uuid.uuid4().hex + uuid.uuid4().hex
    service_token = uuid.uuid4().hex + uuid.uuid4().hex
    endpoint["headers"]["Authorization"] = "Bearer " + service_token
    config_path = root / "state" / "openclaw.json"
    config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")
    (root / "token").write_text(service_token, encoding="ascii")
    env = {k: v for k, v in os.environ.items() if not k.startswith("OPENCLAW_")}
    env.update(
        OPENCLAW_STATE_DIR=str(root / "state"),
        OPENCLAW_CONFIG_PATH=str(config_path),
        OPENCLAW_HOME=str(root / "home"),
        OPENCLAW_SKIP_CHANNELS="1",
        OPENCLAW_NO_RESPAWN="1",
        NODE_DISABLE_COMPILE_CACHE="1",
        HOME=str(root / "home"),
        USERPROFILE=str(root / "home"),
        LOCALAPPDATA=str(root / "home" / "AppData" / "Local"),
        TEMP=str(root / "temp"),
        TMP=str(root / "temp"),
    )
    transport = root / "transport.jsonl"
    provider = root / "provider.jsonl"
    stop_file = root / "stop-service"
    crash_file = root / "crash-service"
    idle_expiry_file = root / "idle-expiry"
    drop_file = root / "drop-result"
    refusal_file = root / "refuse-cleanup"
    completed_refusal_file = root / "refuse-completed-cleanup"
    owner_file = root / "owner" / "owner"
    image = resolve_image(args.image)
    digest = hashlib.sha256(args.bicep_config.read_bytes()).hexdigest()
    processes: list[subprocess.Popen] = []
    report: dict[str, Any] = {"case": case, "complete_matrix": False, "image": image}
    with ExitStack() as stack:
        docker_fault = DockerConnectionFault(root, stack) if case == "docker-disconnect" else None
        retained_container = None

        def start(name: str, command: list[str], environment=None):
            log = stack.enter_context((root / (name + ".log")).open("ab"))
            process = subprocess.Popen(
                command,
                cwd=repo,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
            )
            processes.append(process)
            return process

        def gateway():
            return start(
                "gateway", ["node", str(args.openclaw / "openclaw.mjs"), "gateway", "run"], env
            )

        def service():
            stop_file.unlink(missing_ok=True)
            crash_file.unlink(missing_ok=True)
            return start(
                "service",
                [
                    shutil.which("uv") or "uv",
                    "run",
                    "--script",
                    str(repo / "tests/fixtures/openclaw_http_observer.py"),
                    "--prototype",
                    str(repo / "samples/experimental/openclaw_bicep/server.py"),
                    "--config",
                    str(args.bicep_config.resolve()),
                    "--state-dir",
                    str(root / "owner"),
                    "--token-file",
                    str(root / "token"),
                    "--image",
                    image,
                    "--port",
                    str(service_url.port),
                    "--evidence",
                    str(transport),
                    "--stop-file",
                    str(stop_file),
                    "--drop-result-file",
                    str(drop_file),
                    "--crash-file",
                    str(crash_file),
                    "--refuse-cleanup-file",
                    str(refusal_file),
                    "--refuse-completed-cleanup-file",
                    str(completed_refusal_file),
                    *(["--observe-docker-removal"] if docker_fault else []),
                    *(["--idle-expiry-file", str(idle_expiry_file)] if case == "idle" else []),
                    *(
                        ["--default-idle-expiry-file", str(idle_expiry_file)]
                        if case == "idle-default"
                        else []
                    ),
                ],
                docker_fault.environment if docker_fault else None,
            )

        def ready(port, path, token, process):
            require(
                process.poll() is None, "Fixture exited before readiness; inspect its private log"
            )
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
            try:
                connection.request("GET", path, headers={"Authorization": "Bearer " + token})
                response = connection.getresponse()
                response.read()
                return response.status == 200
            except (OSError, http.client.HTTPException):
                return False
            finally:
                connection.close()

        def service_ready(process):
            wait_for(
                lambda: ready(service_url.port, "/ready", service_token, process),
                "Service not ready",
            )

        def gateway_ready(process):
            wait_for(
                lambda: ready(ports[0], "/health", config["gateway"]["auth"]["token"], process),
                "Gateway not ready",
                180,
            )

        names = ["lifecycle-" + uuid.uuid4().hex for _ in range(2)]
        expected_calls = 0

        def begin_turn(index: int, case: str):
            turn_id = uuid.uuid4().hex
            connection = http.client.HTTPConnection("127.0.0.1", ports[0], timeout=180)
            try:
                connection.request(
                    "POST",
                    "/v1/chat/completions",
                    json.dumps(
                        {
                            "model": "openclaw",
                            "user": names[index],
                            "messages": [
                                {
                                    "role": "user",
                                    "content": f"qualification {case} qualification-turn={turn_id}",
                                }
                            ],
                        }
                    ),
                    {
                        "Content-Type": "application/json",
                        "Authorization": "Bearer " + config["gateway"]["auth"]["token"],
                    },
                )
            except BaseException:
                connection.close()
                raise
            return connection, turn_id

        def turn(index: int, *, loss=False):
            nonlocal expected_calls
            before = len(records(transport))
            connection, turn_id = begin_turn(index, "valid")
            try:
                response = connection.getresponse()
                response.read()
                require(response.status == 200, "Gateway turn failed")
            finally:
                connection.close()
            result = projected_result(records(provider), turn_id, "valid")
            evidence = records(transport)[before:]
            if loss:
                verified = verify_loss(evidence, result)
            else:
                verified = verify_outcome(result, "valid", digest, image)
            calls = requests(evidence)
            require(len(calls) == 1 and calls[0].get("session"), "Expected one MCP dispatch")
            expected_calls += 1
            require(
                len(requests(records(transport))) == expected_calls,
                "Unexpected background tool replay",
            )
            require(not owned(), "Owned resource survived a settled turn")
            return calls[0]["session"], verified

        def owned():
            scope = "openclaw-bicep-" + owner_file.read_text(encoding="ascii").strip()
            return docker("ps", "-aq", "--filter", f"label=maf-sandbox.scope={scope}").split()

        def snapshot_registry(expected, boot, owner_bytes):
            current_service = service_process
            if current_service is None:
                raise RuntimeError("Registry observation requires a started service")
            before = len(records(transport))
            requested_ns = time.time_ns()
            require(
                ready(service_url.port, "/ready", service_token, current_service),
                "Registry service lost readiness",
            )
            snapshots = wait_for(
                lambda: [
                    r for r in records(transport)[before:] if r.get("event") == "registry_snapshot"
                ],
                "Missing registry snapshot",
                10,
            )
            require(len(snapshots) == 1, "Repeated registry snapshot")
            verified = verify_registry(snapshots[0], set(expected), boot, requested_ns)
            require(
                current_service.poll() is None
                and gateway_process.poll() is None
                and owner_file.read_bytes() == owner_bytes
                and not owned()
                and docker("ps", "-q", "--filter", f"id={sentinel}"),
                "Registry observation found changed ownership, processes or container isolation",
            )
            return verified

        sentinel = None
        service_process = None
        try:
            start(
                "provider",
                [
                    sys.executable,
                    str(repo / "tests/fixtures/openclaw_gateway_provider.py"),
                    "--port",
                    str(provider_url.port),
                    "--evidence",
                    str(provider),
                ],
            )
            if case != "unavailable":
                service_process = service()
                service_ready(service_process)
            gateway_process = gateway()
            gateway_ready(gateway_process)
            baseline_args = argparse.Namespace(
                config=config_path,
                owner=root / "owner",
                bicep_config=args.bicep_config,
                evidence=provider,
                transport_evidence=transport,
                openclaw=args.openclaw,
                image=image,
            )
            sentinel = docker(
                "run",
                "-d",
                "--network",
                "none",
                "--label",
                f"maf-sandbox.scope=lifecycle-other-{uuid.uuid4().hex}",
                "--entrypoint",
                "/bin/sh",
                image,
                "-c",
                "sleep 1800",
            )

            def rpc(method, params):
                operation = subprocess.run(
                    [
                        "node",
                        str(args.openclaw / "openclaw.mjs"),
                        "gateway",
                        "call",
                        method,
                        "--expect-url",
                        f"ws://127.0.0.1:{ports[0]}",
                        "--timeout",
                        "60000",
                        "--params",
                        json.dumps(params),
                        "--json",
                    ],
                    cwd=repo,
                    env=env,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    timeout=90,
                )
                (root / ("rpc-" + uuid.uuid4().hex + ".log")).write_text(
                    operation.stdout + operation.stderr,
                    encoding="utf-8",
                )
                require(
                    operation.returncode == 0, "Gateway session RPC failed; inspect private RPC log"
                )
                value = json.loads(operation.stdout)
                require(isinstance(value, dict), "Gateway session RPC returned no object")
                return value

            registry_pair: tuple[str, str] | None = None
            if case == "unavailable":
                refused_turns = []
                unavailable_observations = []

                def port_closed():
                    with socket.socket() as probe:
                        return probe.connect_ex(("127.0.0.1", service_url.port)) != 0

                def catalog_failed(log, index):
                    return (
                        "No callable tools remain after resolving explicit tool allowlist" in log
                        and ("sessionKey=agent:main:openai-user:" + names[index]) in log
                        and "[openai-compat] chat completion failed:" in log
                    )

                for index in range(2):
                    closed_before = port_closed()
                    log_start = (root / "gateway.log").stat().st_size
                    connection, turn_id = begin_turn(index, "valid")
                    try:
                        response = connection.getresponse()
                        payload = json.loads(response.read())
                    finally:
                        connection.close()
                    log = (
                        (root / "gateway.log")
                        .read_bytes()[log_start:]
                        .decode("utf-8", errors="replace")
                    )
                    observed = {
                        "service_not_started": service_process is None,
                        "port_closed_before": closed_before,
                        "port_closed_after": port_closed(),
                        "owner_absent": not owner_file.exists(),
                        "transport_absent": not transport.exists(),
                        "provider_seen": any(r.get("turn") == turn_id for r in records(provider)),
                        "catalog_error": catalog_failed(log, index),
                    }
                    verify_unavailable_turn(response.status, payload, observed)
                    unavailable_observations.append(observed)
                    refused_turns.append(turn_id)
                print(
                    "Both Gateway sessions refused before the MCP service was started", flush=True
                )
                service_process = service()
                service_ready(service_process)
                report["environment"] = verify_environment(baseline_args)
                started = [r for r in records(transport) if r.get("event") == "startup"]
                ready_rows = [r for r in records(transport) if r.get("event") == "startup_ready"]
                require(
                    len(started) == len(ready_rows) == 1
                    and started[0].get("boot") == ready_rows[0].get("boot")
                    and ready_rows[0].get("owner_empty") is True,
                    "Service did not start once with an empty owner scope",
                )
                boot = started[0]["boot"]
                owner_before = owner_file.read_bytes()
                snapshots = [snapshot_registry([], boot, owner_before)]
                dormant_start = len(records(transport))
                time.sleep(DISCOVERY_COOLDOWN_SECONDS)
                require(
                    not any(
                        r.get("event")
                        in {"request", "binding_started", "retired", "delete_requested"}
                        for r in records(transport)[dormant_start:]
                    ),
                    "Gateway attempted discovery or replay without a new turn",
                )
                snapshots.append(snapshot_registry([], boot, owner_before))
                sessions = []
                for index in range(2):
                    before_refresh = len(records(transport))
                    refresh_ns = time.time_ns()
                    log_start = (root / "gateway.log").stat().st_size
                    connection, turn_id = begin_turn(index, "valid")
                    try:
                        response = connection.getresponse()
                        payload = json.loads(response.read())
                    finally:
                        connection.close()
                    log = (
                        (root / "gateway.log")
                        .read_bytes()[log_start:]
                        .decode("utf-8", errors="replace")
                    )
                    require(
                        response.status == 500
                        and payload == {"error": {"message": "internal error", "type": "api_error"}}
                        and catalog_failed(log, index)
                        and not any(r.get("turn") == turn_id for r in records(provider)),
                        "Startup recovery trigger did not fail before provider execution",
                    )
                    refused_turns.append(turn_id)

                    def refreshed():
                        evidence = records(transport)[before_refresh:]
                        listings = {
                            r.get("exchange")
                            for r in evidence
                            if r.get("event") == "request" and r.get("method") == "tools/list"
                        }
                        return any(
                            r.get("event") == "settled" and r.get("exchange") in listings
                            for r in evidence
                        )

                    wait_for(refreshed, "Gateway did not discover the newly available service", 30)
                    fresh = verify_discovery_refresh(
                        records(transport)[before_refresh:],
                        boot,
                        set(sessions),
                        refresh_ns,
                        expected_count=index + 1,
                    )
                    sessions.append(fresh)
                    snapshots.append(snapshot_registry(sessions, boot, owner_before))
                    verify_dispatch_count(records(transport), expected_calls)
                    require(turn(index)[0] == fresh, "New work missed the recovered MCP session")
                for index in range(2):
                    require(
                        turn(index)[0] == sessions[index], "Recovery did not preserve both sessions"
                    )
                snapshots.append(snapshot_registry(sessions, boot, owner_before))
                require(
                    not any(r.get("turn") in refused_turns for r in records(provider))
                    and len([r for r in records(transport) if r.get("event") == "startup"]) == 1,
                    "Startup recovery replayed a refused turn or restarted the service",
                )
                report["startup_discovery_recovery"] = {
                    "initial_refusals": unavailable_observations,
                    "gateway_sources": gateway_source_hashes(args.openclaw),
                    "service_starts": 1,
                    "gateway_restarted": False,
                    "cooldown_wait_seconds": DISCOVERY_COOLDOWN_SECONDS,
                    "refresh_trigger_statuses": [500, 500],
                    "fresh_sessions": len(sessions),
                    "refused_turns_replayed": False,
                    "owner_unchanged_after_start": True,
                    "snapshots": snapshots,
                }
                (root / "startup-discovery.json").write_text(
                    json.dumps(
                        {
                            "transport": records(transport),
                            "observed": report["startup_discovery_recovery"],
                        }
                    ),
                    encoding="utf-8",
                )
                print(
                    "Both sessions recovered discovery and reused their MCP identities without a Gateway restart or replay",
                    flush=True,
                )
            elif case == "docker-disconnect":
                if service_process is None or docker_fault is None:
                    raise RuntimeError(
                        "Docker disconnect requires a started private-context service"
                    )
                report["environment"] = verify_environment(baseline_args)
                owner_before = owner_file.read_bytes()
                sessions = [turn(index)[0] for index in range(2)]
                require(len(set(sessions)) == 2, "Docker disconnect requires distinct sessions")
                startup = [r for r in records(transport) if r.get("event") == "startup"][-1]
                boot = startup["boot"]
                snapshot_registry(sessions, boot, owner_before)
                before_active = len(records(transport))
                pending, active_turn = begin_turn(0, "cancel")
                try:

                    def compiling_for_disconnect():
                        candidates = owned()
                        if len(candidates) == 1 and "bicep" in docker(
                            "top", candidates[0], "-eo", "pid,comm"
                        ):
                            return candidates[0]
                        return None

                    short_id = wait_for(
                        compiling_for_disconnect, "No compiler before disconnect", 60
                    )
                    info = json.loads(docker("inspect", short_id))[0]
                    retained_container = info["Id"]
                    target = info["Name"].removeprefix("/")
                    active_calls = requests(records(transport)[before_active:])
                    require(
                        len(active_calls) == 1 and active_calls[0]["session"] == sessions[0],
                        "Docker disconnect lacks one selected active call",
                    )
                    active_call = active_calls[0]
                    expected_calls += 1
                    # Retain the exact compiler so natural completion cannot mimic recovery.
                    docker("pause", retained_container)
                    require(
                        json.loads(docker("inspect", retained_container))[0]["State"]["Paused"],
                        "Compiler was not paused before disconnect",
                    )
                    docker_fault.disconnect()
                    disconnected_ns = time.time_ns()
                    require(pending.sock is not None, "Active Gateway turn already ended")
                    abort_ns = time.time_ns()
                    pending.sock.shutdown(socket.SHUT_RDWR)
                    pending.close()
                    wait_for(
                        lambda: any(
                            r.get("event") == "settled"
                            and r.get("exchange") == active_call["exchange"]
                            and r.get("poisoned") is True
                            and r.get("active") is False
                            for r in records(transport)[before_active:]
                        ),
                        "Disconnected cleanup did not poison and settle the active call",
                        120,
                    )
                    probe = http.client.HTTPConnection("127.0.0.1", service_url.port, timeout=10)
                    try:
                        probe.request(
                            "GET", "/ready", headers={"Authorization": "Bearer " + service_token}
                        )
                        response = probe.getresponse()
                        response.read()
                        readiness_status = response.status
                    finally:
                        probe.close()
                    survivor = json.loads(docker("inspect", retained_container))[0]
                    observed = {
                        "container": retained_container,
                        "target": target,
                        "disconnected_ns": disconnected_ns,
                        "abort_ns": abort_ns,
                        "compiler_before": True,
                        "paused": survivor["State"]["Paused"],
                        "owned": [json.loads(docker("inspect", item))[0]["Id"] for item in owned()],
                        "readiness_status": readiness_status,
                        "same_gateway": gateway_process.poll() is None,
                        "same_service": service_process.poll() is None,
                        "owner_unchanged": owner_file.read_bytes() == owner_before,
                        "sentinel_preserved": bool(
                            docker("ps", "-q", "--filter", f"id={sentinel}")
                        ),
                        "observed_ns": time.time_ns(),
                    }
                    failure_evidence = records(transport)[before_active:]
                    failure_report = verify_docker_disconnect(
                        failure_evidence, active_call, observed
                    )
                    before_poisoned = len(records(transport))
                    poisoned_projections = []
                    for index in range(2):
                        blocked, blocked_turn = begin_turn(index, "valid")
                        try:
                            response = blocked.getresponse()
                            response.read()
                            require(response.status == 200, "Poisoned Gateway turn did not settle")
                        finally:
                            blocked.close()
                        poisoned_projections.append(
                            projected_result(records(provider), blocked_turn, "valid")
                        )
                    verify_poisoned_turns(
                        records(transport)[before_poisoned:], poisoned_projections, sessions, boot
                    )
                    expected_calls += 2
                    verify_dispatch_count(records(transport), expected_calls)
                    require(
                        json.loads(docker("inspect", retained_container))[0]["State"]["Paused"],
                        "Poisoned probes lost the retained compiler",
                    )
                    stop_file.touch()
                    require(service_process.wait(timeout=90) == 1, "Poisoned shutdown did not fail")
                    shutdown = [
                        r
                        for r in records(transport)
                        if r.get("event") == "shutdown" and r.get("boot") == boot
                    ]
                    require(
                        len(shutdown) == 1
                        and shutdown[0].get("sessions") == 0
                        and shutdown[0].get("active") is False
                        and shutdown[0].get("poisoned") is True,
                        "Disconnected service did not drain poisoned state",
                    )
                    require(
                        json.loads(docker("inspect", retained_container))[0]["State"]["Paused"],
                        "Disconnected shutdown removed the retained compiler",
                    )
                    require(
                        owner_file.read_bytes() == owner_before, "Poisoned shutdown changed owner"
                    )
                    print(
                        "Real Docker removal failed; both sessions stayed blocked and the compiler survived shutdown",
                        flush=True,
                    )
                    before_recovery = len(records(transport))
                    docker_fault.restore()
                    service_process = service()
                    service_ready(service_process)
                    recovery_rows = records(transport)[before_recovery:]
                    recovery_boot = verify_docker_recovery(recovery_rows, startup, target)
                    require(
                        not docker("ps", "-aq", "--filter", f"id={retained_container}")
                        and not owned(),
                        "Restored startup left owned resources",
                    )
                    require(owner_file.read_bytes() == owner_before, "Recovery changed owner bytes")
                    retained_container = None
                    fresh = [turn(index)[0] for index in range(2)]
                    require(
                        len(set(fresh)) == 2 and not set(fresh) & set(sessions),
                        "Recovered sessions reused previous identities",
                    )
                    final_registry = snapshot_registry(fresh, recovery_boot, owner_before)
                    require(
                        gateway_process.poll() is None
                        and not any(
                            r.get("turn") == active_turn and "tool_result" in r
                            for r in records(provider)
                        ),
                        "Recovery restarted Gateway or replayed aborted work",
                    )
                    failure_report.update(
                        gateway_turns_refused=2,
                        poisoned_shutdown_exit=1,
                        survivor_after_shutdown=True,
                        recovery_before_readiness=True,
                        fresh_sessions=2,
                        final_registry=final_registry,
                        same_gateway=True,
                        owner_unchanged=True,
                        replayed=False,
                    )
                    report["docker_disconnect"] = failure_report
                    (root / "docker-disconnect.json").write_text(
                        json.dumps(
                            {
                                "transport": records(transport)[before_active:],
                                "observed": observed,
                                "call": active_call,
                                "poisoned_projections": poisoned_projections,
                            }
                        ),
                        encoding="utf-8",
                    )
                    print(
                        "Restored Docker connection and service restart removed the retained compiler; new work succeeded without replay",
                        flush=True,
                    )
                finally:
                    pending.close()
            elif case == "idle-default":
                if service_process is None:
                    raise RuntimeError("Default idle expiry requires a started service")
                report["environment"] = verify_environment(baseline_args)
                owner_before = owner_file.read_bytes()
                sessions = [turn(index)[0] for index in range(2)]
                require(len(set(sessions)) == 2, "Default idle expiry requires distinct sessions")
                boot = records(transport)[-1]["boot"]
                snapshot_registry(sessions, boot, owner_before)
                before_expiry = len(records(transport))
                idle_expiry_file.touch()
                wait_for(
                    lambda: any(
                        r.get("event") == "idle_expiry_armed"
                        for r in records(transport)[before_expiry:]
                    ),
                    "Default idle observer was not armed",
                    10,
                )
                began = time.monotonic()
                for index in range(DEFAULT_IDLE_KEEPALIVES):
                    deadline = began + (index + 1) * 120
                    wait_for(
                        lambda: time.monotonic() >= deadline,
                        "Default idle keepalive scheduling failed",
                        125,
                    )
                    require(
                        not any(
                            r.get("event") == "idle_expiry_started"
                            for r in records(transport)[before_expiry:]
                        ),
                        "Idle expiry began before the bounded keepalive sequence completed",
                    )
                    require(turn(0)[0] == sessions[0], "Keepalive replaced the surviving session")
                    snapshot_registry(sessions, boot, owner_before)
                    print(
                        f"Default idle wait: {index + 1}/{DEFAULT_IDLE_KEEPALIVES} A calls completed; no B workload",
                        flush=True,
                    )
                wait_for(
                    lambda: any(
                        r.get("event") == "idle_expiry_finished"
                        for r in records(transport)[before_expiry:]
                    ),
                    "Default idle session did not expire after the 900-second interval",
                    180,
                )
                expiry_evidence = records(transport)[before_expiry:]
                report["default_idle_expiry"] = verify_default_idle_expiry(
                    expiry_evidence, boot, sessions[1], sessions[0]
                )
                snapshot_registry([sessions[0]], boot, owner_before)
                verify_dispatch_count(records(transport), expected_calls)
                require(turn(0)[0] == sessions[0], "Default expiry retired the surviving session")
                before_reconnect = len(records(transport))
                fresh = turn(1)[0]
                require(fresh not in sessions, "Default-expired identity was reused")
                reconnect = records(transport)[before_reconnect:]
                require(
                    any(
                        r.get("event") == "response"
                        and r.get("status") == 404
                        and r.get("session") == sessions[1]
                        for r in reconnect
                    )
                    and len(
                        [
                            r
                            for r in reconnect
                            if r.get("event") == "request" and r.get("method") == "initialize"
                        ]
                    )
                    == 1,
                    "Default expiry reconnect did not reject the stale identity and initialize once",
                )
                require(turn(1)[0] == fresh, "Default-expiry replacement identity was not reused")
                final_registry = snapshot_registry([sessions[0], fresh], boot, owner_before)
                report["default_idle_expiry"].update(
                    surviving_session_reused=True,
                    fresh_session_reused=True,
                    stale_session_status=404,
                    final_registry=final_registry,
                    same_gateway_and_service=True,
                    owner_unchanged=True,
                    replayed=False,
                )
                (root / "default-idle-expiry.json").write_text(
                    json.dumps(
                        {
                            "transport": records(transport)[before_expiry:],
                            "observed": report["default_idle_expiry"],
                        }
                    ),
                    encoding="utf-8",
                )
                print(
                    "Default 900-second idle expiry drained B; A remained callable and B reconnected without replay",
                    flush=True,
                )
            elif case == "idle":
                if service_process is None:
                    raise RuntimeError("Idle expiry requires a started service")
                report["environment"] = verify_environment(baseline_args)
                owner_before = owner_file.read_bytes()
                sessions = [turn(index)[0] for index in range(2)]
                require(len(set(sessions)) == 2, "Idle expiry requires distinct MCP sessions")
                boot = records(transport)[-1]["boot"]
                snapshot_registry(sessions, boot, owner_before)
                before_active = len(records(transport))
                pending, active_turn = begin_turn(0, "cancel")
                container = None
                try:

                    def compiling():
                        candidates = owned()
                        if len(candidates) == 1 and "bicep" in docker(
                            "top", candidates[0], "-eo", "pid,comm"
                        ):
                            return candidates[0]
                        return None

                    container = wait_for(compiling, "No compiler before idle expiry", 60)
                    active_calls = requests(records(transport)[before_active:])
                    require(
                        len(active_calls) == 1 and active_calls[0]["session"] == sessions[0],
                        "Idle expiry has no unique active call",
                    )
                    active_call = active_calls[0]
                    expected_calls += 1
                    # Both registrations must exceed the fixture threshold before its sweeper runs.
                    time.sleep(2.5)
                    before_expiry = len(records(transport))
                    requested_ns = time.time_ns()
                    idle_expiry_file.touch()
                    wait_for(
                        lambda: any(
                            r.get("event") == "idle_expiry_finished"
                            for r in records(transport)[before_expiry:]
                        ),
                        "Idle session did not finish expiry",
                        10,
                    )
                    observed = {
                        "requested_ns": requested_ns,
                        "same_gateway": gateway_process.poll() is None,
                        "same_service": service_process.poll() is None,
                        "owner_unchanged": owner_file.read_bytes() == owner_before,
                        "compiler_survived": owned() == [container]
                        and "bicep" in docker("top", container, "-eo", "pid,comm"),
                        "sentinel_preserved": bool(
                            docker("ps", "-q", "--filter", f"id={sentinel}")
                        ),
                        "observed_ns": time.time_ns(),
                    }
                    expiry_evidence = records(transport)[before_expiry:]
                    report["idle_expiry"] = verify_idle_expiry(
                        expiry_evidence, boot, sessions[1], sessions[0], observed
                    )
                    verify_dispatch_count(records(transport), expected_calls)
                    require(
                        pending.sock is not None, "Active Gateway turn ended before explicit abort"
                    )
                    abort_ns = time.time_ns()
                    pending.sock.shutdown(socket.SHUT_RDWR)
                    pending.close()
                    wait_for(
                        lambda: (
                            matching_cancel(
                                records(transport)[before_active:], active_call, after_ns=abort_ns
                            )
                            and not docker("ps", "-aq", "--filter", f"id={container}")
                        ),
                        "Explicit abort did not cancel and remove the exact compiler",
                        60,
                    )
                    require(not owned(), "Owned resources survived explicit abort")
                    require(turn(0)[0] == sessions[0], "Expiry retired the active session")
                    before_reconnect = len(records(transport))
                    fresh = turn(1)[0]
                    require(fresh not in sessions, "Expired MCP identity was reused")
                    reconnect = records(transport)[before_reconnect:]
                    stale = [
                        r
                        for r in reconnect
                        if r.get("event") == "response"
                        and r.get("status") == 404
                        and r.get("session") == sessions[1]
                    ]
                    initialized = [
                        r
                        for r in reconnect
                        if r.get("event") == "request" and r.get("method") == "initialize"
                    ]
                    require(
                        bool(stale) and len(initialized) == 1,
                        "Reconnect did not reject the stale identity and initialize once",
                    )
                    require(turn(1)[0] == fresh, "Reconnected idle session was not reused")
                    final_registry = snapshot_registry([sessions[0], fresh], boot, owner_before)
                    require(
                        not any(
                            r.get("turn") == active_turn and "tool_result" in r
                            for r in records(provider)
                        ),
                        "Aborted work was replayed or projected",
                    )
                    verify_dispatch_count(records(transport), expected_calls)
                    report["idle_expiry"].update(
                        explicit_abort_after_expiry=True,
                        fresh_session_reused=True,
                        active_session_reused=True,
                        stale_session_status=404,
                        replayed=False,
                        final_registry=final_registry,
                        same_gateway_and_service=True,
                        owner_unchanged=True,
                    )
                    (root / "idle-expiry.json").write_text(
                        json.dumps(
                            {
                                "transport": records(transport)[before_active:],
                                "observed": observed,
                                "call": active_call,
                                "abort_ns": abort_ns,
                            }
                        ),
                        encoding="utf-8",
                    )
                    print(
                        "Idle expiry drained one session, preserved active work, and reconnected new work without replay",
                        flush=True,
                    )
                finally:
                    pending.close()
            elif case == "registry":
                report["environment"] = verify_environment(baseline_args)
                owner_before = owner_file.read_bytes()
                new_active, replacement = (turn(index)[0] for index in range(2))
                require(new_active != replacement, "Registry sessions are not distinct")
                registry_pair = (new_active, replacement)
            else:
                if service_process is None:
                    raise RuntimeError("Lifecycle baseline requires a started service")
                report["two_session_baseline"] = check(baseline_args)
                verify_dispatch_count(records(transport), BASELINE_DISPATCHES)
                expected_calls = BASELINE_DISPATCHES
                old = [turn(index)[0] for index in range(2)]
                require(len(set(old)) == 2, "Lifecycle sessions are not distinct")
                drop_file.touch()
                _, report["unknown_outcome"] = turn(0, loss=True)
                print(
                    "Completed result withheld; Gateway reported transport failure without replay",
                    flush=True,
                )

                before = len(records(transport))
                endpoint["requestTimeoutMs"] += 1000
                config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")
                wait_for(
                    lambda: all(
                        any(
                            r.get("event") == "response"
                            and r.get("method") == "DELETE"
                            and r.get("session") == sid
                            and r.get("status") == 200
                            for r in records(transport)[before:]
                        )
                        for sid in old
                    ),
                    "Reload did not retire both idle MCP sessions",
                )
                require(gateway_process.poll() is None, "Configuration reload exited the Gateway")
                fresh = [turn(index)[0] for index in range(2)]
                require(
                    not set(fresh) & set(old) and len(set(fresh)) == 2,
                    "Reload reused retired sessions",
                )
                report["config_reload"] = {
                    "accepted_deletes": 2,
                    "fresh_sessions": 2,
                    "same_gateway_process": True,
                }
                print("Configuration reload retired both sessions and reconnected", flush=True)

                gateway_process.kill()
                gateway_process.wait(timeout=30)
                gateway_process = gateway()
                gateway_ready(gateway_process)
                restarted = [turn(index)[0] for index in range(2)]
                require(
                    not set(restarted) & set(fresh + old) and len(set(restarted)) == 2,
                    "Gateway restart reused MCP sessions",
                )
                report["gateway_restart"] = {
                    "mode": "idle-process-kill-and-relaunch",
                    "fresh_sessions": 2,
                }
                print("Gateway process restart reconnected both logical sessions", flush=True)

                probe = http.client.HTTPConnection("127.0.0.1", service_url.port, timeout=10)
                try:
                    probe.request(
                        "POST",
                        "/mcp",
                        json.dumps(
                            {
                                "jsonrpc": "2.0",
                                "id": 1,
                                "method": "initialize",
                                "params": {
                                    "protocolVersion": "2025-03-26",
                                    "capabilities": {},
                                    "clientInfo": {"name": "lifecycle-stale-probe", "version": "1"},
                                },
                            }
                        ),
                        {
                            "Authorization": "Bearer " + service_token,
                            "Content-Type": "application/json",
                            "Accept": "application/json, text/event-stream",
                        },
                    )
                    response = probe.getresponse()
                    response.read()
                    stale_session = response.getheader("mcp-session-id") or ""
                    require(
                        response.status == 200 and stale_session,
                        "Stale-session probe did not initialize",
                    )
                finally:
                    probe.close()
                owner_before = owner_file.read_bytes()
                stop_file.touch()
                require(service_process.wait(timeout=90) == 0, "Service shutdown failed")
                shutdown = [r for r in records(transport) if r.get("event") == "shutdown"]
                require(
                    len(shutdown) == 1
                    and shutdown[0].get("sessions") == 0
                    and shutdown[0].get("active") is False
                    and shutdown[0].get("poisoned") is False,
                    "Service did not drain cleanly",
                )
                require(not owned(), "Owned resource survived service shutdown")
                service_process = service()
                service_ready(service_process)
                require(
                    owner_file.read_bytes() == owner_before, "Service changed its recovery owner"
                )
                probe = http.client.HTTPConnection("127.0.0.1", service_url.port, timeout=10)
                try:
                    probe.request(
                        "GET",
                        "/mcp",
                        headers={
                            "Authorization": "Bearer " + service_token,
                            "mcp-session-id": stale_session,
                            "Accept": "text/event-stream",
                        },
                    )
                    response = probe.getresponse()
                    response.read()
                    require(
                        response.status == 404,
                        "Service accepted a session from its previous process",
                    )
                finally:
                    probe.close()
                startups = [r for r in records(transport) if r.get("event") == "startup"]
                require(
                    len(startups) == 2
                    and startups[0]["boot"] != startups[1]["boot"]
                    and startups[0]["source_hashes"] == startups[1]["source_hashes"]
                    and startups[0]["versions"] == startups[1]["versions"],
                    "Restart changed service code or dependency identity",
                )
                after_restart = [turn(index)[0] for index in range(2)]
                require(
                    not set(after_restart) & set(restarted + fresh + old)
                    and len(set(after_restart)) == 2,
                    "Service restart reused stale sessions",
                )
                report["service_restart"] = {
                    "retained_owner": True,
                    "stale_session_status": 404,
                    "clean_shutdown": True,
                    "fresh_sessions": 2,
                }
                before_crash = len(records(transport))
                crash_boot = startups[-1]["boot"]
                connection, crash_turn = begin_turn(0, "cancel")
                crashed_container = None
                try:

                    def active_compiler():
                        candidates = owned()
                        if len(candidates) == 1 and "bicep" in docker(
                            "top", candidates[0], "-eo", "pid,comm"
                        ):
                            return candidates[0]
                        return None

                    crashed_container = wait_for(
                        active_compiler, "No active compiler before crash", 60
                    )
                    crash_file.touch()
                    require(service_process.wait(timeout=30) == 86, "Service did not exit abruptly")
                    require(
                        owned() == [crashed_container]
                        and "bicep" in docker("top", crashed_container, "-eo", "pid,comm"),
                        "Compiler container did not survive service death",
                    )
                    survivor_observed = time.time_ns()
                    # Freeze the orphan so natural completion cannot masquerade as recovery.
                    docker("pause", crashed_container)
                    response = connection.getresponse()
                    response.read()
                    require(response.status == 200, "Gateway did not settle the crashed turn")
                    projected = projected_result(records(provider), crash_turn, "cancel")
                    crash_evidence = records(transport)[before_crash:]
                    report["active_service_crash"] = verify_crash(crash_evidence, projected)
                    expected_calls += 1
                    verify_dispatch_count(records(transport), expected_calls)
                    require(
                        requests(crash_evidence)[0]["session"] == after_restart[0]
                        and requests(crash_evidence)[0]["boot"] == crash_boot,
                        "Crash affected another session or service process",
                    )
                    require(
                        owner_file.read_bytes() == owner_before, "Crash changed retained ownership"
                    )
                    require(
                        docker("ps", "-q", "--filter", f"id={sentinel}"),
                        "Crash removed the unrelated owner",
                    )
                    require(
                        docker("inspect", "--format", "{{.State.Paused}}", crashed_container)
                        == "true"
                        and owned() == [crashed_container],
                        "Orphan did not remain frozen until replacement startup",
                    )
                    before_refusal = len(records(transport))
                    refusal_file.write_text(crashed_container, encoding="ascii")
                    service_process = service()
                    exit_code = service_process.wait(timeout=90)
                    refused_startups = [
                        r
                        for r in records(transport)[before_refusal:]
                        if r.get("event") == "startup"
                    ]
                    require(len(refused_startups) == 1, "Missing refused replacement startup")
                    refused_startup = refused_startups[0]
                    require(
                        refused_startup["boot"] != crash_boot
                        and refused_startup["source_hashes"] == startups[-1]["source_hashes"]
                        and refused_startup["versions"] == startups[-1]["versions"],
                        "Refused startup changed code or dependencies",
                    )
                    refused_projections = []
                    for index in range(2):
                        refused_connection, refused_turn = begin_turn(index, "valid")
                        try:
                            response = refused_connection.getresponse()
                            response.read()
                            require(response.status == 200, "Gateway did not settle refused work")
                        finally:
                            refused_connection.close()
                        refused_projections.append(
                            projected_result(records(provider), refused_turn, "valid")
                        )
                    with socket.socket() as probe:
                        listener_closed = probe.connect_ex(("127.0.0.1", service_url.port)) in {
                            errno.ECONNREFUSED,
                            10061,
                        }
                    refused_observation = {
                        "boot": refused_startup["boot"],
                        "container": crashed_container,
                        "exit_code": exit_code,
                        "paused": docker(
                            "inspect", "--format", "{{.State.Paused}}", crashed_container
                        )
                        == "true",
                        "sole_owned": owned() == [crashed_container],
                        "owner_unchanged": owner_file.read_bytes() == owner_before,
                        "listener_closed": listener_closed,
                        "sentinel_preserved": bool(
                            docker("ps", "-q", "--filter", f"id={sentinel}")
                        ),
                        "time_ns": time.time_ns(),
                    }
                    refusal_evidence = records(transport)[before_refusal:]
                    report["startup_cleanup_refusal"] = verify_cleanup_refusal(
                        refusal_evidence,
                        refused_startup["boot"],
                        crashed_container,
                        refused_observation,
                        refused_projections,
                    )
                    verify_dispatch_count(records(transport), expected_calls)
                    (root / "cleanup-refusal.json").write_text(
                        json.dumps(
                            {"transport": refusal_evidence, "observed": refused_observation}
                        ),
                        encoding="utf-8",
                    )
                    print(
                        "Unconfirmed startup cleanup refused readiness and both Gateway turns",
                        flush=True,
                    )
                    refusal_file.unlink()
                    service_process = service()
                    recovery_evidence = []

                    def recovered():
                        require(service_process.poll() is None, "Replacement service exited")
                        started = [
                            r
                            for r in records(transport)[before_crash:]
                            if r.get("event") == "startup_ready"
                        ]
                        if not started:
                            return False
                        require(
                            len(started) == 1 and started[0].get("owner_empty") is True,
                            "Replacement advertised readiness before owned cleanup",
                        )
                        require(
                            not docker("ps", "-aq", "--filter", f"id={crashed_container}")
                            and not owned(),
                            "Owned resource survived replacement startup",
                        )
                        recovery_evidence.extend(started)
                        recovery_evidence.append(
                            {
                                "event": "recovery_observed",
                                "container": crashed_container,
                                "absent": True,
                                "owner_empty": True,
                                "time_ns": time.time_ns(),
                            }
                        )
                        return True

                    wait_for(recovered, "Replacement did not reconcile retained resources")
                    service_ready(service_process)
                    startup = [r for r in records(transport) if r.get("event") == "startup"][-1]
                    require(
                        startup["boot"] not in {crash_boot, refused_startup["boot"]}
                        and startup["source_hashes"] == startups[-1]["source_hashes"]
                        and startup["versions"] == startups[-1]["versions"],
                        "Crash recovery changed service code or dependency identity",
                    )
                    recovery_evidence[1]["boot"] = startup["boot"]
                    recovery_evidence.append(
                        {
                            "event": "ready_observed",
                            "boot": startup["boot"],
                            "status": 200,
                            "time_ns": time.time_ns(),
                        }
                    )
                    verify_recovery(recovery_evidence, startup["boot"], crashed_container)
                    require(
                        owner_file.read_bytes() == owner_before,
                        "Recovery changed retained ownership",
                    )
                    # Rebuild the Gateway catalog after discovery failed against the stopped service.
                    gateway_process.kill()
                    gateway_process.wait(timeout=30)
                    gateway_process = gateway()
                    gateway_ready(gateway_process)
                    recovered_sessions = [turn(index)[0] for index in range(2)]
                    require(
                        len(set(recovered_sessions)) == 2
                        and not set(recovered_sessions)
                        & set(after_restart + restarted + fresh + old),
                        "Crash recovery reused previous MCP sessions",
                    )
                    report["startup_cleanup_refusal"].update(
                        fault_removed=True,
                        gateway_recovery="explicit-idle-process-restart",
                        retained_owner=True,
                        recovery_before_readiness=True,
                        fresh_sessions=2,
                    )
                    report["active_service_crash"].update(
                        compiler_survived=True,
                        survivor_paused_before_recovery=True,
                        retained_owner=True,
                        startup_cleanup=True,
                        cleanup_before_readiness=True,
                        fresh_sessions=2,
                        recovery_seconds=(recovery_evidence[1]["time_ns"] - survivor_observed)
                        / 1e9,
                    )
                    (root / "recovery.json").write_text(
                        json.dumps(recovery_evidence), encoding="utf-8"
                    )
                    print(
                        "Active-service crash left a compiler; retained-owner startup removed it",
                        flush=True,
                    )
                finally:
                    connection.close()
                    # Failed qualification must not leave its deliberately orphaned compiler behind.
                    if crashed_container and docker(
                        "ps", "-aq", "--filter", f"id={crashed_container}"
                    ):
                        docker("rm", "-f", crashed_container)
                before_completed = len(records(transport))
                completed_refusal_file.touch()
                failed_connection, failed_turn = begin_turn(0, "valid")
                try:
                    response = failed_connection.getresponse()
                    response.read()
                    require(response.status == 200, "Gateway did not settle cleanup failure")
                finally:
                    failed_connection.close()
                completed_evidence = records(transport)[before_completed:]
                failed_projection = projected_result(records(provider), failed_turn, "valid")
                report["completed_call_cleanup"] = verify_completed_cleanup(
                    completed_evidence,
                    failed_projection,
                    digest,
                    image,
                )
                expected_calls += 1
                require(
                    requests(completed_evidence)[0]["session"] == recovered_sessions[0],
                    "Completed cleanup affected another session",
                )
                require(
                    not owned() and owner_file.read_bytes() == owner_before,
                    "Completed cleanup changed ownership or left resources",
                )
                completed_refusal_file.unlink()
                before_poisoned = len(records(transport))
                poisoned_projections = []
                for index in range(2):
                    blocked_connection, blocked_turn = begin_turn(index, "valid")
                    try:
                        response = blocked_connection.getresponse()
                        response.read()
                        require(response.status == 200, "Gateway did not settle poisoned turn")
                    finally:
                        blocked_connection.close()
                    poisoned_projections.append(
                        projected_result(records(provider), blocked_turn, "valid")
                    )
                poisoned_evidence = records(transport)[before_poisoned:]
                verify_poisoned_turns(
                    poisoned_evidence, poisoned_projections, recovered_sessions, startup["boot"]
                )
                expected_calls += 2
                verify_dispatch_count(records(transport), expected_calls)
                probe = http.client.HTTPConnection("127.0.0.1", service_url.port, timeout=10)
                try:
                    probe.request(
                        "GET", "/ready", headers={"Authorization": "Bearer " + service_token}
                    )
                    response = probe.getresponse()
                    response.read()
                    require(response.status == 503, "Poisoned service advertised readiness")
                finally:
                    probe.close()
                require(
                    not owned()
                    and owner_file.read_bytes() == owner_before
                    and docker("ps", "-q", "--filter", f"id={sentinel}"),
                    "Poisoned service changed ownership or unrelated resources",
                )
                stop_file.touch()
                require(
                    service_process.wait(timeout=90) == 1,
                    "Poisoned observer did not report failure",
                )
                shutdown = [
                    r for r in records(transport)[before_completed:] if r.get("event") == "shutdown"
                ]
                require(
                    len(shutdown) == 1
                    and shutdown[0].get("boot") == startup["boot"]
                    and shutdown[0].get("poisoned") is True
                    and shutdown[0].get("active") is False
                    and shutdown[0].get("sessions") == 0,
                    "Poisoned service did not drain sessions",
                )
                before_clean_restart = len(records(transport))
                service_process = service()
                service_ready(service_process)
                new_start = [
                    r
                    for r in records(transport)[before_clean_restart:]
                    if r.get("event") == "startup"
                ]
                new_ready = [
                    r
                    for r in records(transport)[before_clean_restart:]
                    if r.get("event") == "startup_ready"
                ]
                require(
                    len(new_start) == len(new_ready) == 1
                    and new_start[0]["boot"] != startup["boot"]
                    and new_start[0]["source_hashes"] == startup["source_hashes"]
                    and new_start[0]["versions"] == startup["versions"]
                    and new_ready[0].get("boot") == new_start[0]["boot"]
                    and new_ready[0].get("owner_empty") is True
                    and owner_file.read_bytes() == owner_before
                    and not owned(),
                    "Completed-call recovery changed identity or advertised readiness before cleanup",
                )
                gateway_process.kill()
                gateway_process.wait(timeout=30)
                gateway_process = gateway()
                gateway_ready(gateway_process)
                final_sessions = [turn(index)[0] for index in range(2)]
                require(
                    len(set(final_sessions)) == 2
                    and not set(final_sessions)
                    & set(recovered_sessions + after_restart + restarted + fresh + old),
                    "Completed-call recovery reused stale MCP sessions",
                )
                report["completed_call_cleanup"].update(
                    fault_removed_before_probes=True,
                    gateway_turns_refused=2,
                    readiness_status=503,
                    retained_owner=True,
                    poisoned_shutdown_exit=1,
                    fresh_sessions=2,
                    gateway_recovery="explicit-idle-process-restart",
                )
                (root / "completed-cleanup.json").write_text(
                    json.dumps(
                        {
                            "transport": completed_evidence,
                            "projected": failed_projection,
                            "poisoned_transport": poisoned_evidence,
                            "poisoned_projections": poisoned_projections,
                        }
                    ),
                    encoding="utf-8",
                )
                print(
                    "Completed-call cleanup refusal suppressed success; both sessions stayed blocked until restart",
                    flush=True,
                )

                assert gateway_process is not None and service_process is not None

                targets = [
                    retirement_target(rpc("sessions.list", {"limit": 50}), name) for name in names
                ]
                before_retirement = len(records(transport))
                pending, retirement_turn = begin_turn(0, "cancel")
                try:
                    container = wait_for(
                        active_compiler, "No active compiler before selective retirement", 60
                    )
                    active_calls = requests(records(transport)[before_retirement:])
                    require(
                        len(active_calls) == 1 and active_calls[0]["session"] == final_sessions[0],
                        "Retirement did not start in the selected MCP session",
                    )
                    active_call = active_calls[0]
                    expected_calls += 1
                    retirement_reports = {}
                    sessions = list(final_sessions)
                    replacement = ""
                    for label, index in [("idle", 1), ("active", 0)]:
                        before_delete = len(records(transport))
                        requested_ns = time.time_ns()
                        deleted = rpc("sessions.delete", targets[index])
                        acknowledged_ns = time.time_ns()
                        require(
                            deleted.get("ok") is True
                            and deleted.get("deleted") is True
                            and deleted.get("key") == targets[index]["key"],
                            "Gateway did not acknowledge deletion of the selected session",
                        )
                        wait_for(
                            lambda: any(
                                r.get("event") == "retired" and r.get("session") == sessions[index]
                                for r in records(transport)[before_delete:]
                            ),
                            "Selected MCP registration did not finish retiring",
                            30,
                        )
                        absent = not docker("ps", "-aq", "--filter", f"id={container}")
                        observation = {
                            "requested_ns": requested_ns,
                            "acknowledged_ns": acknowledged_ns,
                            "deleted": True,
                            "same_gateway": gateway_process.poll() is None,
                            "same_service": service_process.poll() is None,
                            "owner_unchanged": owner_file.read_bytes() == owner_before,
                            "sentinel_preserved": bool(
                                docker("ps", "-q", "--filter", f"id={sentinel}")
                            ),
                            "exact_container_absent": absent,
                            "compiler_survived": not absent
                            and owned() == [container]
                            and "bicep" in docker("top", container, "-eo", "pid,comm"),
                        }
                        observation["observed_ns"] = time.time_ns()
                        evidence = records(transport)[before_delete:]
                        retirement_reports[label] = verify_retirement(
                            evidence,
                            sessions[index],
                            sessions[1 - index],
                            active_call,
                            observation,
                            active=label == "active",
                        )
                        (root / ("retirement-" + label + ".json")).write_text(
                            json.dumps(
                                {
                                    "transport": evidence,
                                    "call": active_call,
                                    "observed": observation,
                                }
                            ),
                            encoding="utf-8",
                        )
                        if label == "idle":
                            before_busy = len(records(transport))
                            busy_connection, busy_turn = begin_turn(1, "valid")
                            try:
                                response = busy_connection.getresponse()
                                response.read()
                                require(
                                    response.status == 200,
                                    "Replacement session did not settle busy",
                                )
                            finally:
                                busy_connection.close()
                            verify_busy(projected_result(records(provider), busy_turn, "valid"))
                            busy_calls = requests(records(transport)[before_busy:])
                            require(
                                len(busy_calls) == 1
                                and busy_calls[0].get("session")
                                and busy_calls[0]["session"] not in final_sessions,
                                "Deleted idle session reused its retired MCP identity",
                            )
                            replacement = busy_calls[0]["session"]
                            sessions[1] = replacement
                            expected_calls += 1
                            verify_dispatch_count(records(transport), expected_calls)
                            require(
                                owned() == [container]
                                and "bicep" in docker("top", container, "-eo", "pid,comm"),
                                "Idle deletion or replacement disturbed the active compiler",
                            )
                    response = pending.getresponse()
                    retirement_reports["aborted_turn_http_status"] = verify_deleted_turn(
                        response.status, json.loads(response.read())
                    )
                    require(
                        not any(
                            r.get("turn") == retirement_turn and "tool_result" in r
                            for r in records(provider)
                        ),
                        "Deleted active turn projected a workload result",
                    )
                    require(not owned(), "Owned resource survived active-session deletion")
                    require(
                        turn(1)[0] == replacement, "Active deletion retired the other MCP session"
                    )
                    new_active = turn(0)[0]
                    require(
                        new_active not in [*final_sessions, replacement],
                        "Deleted active session reused a retired MCP identity",
                    )
                    retirement_reports["fresh_calls"] = {
                        "other_session_reused": True,
                        "deleted_session_reconnected": True,
                    }
                    report["selective_retirement"] = retirement_reports
                    print(
                        "Idle and active Gateway session deletion retired only the selected MCP runtimes",
                        flush=True,
                    )
                finally:
                    pending.close()
                registry_pair = (new_active, replacement)
            if case in {"lifecycle", "registry"}:
                if registry_pair is None:
                    raise RuntimeError("Registry qualification has no baseline sessions")
                registry_start = len(records(transport))
                registry_boot = records(transport)[-1]["boot"]
                registry_sessions = list(registry_pair)
                registry_reports = []

                def inspect_registry(expected):
                    return snapshot_registry(expected, registry_boot, owner_before)

                registry_reports.append(inspect_registry(registry_sessions))
                for _ in range(REGISTRY_LIMIT - 2):
                    names.append("lifecycle-" + uuid.uuid4().hex)
                    session = turn(len(names) - 1)[0]
                    require(
                        session not in registry_sessions,
                        "Gateway shared a registry slot across logical sessions",
                    )
                    registry_sessions.append(session)
                    registry_reports.append(inspect_registry(registry_sessions))

                def retire_registry_target():
                    target = retirement_target(rpc("sessions.list", {"limit": 50}), names[0])
                    before_delete = len(records(transport))
                    requested_ns = time.time_ns()
                    deleted = rpc("sessions.delete", target)
                    require(
                        deleted.get("ok") is True
                        and deleted.get("deleted") is True
                        and deleted.get("key") == target["key"],
                        "Gateway did not delete the churn target",
                    )
                    wait_for(
                        lambda: any(
                            r.get("event") == "retired" and r.get("session") == registry_sessions[0]
                            for r in records(transport)[before_delete:]
                        ),
                        "Churn retirement did not finish",
                        30,
                    )
                    verify_churn_retirement(
                        records(transport)[before_delete:],
                        registry_boot,
                        registry_sessions[0],
                        requested_ns,
                    )
                    registry_reports.append(inspect_registry(registry_sessions[1:]))

                retired_sessions = set()
                for _ in range(CHURN_CYCLES):
                    retire_registry_target()
                    retired_sessions.add(registry_sessions[0])
                    fresh = turn(0)[0]
                    require(
                        fresh not in retired_sessions and fresh not in registry_sessions,
                        "Churn reused a retired or surviving MCP identity",
                    )
                    registry_sessions[0] = fresh
                    registry_reports.append(inspect_registry(registry_sessions))
                for index in range(1, REGISTRY_LIMIT):
                    require(
                        turn(index)[0] == registry_sessions[index],
                        "Registry churn retired a surviving session",
                    )
                registry_reports.append(inspect_registry(registry_sessions))
                before_overflow = len(records(transport))
                names.append("lifecycle-" + uuid.uuid4().hex)
                overflow_connection, overflow_turn = begin_turn(len(names) - 1, "valid")
                try:
                    response = overflow_connection.getresponse()
                    payload = json.loads(response.read())
                    require(
                        response.status == 500
                        and payload
                        == {"error": {"message": "internal error", "type": "api_error"}},
                        "Overflow turn did not return the pinned Gateway error",
                    )
                finally:
                    overflow_connection.close()
                overflow = verify_registry_refusal(
                    records(transport)[before_overflow:], registry_boot
                )
                require(
                    not any(
                        r.get("turn") == overflow_turn and "tool_result" in r
                        for r in records(provider)
                    ),
                    "Overflow projected a workload result",
                )
                verify_dispatch_count(records(transport), expected_calls)
                registry_reports.append(inspect_registry(registry_sessions))
                retire_registry_target()
                before_probe = len(records(transport))
                log_start = (root / "gateway.log").stat().st_size
                probe_connection, probe_turn = begin_turn(0, "valid")
                try:
                    response = probe_connection.getresponse()
                    payload = json.loads(response.read())
                finally:
                    probe_connection.close()
                log = (
                    (root / "gateway.log")
                    .read_bytes()[log_start:]
                    .decode("utf-8", errors="replace")
                )
                catalog_refusal = verify_catalog_refusal(
                    response.status,
                    payload,
                    records(transport)[before_probe:],
                    {
                        "boot": registry_boot,
                        "provider_seen": any(
                            r.get("turn") == probe_turn for r in records(provider)
                        ),
                        "gateway_catalog_error": "No callable tools remain after resolving explicit tool allowlist"
                        in log
                        and ("sessionKey=agent:main:openai-user:" + names[0]) in log
                        and "[openai-compat] chat completion failed:" in log,
                    },
                )
                registry_reports.append(inspect_registry(registry_sessions[1:]))
                verify_dispatch_count(records(transport), expected_calls)
                print("Waiting through the pinned cross-runtime startup cooldown", flush=True)
                dormant_start = len(records(transport))
                time.sleep(DISCOVERY_COOLDOWN_SECONDS)
                dormant_evidence = records(transport)[dormant_start:]
                require(
                    all(r.get("boot") == registry_boot for r in dormant_evidence)
                    and not any(
                        r.get("event") in {"binding_started", "retired", "delete_requested"}
                        or r.get("event") == "request"
                        and r.get("method") not in {"notifications/cancelled"}
                        for r in dormant_evidence
                    ),
                    "Gateway retried discovery or work without a new turn during cooldown",
                )
                registry_reports.append(inspect_registry(registry_sessions[1:]))
                before_refresh = len(records(transport))
                refresh_ns = time.time_ns()
                refresh_log_start = (root / "gateway.log").stat().st_size
                refresh_connection, refresh_turn = begin_turn(0, "valid")
                try:
                    response = refresh_connection.getresponse()
                    refresh_payload = json.loads(response.read())
                    require(
                        response.status == 500
                        and refresh_payload
                        == {"error": {"message": "internal error", "type": "api_error"}},
                        "Refresh-triggering turn did not return the stale-catalog error",
                    )
                finally:
                    refresh_connection.close()
                refresh_log = (
                    (root / "gateway.log")
                    .read_bytes()[refresh_log_start:]
                    .decode("utf-8", errors="replace")
                )
                require(
                    "No callable tools remain after resolving explicit tool allowlist"
                    in refresh_log
                    and ("sessionKey=agent:main:openai-user:" + names[0]) in refresh_log
                    and "[openai-compat] chat completion failed:" in refresh_log
                    and not any(r.get("turn") == refresh_turn for r in records(provider)),
                    "Refresh-triggering turn reached the provider or lacks a catalog error",
                )

                def refreshed():
                    evidence = records(transport)[before_refresh:]
                    listings = {
                        r.get("exchange")
                        for r in evidence
                        if r.get("event") == "request" and r.get("method") == "tools/list"
                    }
                    return any(
                        r.get("event") == "settled" and r.get("exchange") in listings
                        for r in evidence
                    )

                wait_for(refreshed, "Gateway did not refresh discovery after cooldown", 30)
                fresh = verify_discovery_refresh(
                    records(transport)[before_refresh:],
                    registry_boot,
                    set(registry_sessions) | retired_sessions,
                    refresh_ns,
                )
                registry_sessions[0] = fresh
                registry_reports.append(inspect_registry(registry_sessions))
                verify_dispatch_count(records(transport), expected_calls)
                require(turn(0)[0] == fresh, "New work did not use the recovered MCP session")
                for index in range(1, REGISTRY_LIMIT):
                    require(
                        turn(index)[0] == registry_sessions[index],
                        "Discovery recovery replaced an unaffected MCP session",
                    )
                require(
                    not any(
                        r.get("turn") in {overflow_turn, probe_turn, refresh_turn}
                        for r in records(provider)
                    ),
                    "A refused turn was replayed after discovery recovery",
                )
                registry_reports.append(inspect_registry(registry_sessions))
                discovery_recovery = {
                    "cooldown_wait_seconds": DISCOVERY_COOLDOWN_SECONDS,
                    "refresh_trigger_status": 500,
                    "refresh_trigger_provider_seen": False,
                    "initialization_attempts": 1,
                    "tool_listings": 1,
                    "new_turn_completed": True,
                    "surviving_sessions_reused_after_refusal": REGISTRY_LIMIT - 1,
                    "refused_turns_replayed": False,
                    "gateway_sources": gateway_source_hashes(args.openclaw),
                }
                report["registry_churn"] = {
                    "limit": REGISTRY_LIMIT,
                    "cycles": CHURN_CYCLES,
                    "overflow": overflow,
                    "post_refusal_recovery": catalog_refusal,
                    "delayed_discovery_recovery": discovery_recovery,
                    "snapshots": registry_reports,
                    "surviving_sessions_reused": REGISTRY_LIMIT - 1,
                    "same_gateway_and_service": True,
                    "owner_unchanged": True,
                    "owned_scope_empty": True,
                    "other_owner_preserved": True,
                }
                (root / "registry-churn.json").write_text(
                    json.dumps(
                        {
                            "transport": records(transport)[registry_start:],
                            "observed": report["registry_churn"],
                        }
                    ),
                    encoding="utf-8",
                )
                print(
                    "Gateway discovery recovered after cooldown and a refresh-triggering turn; new work and seven surviving sessions succeeded",
                    flush=True,
                )
            require(docker("ps", "-q", "--filter", f"id={sentinel}"), "Unrelated owner was removed")
            report["other_owner_preserved"] = True
            report["total_dispatches"] = verify_dispatch_count(records(transport), dispatches)
            report["remaining"] = [
                "broader idle-expiry timing races, other retirement triggers and prolonged/concurrent churn",
                "daemon-side removal failures, broader Docker outages and remaining interruption cases"
                if case == "docker-disconnect"
                else "Docker cleanup/daemon failures and remaining interruption cases",
                "other discovery failures, repeated backoff and concurrent discovery",
                "full real-host transport/MAF matrix",
            ]
        finally:
            if service_process is not None and service_process.poll() is None:
                stop_file.touch()
                try:
                    service_process.wait(timeout=90)
                except subprocess.TimeoutExpired:
                    service_process.kill()
            for process in reversed(processes):
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=30)
            if retained_container and docker("ps", "-aq", "--filter", f"id={retained_container}"):
                docker("rm", "-f", retained_container)
            if sentinel:
                docker("rm", "-f", sentinel)
        require(not owned(), "Owner resources remain after fixture shutdown")
        verify_final_shutdown(
            records(transport), case, service_process.returncode if service_process else None
        )
        verify_dispatch_count(records(transport), dispatches)
        for port in ports:
            with socket.socket() as probe:
                require(
                    probe.connect_ex(("127.0.0.1", port)) != 0, "Fixture listener survived shutdown"
                )
        report["final_service_shutdown"] = "clean"
    report["candidate"] = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=repo, text=True
    ).strip()
    report["dirty"] = bool(
        subprocess.check_output(["git", "status", "--porcelain"], cwd=repo, text=True).strip()
    )
    report["source_hashes"] = {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest()
        for p in [
            *sorted((repo / "samples/experimental/openclaw_bicep").glob("*.py")),
            *sorted(Path(__file__).parent.glob("openclaw_*.py")),
        ]
    }
    (
        root
        / (
            "docker-disconnect-report.json"
            if case == "docker-disconnect"
            else "default-idle-report.json"
            if case == "idle-default"
            else "idle-report.json"
            if case == "idle"
            else "startup-report.json"
            if case == "unavailable"
            else "registry-report.json"
            if case == "registry"
            else "lifecycle-report.json"
        )
    ).write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("root", "config", "bicep-config", "openclaw"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument(
        "--case",
        choices=[
            "lifecycle",
            "registry",
            "unavailable",
            "idle",
            "idle-default",
            "docker-disconnect",
        ],
        default="lifecycle",
    )
    print(json.dumps(qualify(parser.parse_args()), indent=2))


if __name__ == "__main__":
    main()
