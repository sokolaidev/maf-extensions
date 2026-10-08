"""Qualify two real Gateway sessions against an observed, independently supervised HTTP service."""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import socket
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any

from openclaw_gateway_check import docker
from openclaw_gateway_provider import SOURCES

BASELINE_DISPATCHES = 9


def require(condition: Any, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def records(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    data = path.read_bytes()
    require(len(data) <= 16 * 1024 * 1024, "Evidence exceeds 16 MiB; rotate before a new check")
    # A concurrent writer may not have finished the last line yet.
    return [json.loads(line) for line in data.split(b"\n")[:-1] if line]


def verify_dispatch_count(evidence: list[dict[str, Any]], expected: int) -> int:
    count = sum(r.get("event") == "request" and r.get("method") == "tools/call" for r in evidence)
    require(count == expected, f"Expected {expected} MCP dispatches, observed {count}")
    return count


def projected_result(evidence: list[dict[str, Any]], turn: str, case: str) -> dict[str, Any]:
    matching = [
        record for record in evidence if record.get("turn") == turn and "tool_result" in record
    ]
    require(len(matching) == 1, "Expected exactly one provider tool result for this turn")
    record = matching[0]
    require(record.get("scenario") == case, "Provider result belongs to another scenario")
    text = record["tool_result"]
    return json.loads(text[text.index("{") : text.rindex("}") + 1])


def verify_outcome(
    projected: dict[str, Any], case: str, config_digest: str, image: str
) -> dict[str, Any]:
    if case in ("denied", "helper"):
        require(
            projected.get("status") == "error" and "Unknown tool id:" in projected.get("error", ""),
            "Unauthorized tool was not refused",
        )
        return {"rejected": True}
    result = projected["result"]
    structured = result["details"]["structuredContent"]
    require(len(result["content"]) == 1, "Gateway duplicated the projected result")
    require(structured["cleanup"] == "confirmed", "Cleanup was not confirmed")
    require(structured["completed"] is (case != "incomplete"), "Wrong completion state")
    require(structured["verdict"] == (None if case == "incomplete" else case), "Wrong verdict")
    require(
        structured["status"] == ("incomplete" if case == "incomplete" else "ok"), "Wrong status"
    )
    canonical = json.dumps([["main.bicep", SOURCES[case]]], separators=(",", ":")).encode()
    require(
        structured["source_sha256"] == hashlib.sha256(canonical).hexdigest(),
        "Wrong source identity",
    )
    require(structured["config_sha256"] == config_digest, "Wrong compiler configuration")
    require(structured["image"] == image, "Wrong image identity")
    return {
        key: structured[key]
        for key in ("completed", "verdict", "status", "cleanup", "source_sha256")
    } | {"projected_content_items": 1}


def verify_busy(projected: dict[str, Any]) -> None:
    result = projected["result"]
    require(result.get("details", {}).get("status") == "error", "Busy did not return a tool error")
    require("busy" in json.dumps(result).lower(), "Expected the service busy error")
    require(
        "structuredContent" not in result.get("details", {}), "Busy fabricated a workload result"
    )


def matching_cancel(
    evidence: list[dict[str, Any]], call: dict[str, Any], *, after_ns: int = 0
) -> bool:
    if not call.get("session") or not call.get("request"):
        return False
    return any(
        record.get("event") == "request"
        and record.get("method") == "notifications/cancelled"
        and record.get("session") == call.get("session")
        and record.get("target") == call.get("request")
        and record.get("time_ns", 0) > max(call.get("time_ns", 0), after_ns)
        and bool(record.get("exchange"))
        and any(
            response.get("event") == "response"
            and response.get("exchange") == record["exchange"]
            and response.get("session") == record["session"]
            and response.get("status") == 202
            and response.get("time_ns", 0) > record["time_ns"]
            for response in evidence
        )
        for record in evidence
    )


def verify_environment(args: argparse.Namespace) -> dict[str, Any]:
    """Pin the host version, published dependencies and actually loaded service sources."""
    package = json.loads((args.openclaw / "package.json").read_text(encoding="utf-8"))
    require(package["version"] == "2026.9.7", "Requalify an OpenClaw version change separately")
    startup = [r for r in records(args.transport_evidence) if r.get("event") == "startup"]
    require(bool(startup), "No service startup evidence")
    expected = {
        "maf-sandbox": "0.46.0",
        "maf-sandbox-bicep": "0.22.0",
        "maf-sandbox-docker": "0.24.4",
        "mcp": "1.28.1",
        "agent-framework-core": "1.19.0",
        "uvicorn": "0.54.0",
    }
    require(startup[-1]["versions"] == expected, "Unexpected published service dependencies")
    root = Path(__file__).resolve().parents[2]
    expected_sources = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in [
            *sorted((root / "samples/experimental/openclaw_bicep").glob("*.py")),
            Path(__file__).with_name("openclaw_http_observer.py"),
        ]
    }
    require(
        startup[-1].get("source_hashes") == expected_sources,
        "Observer is running different source files",
    )
    return {"openclaw": package["version"], "versions": expected}


def check(args: argparse.Namespace) -> dict[str, Any]:
    config = json.loads(args.config.read_text(encoding="utf-8-sig"))
    require(config["gateway"]["bind"] == "loopback", "Use a dedicated loopback Gateway")
    require(
        config["mcp"]["servers"]["bicep"]["transport"] == "streamable-http",
        "This checker requires shared HTTP",
    )
    require(config["tools"]["allow"] == ["bicep__bicep_validate"], "Unexpected tool policy")
    environment = verify_environment(args)
    baseline_start = len(records(args.transport_evidence))
    owner = "openclaw-bicep-" + (args.owner / "owner").read_text().strip()
    sessions = ["http-qualification-" + uuid.uuid4().hex for _ in range(2)]
    config_digest = hashlib.sha256(args.bicep_config.read_bytes()).hexdigest()
    report: dict[str, Any] = {
        "provider": "deterministic-local-fixture",
        "openclaw": environment["openclaw"],
        "versions": environment["versions"],
        "complete_matrix": False,
        "sessions": [{}, {}],
    }
    transport_sessions: list[str] = []

    def containers() -> list[str]:
        return docker("ps", "-aq", "--filter", f"label=maf-sandbox.scope={owner}").split()

    def request(case: str, index: int, *, authenticated: bool = True):
        turn = uuid.uuid4().hex
        connection = http.client.HTTPConnection("127.0.0.1", config["gateway"]["port"], timeout=180)
        headers = {"Content-Type": "application/json"}
        if authenticated:
            headers["Authorization"] = "Bearer " + config["gateway"]["auth"]["token"]
        try:
            connection.request(
                "POST",
                "/v1/chat/completions",
                json.dumps(
                    {
                        "model": "openclaw",
                        "messages": [
                            {
                                "role": "user",
                                "content": f"qualification {case} qualification-turn={turn}",
                            }
                        ],
                        "user": sessions[index],
                    }
                ),
                headers,
            )
        except BaseException:
            connection.close()
            raise
        return connection, turn

    def outcome(case: str, index: int, *, busy: bool = False):
        before = len(records(args.transport_evidence))
        connection, turn = request(case, index)
        try:
            response = connection.getresponse()
            payload = json.loads(response.read())
            require(response.status == 200, "Gateway turn failed")
            require(
                payload["choices"][0]["message"]["content"] == "QUALIFICATION_TOOL_RESULT_RECEIVED",
                "Provider did not finish",
            )
        finally:
            connection.close()
        projected = projected_result(records(args.evidence), turn, case)
        if busy:
            verify_busy(projected)
        else:
            report["sessions"][index][case] = verify_outcome(
                projected, case, config_digest, args.image
            )
            require(not containers(), "Owned container survived completed call")
        calls = [
            r
            for r in records(args.transport_evidence)[before:]
            if r.get("event") == "request" and r.get("method") == "tools/call"
        ]
        if case in ("denied", "helper"):
            verify_dispatch_count(calls, 0)
        else:
            require(
                len(calls) == 1 and calls[0].get("session"), "Expected one observed MCP tool call"
            )
            if len(transport_sessions) <= index:
                transport_sessions.append(calls[0]["session"])
            require(
                calls[0]["session"] == transport_sessions[index],
                "Gateway unexpectedly replaced its MCP session",
            )

    connection, _ = request("valid", 0, authenticated=False)
    try:
        require(connection.getresponse().status == 401, "Gateway accepted missing authentication")
    finally:
        connection.close()
    for index in range(2):
        for case in ("valid", "invalid", "incomplete", "denied", "helper"):
            outcome(case, index)
    require(len(set(transport_sessions)) == 2, "Gateway sessions did not own distinct MCP sessions")
    report["distinct_mcp_sessions"] = True

    other = docker(
        "run",
        "-d",
        "--network",
        "none",
        "--label",
        f"maf-sandbox.scope=qualification-other-{uuid.uuid4().hex}",
        "--entrypoint",
        "/bin/sh",
        args.image,
        "-c",
        "sleep 300",
    )
    connection = None
    try:
        before = len(records(args.transport_evidence))
        connection, _ = request("cancel", 0)
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            active = containers()
            if len(active) == 1 and "bicep" in docker("top", active[0], "-eo", "pid,comm"):
                container = active[0]
                break
            time.sleep(0.05)
        else:
            raise RuntimeError("No active compiler observed")
        calls = [
            r for r in records(args.transport_evidence)[before:] if r.get("method") == "tools/call"
        ]
        require(
            len(calls) == 1 and calls[0]["session"] == transport_sessions[0],
            "Missing active MCP call identity",
        )
        active_call = calls[0]
        outcome("valid", 1, busy=True)
        require(
            containers() == [container],
            "Busy allocated another container or active work already ended",
        )
        require(
            "bicep" in docker("top", container, "-eo", "pid,comm"),
            "Compilation ended before the abort",
        )
        require(connection.sock is not None, "Gateway socket already closed")
        abort_time = time.time_ns()
        connection.sock.shutdown(socket.SHUT_RDWR)
        connection.close()
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            evidence = records(args.transport_evidence)[before:]
            if matching_cancel(evidence, active_call) and not docker(
                "ps", "-aq", "--filter", f"id={container}"
            ):
                break
            time.sleep(0.1)
        else:
            raise RuntimeError(
                "Gateway abort did not establish MCP cancellation and exact-container removal"
            )
        require(
            matching_cancel(evidence, active_call, after_ns=abort_time),
            "Cancellation was not observed after Gateway abort",
        )
        require(docker("ps", "-q", "--filter", f"id={other}"), "Unrelated owner was removed")
        report["shared_admission_and_abort"] = {
            "busy_without_allocation": True,
            "mcp_cancellation_observed": True,
            "exact_container_removed": True,
            "other_owner_preserved": True,
        }
    finally:
        if connection is not None:
            connection.close()
        docker("rm", "-f", other)
    outcome("valid", 1)
    report["post_abort_call"] = "valid"
    verify_dispatch_count(records(args.transport_evidence)[baseline_start:], BASELINE_DISPATCHES)
    report["remaining"] = [
        "restart/reload and no replay",
        "active/idle Gateway runtime disposal",
        "service crash and cleanup failure recovery",
        "full transport/MAF matrix on this candidate",
    ]
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in (
        "config",
        "owner",
        "bicep-config",
        "evidence",
        "transport-evidence",
        "report",
        "openclaw",
    ):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--image", required=True)
    args = parser.parse_args()
    args.report.unlink(missing_ok=True)
    report = check(args)
    root = Path(__file__).resolve().parents[2]
    report["candidate"] = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=root, text=True
    ).strip()
    report["dirty"] = bool(
        subprocess.check_output(["git", "status", "--porcelain"], cwd=root, text=True).strip()
    )
    report["source_hashes"] = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in [
            *sorted((root / "samples/experimental/openclaw_bicep").glob("*.py")),
            Path(__file__),
            Path(__file__).with_name("openclaw_gateway_provider.py"),
            Path(__file__).with_name("openclaw_http_observer.py"),
        ]
    }
    args.report.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
