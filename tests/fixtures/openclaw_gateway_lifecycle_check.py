"""Supervise a dedicated Gateway and qualify bounded HTTP restart and result-loss behavior."""

from __future__ import annotations

import argparse
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
from openclaw_gateway_http_check import check, projected_result, records, require, verify_outcome


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
    serialized = json.dumps(projected)
    require("structuredContent" not in serialized, "Lost result was projected as an outcome")
    require(
        projected.get("status") == "error"
        and "Streamable HTTP error:" in projected.get("error", "")
        and "Internal Server Error" in projected.get("error", ""),
        "Missing transport error",
    )
    return {"completed_result_withheld": True, "dispatches": 1, "transport_error": True}


def run(args: argparse.Namespace) -> dict[str, Any]:
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
    drop_file = root / "drop-result"
    owner_file = root / "owner" / "owner"
    image = args.image
    digest = hashlib.sha256(args.bicep_config.read_bytes()).hexdigest()
    processes: list[subprocess.Popen] = []
    report: dict[str, Any] = {"complete_matrix": False}
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

        def turn(index: int, *, loss=False):
            nonlocal expected_calls
            before = len(records(transport))
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
                                    "content": f"qualification valid qualification-turn={turn_id}",
                                }
                            ],
                        }
                    ),
                    {
                        "Content-Type": "application/json",
                        "Authorization": "Bearer " + config["gateway"]["auth"]["token"],
                    },
                )
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
            expected_calls = len(requests(records(transport)))
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
            require(docker("ps", "-q", "--filter", f"id={sentinel}"), "Unrelated owner was removed")
            report["other_owner_preserved"] = True
            report["total_dispatches"] = expected_calls
            report["remaining"] = [
                "active Gateway runtime disposal",
                "service crash and cleanup-failure recovery",
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
            len(final) == 2
            and all(
                r.get("sessions") == 0 and r.get("active") is False and r.get("poisoned") is False
                for r in final
            ),
            "Final service shutdown was not clean",
        )
        require(
            len(requests(records(transport))) == expected_calls,
            "Unexpected tool replay before final shutdown",
        )
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
    print(json.dumps(run(parser.parse_args()), indent=2))


if __name__ == "__main__":
    main()
