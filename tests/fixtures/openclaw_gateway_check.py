"""Check a dedicated, already-running Gateway against the local qualification provider."""

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

from openclaw_gateway_provider import SOURCES


def docker(*args: str) -> str:
    return subprocess.check_output(
        ["docker", *args], text=True, stderr=subprocess.PIPE, timeout=15
    ).strip()


def check(args: argparse.Namespace) -> dict[str, Any]:
    config = json.loads(args.config.read_text(encoding="utf-8-sig"))
    assert config["gateway"]["bind"] == "loopback"
    port = config["gateway"]["port"]
    token = config["gateway"]["auth"]["token"]
    owner = "openclaw-bicep-" + (args.owner / "owner").read_text().strip()
    session = args.session or uuid.uuid4().hex
    report: dict[str, Any] = {"provider": "deterministic-local-fixture", "cases": {}}

    def request(case: str, *, authenticated: bool = True) -> http.client.HTTPConnection:
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=180)
        headers = {"Content-Type": "application/json"}
        if authenticated:
            headers["Authorization"] = "Bearer " + token
        connection.request(
            "POST",
            "/v1/chat/completions",
            json.dumps(
                {
                    "model": "openclaw",
                    "messages": [{"role": "user", "content": "qualification " + case}],
                    "user": session,
                }
            ),
            headers,
        )
        return connection

    connection = request("valid", authenticated=False)
    try:
        assert connection.getresponse().status == 401
        report["unauthenticated_http"] = 401
    finally:
        connection.close()

    def outcome(case: str) -> None:
        before = len(args.evidence.read_text().splitlines()) if args.evidence.exists() else 0
        connection = request(case)
        try:
            response = connection.getresponse()
            payload = json.loads(response.read())
            assert response.status == 200, payload
            assert (
                payload["choices"][0]["message"]["content"] == "QUALIFICATION_TOOL_RESULT_RECEIVED"
            )
        finally:
            connection.close()
        records = [json.loads(line) for line in args.evidence.read_text().splitlines()[before:]]
        recorded = [item for item in records if "tool_result" in item]
        assert recorded and recorded[-1]["scenario"] == case
        text = recorded[-1]["tool_result"]
        projected = json.loads(text[text.index("{") : text.rindex("}") + 1])
        if case in ("denied", "helper"):
            assert projected["status"] == "error" and "Unknown tool id:" in projected["error"]
            report["cases"][case] = {"rejected": True}
            return
        result = projected["result"]
        structured = result["details"]["structuredContent"]
        assert len(result["content"]) == 1
        assert structured["cleanup"] == "confirmed"
        assert structured["completed"] is (case != "incomplete")
        assert structured["verdict"] == (None if case == "incomplete" else case)
        assert structured["status"] == ("incomplete" if case == "incomplete" else "ok")
        canonical = json.dumps([["main.bicep", SOURCES[case]]], separators=(",", ":")).encode()
        assert structured["source_sha256"] == hashlib.sha256(canonical).hexdigest()
        assert (
            structured["config_sha256"]
            == hashlib.sha256(args.bicep_config.read_bytes()).hexdigest()
        )
        assert structured["image"] == args.image
        assert not docker("ps", "-aq", "--filter", f"label=maf-sandbox.scope={owner}")
        report["cases"][case] = {
            key: structured[key]
            for key in ("completed", "verdict", "status", "cleanup", "source_sha256")
        } | {"projected_content_items": len(result["content"])}

    for case in ("valid", "invalid", "incomplete", "denied", "helper"):
        outcome(case)

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
    try:
        connection = request("cancel")
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            containers = docker("ps", "-q", "--filter", f"label=maf-sandbox.scope={owner}").split()
            if containers and "bicep" in docker("top", containers[0], "-eo", "pid,comm"):
                container = containers[0]
                break
            time.sleep(0.05)
        else:
            raise AssertionError("No active compiler observed")
        limits = json.loads(docker("inspect", container))[0]["HostConfig"]
        assert limits["NetworkMode"] == "none" and limits["Memory"] == 1024**3
        assert limits["NanoCpus"] == 10**9 and limits["PidsLimit"] == 128
        assert "ALL" in limits["CapDrop"]
        started = time.monotonic()
        assert connection.sock is not None
        connection.sock.shutdown(socket.SHUT_RDWR)
        connection.close()
        while docker("ps", "-aq", "--filter", f"id={container}"):
            assert time.monotonic() - started < 60, "Container survived disconnect"
            time.sleep(0.1)
        assert docker("ps", "-q", "--filter", f"id={other}")
        report["disconnect"] = {
            "active_compiler_observed": True,
            "exact_container_removed": True,
            "other_owner_survived": True,
            "seconds_after_disconnect": round(time.monotonic() - started, 3),
            "resource_limits_verified": True,
        }
    finally:
        connection.close()
        docker("rm", "-f", other)
    outcome("valid")
    report["post_disconnect_call"] = "valid"
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("config", "owner", "bicep-config", "evidence", "report"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--session", help="Reuse the sole session already owning this service")
    options = parser.parse_args()
    options.report.unlink(missing_ok=True)
    result = check(options)
    options.report.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2))
