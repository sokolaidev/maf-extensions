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
    verify_outcome,
)
from openclaw_http_observer import resolve_image

REGISTRY_LIMIT = 8
CHURN_CYCLES = 3
LIFECYCLE_DISPATCHES = 30 + (REGISTRY_LIMIT - 2) + CHURN_CYCLES + (REGISTRY_LIMIT - 1)


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
    attempts = [r for r in evidence if r.get("event") == "request"]
    require(
        bool(attempts)
        and all(r.get("method") == "initialize" for r in attempts)
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
        and not any(r.get("event") in {"request", "binding_started"} for r in evidence),
        "Churn retirement did not drain only the selected idle session",
    )


def qualify(args: argparse.Namespace) -> dict[str, Any]:
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
    drop_file = root / "drop-result"
    refusal_file = root / "refuse-cleanup"
    completed_refusal_file = root / "refuse-completed-cleanup"
    owner_file = root / "owner" / "owner"
    image = resolve_image(args.image)
    digest = hashlib.sha256(args.bicep_config.read_bytes()).hexdigest()
    processes: list[subprocess.Popen] = []
    report: dict[str, Any] = {"complete_matrix": False, "image": image}
    with ExitStack() as stack:

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
                ],
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
            report["two_session_baseline"] = check(baseline_args)
            verify_dispatch_count(records(transport), BASELINE_DISPATCHES)
            expected_calls = BASELINE_DISPATCHES
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
                not set(fresh) & set(old) and len(set(fresh)) == 2, "Reload reused retired sessions"
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
            require(owner_file.read_bytes() == owner_before, "Service changed its recovery owner")
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
                    response.status == 404, "Service accepted a session from its previous process"
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

                crashed_container = wait_for(active_compiler, "No active compiler before crash", 60)
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
                require(owner_file.read_bytes() == owner_before, "Crash changed retained ownership")
                require(
                    docker("ps", "-q", "--filter", f"id={sentinel}"),
                    "Crash removed the unrelated owner",
                )
                require(
                    docker("inspect", "--format", "{{.State.Paused}}", crashed_container) == "true"
                    and owned() == [crashed_container],
                    "Orphan did not remain frozen until replacement startup",
                )
                before_refusal = len(records(transport))
                refusal_file.write_text(crashed_container, encoding="ascii")
                service_process = service()
                exit_code = service_process.wait(timeout=90)
                refused_startups = [
                    r for r in records(transport)[before_refusal:] if r.get("event") == "startup"
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
                    "paused": docker("inspect", "--format", "{{.State.Paused}}", crashed_container)
                    == "true",
                    "sole_owned": owned() == [crashed_container],
                    "owner_unchanged": owner_file.read_bytes() == owner_before,
                    "listener_closed": listener_closed,
                    "sentinel_preserved": bool(docker("ps", "-q", "--filter", f"id={sentinel}")),
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
                    json.dumps({"transport": refusal_evidence, "observed": refused_observation}),
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
                    owner_file.read_bytes() == owner_before, "Recovery changed retained ownership"
                )
                # Rebuild the Gateway catalog after discovery failed against the stopped service.
                gateway_process.kill()
                gateway_process.wait(timeout=30)
                gateway_process = gateway()
                gateway_ready(gateway_process)
                recovered_sessions = [turn(index)[0] for index in range(2)]
                require(
                    len(set(recovered_sessions)) == 2
                    and not set(recovered_sessions) & set(after_restart + restarted + fresh + old),
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
                    recovery_seconds=(recovery_evidence[1]["time_ns"] - survivor_observed) / 1e9,
                )
                (root / "recovery.json").write_text(json.dumps(recovery_evidence), encoding="utf-8")
                print(
                    "Active-service crash left a compiler; retained-owner startup removed it",
                    flush=True,
                )
            finally:
                connection.close()
                # Failed qualification must not leave its deliberately orphaned compiler behind.
                if crashed_container and docker("ps", "-aq", "--filter", f"id={crashed_container}"):
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
                probe.request("GET", "/ready", headers={"Authorization": "Bearer " + service_token})
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
                service_process.wait(timeout=90) == 1, "Poisoned observer did not report failure"
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
                r for r in records(transport)[before_clean_restart:] if r.get("event") == "startup"
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
                            {"transport": evidence, "call": active_call, "observed": observation}
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
                                response.status == 200, "Replacement session did not settle busy"
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
                require(turn(1)[0] == replacement, "Active deletion retired the other MCP session")
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
            registry_start = len(records(transport))
            registry_boot = records(transport)[-1]["boot"]
            registry_sessions = [new_active, replacement]
            registry_reports = []

            def inspect_registry(expected):
                before = len(records(transport))
                requested_ns = time.time_ns()
                require(
                    ready(service_url.port, "/ready", service_token, service_process),
                    "Registry service lost readiness",
                )
                snapshots = wait_for(
                    lambda: [
                        r
                        for r in records(transport)[before:]
                        if r.get("event") == "registry_snapshot"
                    ],
                    "Missing registry snapshot",
                    10,
                )
                require(len(snapshots) == 1, "Repeated registry snapshot")
                verified = verify_registry(snapshots[0], set(expected), registry_boot, requested_ns)
                require(
                    service_process.poll() is None
                    and gateway_process.poll() is None
                    and owner_file.read_bytes() == owner_before
                    and not owned()
                    and docker("ps", "-q", "--filter", f"id={sentinel}"),
                    "Registry churn changed ownership, processes or container isolation",
                )
                return verified

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
            before_overflow = len(records(transport))
            names.append("lifecycle-" + uuid.uuid4().hex)
            overflow_connection, overflow_turn = begin_turn(len(names) - 1, "valid")
            try:
                response = overflow_connection.getresponse()
                payload = json.loads(response.read())
                require(
                    response.status == 500
                    and payload == {"error": {"message": "internal error", "type": "api_error"}},
                    "Overflow turn did not return the pinned Gateway error",
                )
            finally:
                overflow_connection.close()
            overflow = verify_registry_refusal(records(transport)[before_overflow:], registry_boot)
            require(
                not any(
                    r.get("turn") == overflow_turn and "tool_result" in r for r in records(provider)
                ),
                "Overflow projected a workload result",
            )
            verify_dispatch_count(records(transport), expected_calls)
            registry_reports.append(inspect_registry(registry_sessions))
            retired_sessions = set()
            for _ in range(CHURN_CYCLES):
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
                retired_sessions.add(registry_sessions[0])
                registry_reports.append(inspect_registry(registry_sessions[1:]))
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
            report["registry_churn"] = {
                "limit": REGISTRY_LIMIT,
                "cycles": CHURN_CYCLES,
                "overflow": overflow,
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
                "Eight Gateway sessions filled the registry; overflow refused and three churn cycles reclaimed SDK and adapter state",
                flush=True,
            )
            require(docker("ps", "-q", "--filter", f"id={sentinel}"), "Unrelated owner was removed")
            report["other_owner_preserved"] = True
            report["total_dispatches"] = verify_dispatch_count(
                records(transport), LIFECYCLE_DISPATCHES
            )
            report["remaining"] = [
                "other Gateway retirement triggers, idle expiry and prolonged/concurrent churn",
                "Docker cleanup/daemon failures and remaining interruption cases",
                "automatic Gateway discovery recovery",
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
            if sentinel:
                docker("rm", "-f", sentinel)
        require(not owned(), "Owner resources remain after fixture shutdown")
        final = [r for r in records(transport) if r.get("event") == "shutdown"]
        require(
            len(final) == 3
            and [r.get("poisoned") for r in final] == [False, True, False]
            and all(r.get("sessions") == 0 and r.get("active") is False for r in final),
            "Final service shutdown was not clean",
        )
        verify_dispatch_count(records(transport), LIFECYCLE_DISPATCHES)
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
    (root / "lifecycle-report.json").write_text(
        json.dumps(report, indent=2) + "\n", encoding="utf-8"
    )
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("root", "config", "bicep-config", "openclaw"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--image", required=True)
    print(json.dumps(qualify(parser.parse_args()), indent=2))


if __name__ == "__main__":
    main()
