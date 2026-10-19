"""Observe one bounded owner-loss case in an installed process."""

from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes
import http.client as http_client
import importlib
import json
import os
import secrets
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from .evidence import Refused, digest, file_hash, read, require, write
from .install import FIXTURES, SAMPLE, environment
from .owner_loss_evidence import DEADLINES, DENIAL_IMAGE, DENIAL_SOURCE

sys.path[:0] = [str(FIXTURES), str(SAMPLE)]


def wait(test, reason: str, seconds: float = 90):
    """Poll an observer predicate within its selected phase deadline."""
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        value = test()
        if value:
            return value
        time.sleep(0.05)
    raise Refused(reason)


def rows(path: Path) -> list[dict[str, Any]]:
    """Read complete bounded event records while a child may still append."""
    return importlib.import_module("openclaw_gateway_http_check").records(path)


def http(port: int, token: str, path: str = "/ready", body=None, sid=None, method=None, timeout=5):
    """Exchange one bounded loopback request without retrying it."""
    checker = importlib.import_module("openclaw_gateway_http_check")
    connection = http_client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    headers = {
        "Authorization": "Bearer " + token,
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if sid is not None:
        headers["mcp-session-id"] = sid
    try:
        connection.request(
            method or ("POST" if body is not None else "GET"),
            path,
            None if body is None else json.dumps(body),
            headers,
        )
        response = connection.getresponse()
        return response.status, checker.read_response(response), dict(response.getheaders())
    finally:
        connection.close()


class Actor:
    """Retain the exact actor handle after proving membership in its supervisor's job."""

    def __init__(self, processes: Any, pid: int):
        require(os.name == "nt" and type(pid) is int and pid > 0, "windows_actor")
        child = processes.children[-1][0]
        job = processes.jobs[child.pid]
        self.api = job.api
        self.api.IsProcessInJob.argtypes = [
            ctypes.wintypes.HANDLE,
            ctypes.wintypes.HANDLE,
            ctypes.POINTER(ctypes.wintypes.BOOL),
        ]
        self.api.IsProcessInJob.restype = ctypes.wintypes.BOOL
        self.api.TerminateProcess.argtypes = [ctypes.wintypes.HANDLE, ctypes.wintypes.UINT]
        self.api.TerminateProcess.restype = ctypes.wintypes.BOOL
        self.handle = self.api.OpenProcess(0x100000 | 0x1000 | 0x0001, False, pid)
        job.check(self.handle)
        member = ctypes.wintypes.BOOL()
        try:
            job.check(self.api.IsProcessInJob(self.handle, job.handle, ctypes.byref(member)))
            require(member.value, "actor_not_owned")
        except BaseException:
            self.close()
            raise

    def alive(self) -> bool:
        return self.api.WaitForSingleObject(self.handle, 0) == 258

    def kill(self) -> None:
        require(self.alive(), "actor_exited_before_fault")
        require(self.api.TerminateProcess(self.handle, 86), "actor_termination_failed")
        require(self.api.WaitForSingleObject(self.handle, 10000) == 0, "actor_did_not_exit")

    def close(self) -> None:
        if self.handle:
            self.api.CloseHandle(self.handle)
            self.handle = None


def case(
    root: Path, prepared: Path, manifest: dict[str, Any], name: str, denial=None
) -> dict[str, Any]:
    """Observe one isolated fault and retain independent cleanup on every exit."""
    Processes = importlib.import_module("agent").Processes
    configuration = importlib.import_module("agent").configuration
    free_port = importlib.import_module("agent").free_port
    from maf_sandbox_mcp.server import ownership

    compiler_running = importlib.import_module("openclaw_bicep_ownership_check").compiler_running
    projected_result = importlib.import_module("openclaw_gateway_http_check").projected_result
    rpc_result = importlib.import_module("openclaw_gateway_http_check").rpc_result
    verify_outcome = importlib.import_module("openclaw_gateway_http_check").verify_outcome
    workload_result = importlib.import_module("openclaw_gateway_http_check").workload_result
    handler = importlib.import_module("openclaw_gateway_provider").handler
    record = importlib.import_module("openclaw_owner_loss_worker").record
    LARGE_SOURCE = importlib.import_module("openclaw_sources").LARGE_SOURCE
    SIMPLE_SOURCE = importlib.import_module("openclaw_sources").SIMPLE_SOURCE

    root.mkdir(parents=True)
    for folder in ("home", "state", "workspace", "temp"):
        (root / folder).mkdir(exist_ok=True)
    (root / "home/AppData/Local/OpenClaw/locks/openclaw-state-owners").mkdir(parents=True)
    started = time.monotonic()
    journal = root / "observer.jsonl"

    def note(event, **fields):
        record(journal, event, **fields)

    env = environment(root)
    for key in ("DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH", "DOCKER_CUSTOM_HEADERS"):
        if key in os.environ:
            env[key] = os.environ[key]
    if denial:
        env["DOCKER_CUSTOM_HEADERS"] = denial.environment["DOCKER_CUSTOM_HEADERS"]

    def docker(*args, timeout=30):
        value = subprocess.run(
            ["docker", *args],
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=timeout,
        )
        require(value.returncode == 0, "observer_docker_failure")
        return value.stdout.strip()

    def owned():
        owner_file = root / "owner/owner"
        if not owner_file.exists():
            return []
        owner = owner_file.read_text(encoding="ascii")
        require(len(owner) == 32 and all(c in "0123456789abcdef" for c in owner), "owner_identity")
        return sorted(
            docker(
                "ps",
                "-aq",
                "--no-trunc",
                "--filter",
                "label=maf-sandbox.scope=openclaw-bicep-" + owner,
            ).split()
        )

    sentinel_name = "oc-owner-loss-" + uuid.uuid4().hex
    sentinel_scope = "owner-loss-sentinel-" + uuid.uuid4().hex
    sentinel = None

    def sentinel_alive():
        if sentinel is None:
            return False
        item = json.loads(docker("inspect", sentinel))[0]
        return (
            item["Id"] == sentinel
            and item["State"]["Running"] is True
            and item["Config"]["Labels"].get("maf-sandbox.scope") == sentinel_scope
        )

    worker_port, gateway_port = free_port(), free_port()
    require(worker_port != gateway_port, "port_collision")
    token, gateway_token = secrets.token_hex(32), secrets.token_hex(32)
    phase = name.split("-", 1)[1] if "-" in name else "control"
    if phase in {"unavailable", "denied"}:
        phase = "active"
    worker_settings = {
        "image": manifest["image"],
        "config": (prepared / "bicepconfig.json").read_text(encoding="utf-8"),
        "token": token,
        "port": worker_port,
        "phase": phase,
    }
    write(root / "worker.json", worker_settings)
    provider = ThreadingHTTPServer(("127.0.0.1", 0), handler(root / "provider.jsonl"))
    provider.daemon_threads = False
    provider_thread = threading.Thread(target=provider.serve_forever, daemon=True)
    provider_thread.start()
    processes = []
    actors: dict[str, Actor] = {}
    scope_stack = ExitStack()
    failure = None
    settings_path = root / "state/openclaw.json"
    write(
        settings_path,
        configuration(
            root,
            SimpleNamespace(token="fixture", url=f"http://127.0.0.1:{provider.server_port}/v1"),
            worker_port,
            gateway_port,
            token,
            gateway_token,
        ),
    )
    private_context = None
    client_process = None

    def start(role, command, child_env):
        proc = Processes(root)
        processes.append(proc)
        proc.start(role + str(len(processes)), command, child_env)
        return proc

    bootstrap = "import runpy,sys; sys.path.insert(0,sys.argv.pop(1)); runpy.run_module('openclaw_owner_loss_worker',run_name='__main__')"

    def start_worker(child_env=None):
        before = len(rows(root / "worker-pid.jsonl"))
        proc = start(
            "worker",
            [sys.executable, "-I", "-B", "-c", bootstrap, str(FIXTURES), "worker", str(root)],
            env if child_env is None else child_env,
        )
        pid = wait(lambda: rows(root / "worker-pid.jsonl")[before:] or None, "worker_pid")[0]["pid"]
        actor = Actor(proc, pid)
        scope_stack.callback(actor.close)
        return proc, actor

    def ready(port, key, actor, path="/ready"):
        def probe():
            require(actor.alive(), "actor_exited_before_ready")
            try:
                return http(port, key, path)[0] == 200
            except (OSError, http_client.HTTPException):
                return False

        wait(probe, "readiness_deadline", DEADLINES["readiness"])

    def initialize():
        status, payload, headers = http(
            worker_port,
            token,
            "/mcp",
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": "owner-loss", "version": "1"},
                },
            },
        )
        require(status == 200, "initialize_failed")
        rpc_result(payload, 1)
        sid = next(v for k, v in headers.items() if k.lower() == "mcp-session-id")
        require(
            http(
                worker_port,
                token,
                "/mcp",
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                sid,
            )[0]
            == 202,
            "initialized_failed",
        )
        return sid

    def payload(source=SIMPLE_SOURCE):
        return {
            "jsonrpc": "2.0",
            "id": 42,
            "method": "tools/call",
            "params": {
                "name": "bicep_validate",
                "arguments": {"files": [{"path": "main.bicep", "content": source}]},
            },
        }

    def valid(sid):
        status, raw, _ = http(worker_port, token, "/mcp", payload(), sid, timeout=90)
        require(status == 200, "sibling_failed")
        return workload_result(rpc_result(raw, 42))

    sibling_session = uuid.uuid4().hex

    def gateway_body(scenario, session=None):
        turn, session = uuid.uuid4().hex, session or uuid.uuid4().hex
        body = {
            "model": "openclaw",
            "user": session,
            "messages": [
                {
                    "role": "user",
                    "content": f"qualification {scenario} qualification-turn={turn} qualification-session={session}",
                }
            ],
        }
        return turn, body

    def gateway_valid():
        turn, body = gateway_body("valid", sibling_session)
        status, _, _ = http(gateway_port, gateway_token, "/v1/chat/completions", body, timeout=270)
        require(status == 200, "gateway_control_failed")
        result = projected_result(rows(root / "provider.jsonl"), turn, "valid")
        checked = verify_outcome(result, "valid", manifest["config_sha256"], manifest["image"])
        note("gateway_result", turn=turn, session=sibling_session, result=checked)
        return checked

    def compiler():
        resources = owned()
        require(len(resources) <= 1, "multiple_owned_resources")
        if resources and compiler_running(docker("top", resources[0], "-eo", "pid,args")):
            docker("pause", resources[0])
            inspected = json.loads(docker("inspect", resources[0]))[0]
            require(
                inspected["State"]["Paused"] is True
                and inspected["HostConfig"]["NetworkMode"] == "none"
                and inspected["Image"] == manifest["image"],
                "compiler_confinement",
            )
            require(
                compiler_running(docker("top", resources[0], "-eo", "pid,args")),
                "compiler_not_running",
            )
            return resources
        return None

    try:
        write(
            root / "resources.json",
            {"sentinel_name": sentinel_name, "sentinel_scope": sentinel_scope},
        )
        sentinel = docker(
            "run",
            "-d",
            "--name",
            sentinel_name,
            "--label",
            "maf-sandbox.scope=" + sentinel_scope,
            "--network",
            "none",
            manifest["image"],
            "sleep",
            "infinity",
            timeout=60,
        )
        worker_process, actors["worker"] = start_worker()
        ready(worker_port, token, actors["worker"])
        locked = False
        try:
            with ownership(root / "owner"):
                pass
        except OSError:
            locked = True
        note("lock_contender", refused=locked)
        require(locked, "concurrent_owner_accepted")
        gateway_env = dict(
            env,
            OPENCLAW_CONFIG_PATH=str(settings_path),
            OPENCLAW_STATE_DIR=str(root / "state"),
            OPENCLAW_HOME=str(root / "home"),
            HOME=str(root / "home"),
            USERPROFILE=str(root / "home"),
            OPENCLAW_NO_RESPAWN="1",
            OPENCLAW_SKIP_CHANNELS="1",
        )
        launcher = "const fs=require('fs'),cp=require('child_process');const p=cp.spawn(process.execPath,[process.argv[1],'gateway','run'],{stdio:'inherit'});fs.writeFileSync(process.argv[2],JSON.stringify({pid:p.pid}));p.on('exit',c=>process.exit(c===null?1:c));"
        gateway_process = start(
            "gateway",
            [
                "node",
                "-e",
                launcher,
                str(prepared / "openclaw/node_modules/openclaw/openclaw.mjs"),
                str(root / "gateway-pid.json"),
            ],
            gateway_env,
        )
        wait(lambda: (root / "gateway-pid.json").exists(), "gateway_pid")
        actors["gateway"] = Actor(gateway_process, read(root / "gateway-pid.json")["pid"])
        scope_stack.callback(actors["gateway"].close)
        ready(gateway_port, gateway_token, actors["gateway"], "/health")
        note("control_result", result=gateway_valid())
        old_sid = initialize()
        if name == "control":
            note("client_settled", status=200)
        else:
            actor_name = name.split("-", 1)[0]
            target_started_ns = time.monotonic_ns()
            (root / "arm").touch()
            turn, body = gateway_body("cancel" if phase == "active" else "valid")
            write(
                root / "client.json",
                {"port": gateway_port, "token": gateway_token, "turn": turn, "body": body},
            )
            client_process = start(
                "client",
                [sys.executable, "-I", "-B", "-c", bootstrap, str(FIXTURES), "client", str(root)],
                env,
            )
            pid = wait(lambda: rows(root / "client.jsonl") or None, "client_pid")[0]["pid"]
            actors["client"] = Actor(client_process, pid)
            scope_stack.callback(actors["client"].close)
            # Gateway preparation has its own deadline; the phase barrier begins at worker ingress.
            wait(
                lambda: any(r["event"] == "target_request" for r in rows(root / "transport.jsonl")),
                "target_ingress",
                DEADLINES["readiness"],
            )
            if phase == "active":
                resources = wait(compiler, "compiler_barrier", DEADLINES["barrier"])
                note(
                    "barrier_observed",
                    phase=phase,
                    resources=resources,
                    compiler_running=True,
                    paused=True,
                    target_started_ns=target_started_ns,
                )
            else:
                observed = wait(
                    lambda: (
                        [r for r in rows(root / "transport.jsonl") if r.get("event") == "barrier"]
                        or None
                    ),
                    "worker_barrier",
                    DEADLINES["barrier"],
                )[0]
                resources = owned()
                note(
                    "barrier_observed",
                    phase=phase,
                    resources=resources,
                    target_started_ns=target_started_ns,
                    result=observed.get("result"),
                )
            identities = [
                r for r in rows(root / "transport.jsonl") if r["event"] == "target_request"
            ]
            require(len(identities) == 1, "target_request_identity")
            identity = identities[0]
            note(
                "target_identity",
                boot=identity["boot"],
                session=identity["session"],
                request=identity["request"],
                request_ns=identity["mono_ns"],
                turn=turn,
            )
            actors[actor_name].kill()
            note(
                "actor_killed",
                actor=actor_name,
                exited=not actors[actor_name].alive(),
                others_alive=all(
                    a.alive() for key, a in actors.items() if key not in {actor_name, "client"}
                ),
                job_membership_verified=True,
            )
            if actor_name == "worker":
                note(
                    "service_observed",
                    state="dead_readiness_unconfirmed",
                    resources=owned(),
                    sentinel_survived=sentinel_alive(),
                )
            else:
                (root / "release").touch()
                if phase == "active":
                    docker("unpause", resources[0])
                    note("harness_unpause", resources=resources)

                def settled():
                    trace = rows(root / "transport.jsonl")
                    requests = [
                        r
                        for r in trace
                        if r["event"] == "request"
                        and r.get("method") == "tools/call"
                        and all(r.get(k) == identity[k] for k in ("boot", "session", "request"))
                    ]
                    if len(requests) != 1:
                        return None
                    terminals = [
                        r
                        for r in trace
                        if r["event"] == "settled" and r.get("exchange") == requests[0]["exchange"]
                    ]
                    if len(terminals) != 1 or owned():
                        return None
                    if http(worker_port, token)[0] != 200:
                        return None
                    snapshots = [
                        r
                        for r in rows(root / "transport.jsonl")
                        if r["event"] == "registry_snapshot"
                        and r["mono_ns"] > terminals[0]["mono_ns"]
                    ]
                    if (
                        not snapshots
                        or snapshots[-1]["active"] is not False
                        or snapshots[-1]["poisoned"] is not False
                    ):
                        return None
                    return {
                        "terminal_ns": terminals[0]["mono_ns"],
                        "snapshot_ns": snapshots[-1]["mono_ns"],
                        "exchange": requests[0]["exchange"],
                    }

                settlement = wait(settled, "service_settlement", DEADLINES["settlement"])
                note(
                    "service_observed",
                    state="settled",
                    **settlement,
                    resources=owned(),
                    sentinel_survived=sentinel_alive(),
                )
            wait(lambda: not actors["client"].alive(), "client_deadline", DEADLINES["client"] + 10)
            code = client_process.children[0][0].wait(timeout=10)
            outcomes = [r for r in rows(root / "client.jsonl") if r["event"] == "client_outcome"]
            if actor_name == "client":
                require(not outcomes, "client_finished_before_fault")
                note("client_settled", outcome="killed", status=None, error=None, exit_code=code)
            else:
                require(len(outcomes) == 1, "client_outcome_missing")
                outcome = outcomes[0]
                note(
                    "client_settled",
                    outcome="response" if outcome["status"] is not None else "transport_error",
                    status=outcome["status"],
                    error=outcome["error"],
                    exit_code=code,
                )
            target_events = [
                r
                for r in rows(root / "transport.jsonl")
                if r["event"] == "binding_started" and r["mono_ns"] >= target_started_ns
            ]
            note("target_dispatches", count=len(target_events))
            (root / "arm").unlink(missing_ok=True)
            (root / "release").touch()
            if actor_name == "worker":
                old_token, old_owner = token, (root / "owner/owner").read_text(encoding="ascii")
                old_boot = identity["boot"]
                with ownership(root / "owner") as recovered_owner:
                    note("owner_released", owner=digest(recovered_owner))
                token = secrets.token_hex(32)
                worker_settings.update(token=token, phase="control")
                write(root / "worker.json", worker_settings)
                info: dict[str, Any] = {}
                if name == "worker-unavailable":
                    DockerConnectionFault = importlib.import_module(
                        "openclaw_gateway_lifecycle_check"
                    ).DockerConnectionFault

                    private_context = DockerConnectionFault(root, scope_stack)
                    private_context.disconnect()
                    blocked_env = dict(
                        env,
                        DOCKER_CONFIG=str(private_context.directory),
                        DOCKER_CONTEXT=private_context.name,
                    )
                    blocked_env.pop("DOCKER_HOST", None)
                elif denial:
                    info = json.loads(docker("inspect", resources[0]))[0]
                    denial.arm(info["Name"].lstrip("/"), resources[0])
                    blocked_env = env
                else:
                    blocked_env = None
                if blocked_env is not None:
                    failed_proc, failed_actor = start_worker(blocked_env)
                    wait(lambda: not failed_actor.alive(), "failed_recovery_deadline", 120)
                    code = failed_proc.children[0][0].wait(timeout=10)
                    try:
                        is_ready = http(worker_port, token)[0] == 200
                    except (OSError, http_client.HTTPException):
                        is_ready = False
                    note("recovery_refused", exit_code=code, ready=is_ready, resources=owned())
                    require(code != 0 and not is_ready, "uncertain_recovery_admitted")
                    if private_context:
                        private_context.restore()
                    if denial:
                        decisions = denial.evidence()
                        verify_authorization = importlib.import_module(
                            "openclaw_docker_denial"
                        ).verify_authorization

                        removals = [
                            r
                            for r in rows(root / "transport.jsonl")
                            if r["event"] == "docker_removal" and r["returncode"] != 0
                        ]
                        verify_authorization(
                            decisions,
                            removals,
                            target=info["Name"].lstrip("/"),
                            container=resources[0],
                            client=denial.client,
                            nonce=denial.nonce,
                            denied=True,
                        )
                        write(
                            root / "denial-decisions.json",
                            {
                                "decisions": decisions,
                                "removals": removals,
                                "target": info["Name"].lstrip("/"),
                                "container": resources[0],
                                "client": denial.client,
                                "nonce": denial.nonce,
                            },
                        )
                        denial.restore()
                _, actors["worker"] = start_worker()
                ready(worker_port, token, actors["worker"])
                require(not owned(), "replacement_left_orphan")
                fresh_sid = initialize()
                unauthorized = http(worker_port, old_token, "/mcp", payload(), fresh_sid)[0]
                stale = http(worker_port, token, "/mcp", payload(), old_sid)[0]
                with ThreadPoolExecutor(max_workers=1) as pool:
                    fresh_call = pool.submit(
                        http, worker_port, token, "/mcp", payload(LARGE_SOURCE), fresh_sid, None, 90
                    )
                    fresh_resources = wait(compiler, "fresh_generation_compiler")
                    try:
                        cancel_status = http(
                            worker_port,
                            token,
                            "/mcp",
                            {
                                "jsonrpc": "2.0",
                                "method": "notifications/cancelled",
                                "params": {"requestId": 42},
                            },
                            old_sid,
                        )[0]
                        delete_status = http(worker_port, token, "/mcp", None, old_sid, "DELETE")[0]
                        fresh_survived = not fresh_call.done() and owned() == fresh_resources
                    finally:
                        docker("unpause", fresh_resources[0])
                    fresh_status, fresh_body, _ = fresh_call.result(timeout=90)
                    require(fresh_status == 200, "fresh_call_transport")
                    fresh_result = workload_result(rpc_result(fresh_body, 42))
                    require(
                        fresh_result["status"] in {"ok", "incomplete"}
                        and fresh_result["cleanup"] == "confirmed",
                        "fresh_call_cancelled",
                    )
                wait(lambda: not owned(), "fresh_call_cleanup")
                boots = [r for r in rows(root / "transport.jsonl") if r["event"] == "startup_ready"]
                note(
                    "replacement_ready",
                    resources=owned(),
                    owner_unchanged=(root / "owner/owner").read_text(encoding="ascii") == old_owner,
                    boot_changed=boots[-1]["boot"] != old_boot,
                    boot=boots[-1]["boot"],
                    ready_ns=boots[-1]["mono_ns"],
                    fresh_result=fresh_result,
                    old_token_status=unauthorized,
                    old_session_status=stale,
                    stale_cancel_status=cancel_status,
                    stale_delete_status=delete_status,
                    fresh_call_survived=fresh_survived,
                )
                sibling = valid(fresh_sid)
            elif actor_name == "client":
                sibling = gateway_valid()
            else:
                sibling = valid(old_sid)
            note("sibling_result", result=sibling)
            known = rows(root / "transport.jsonl")
            after_target = [
                r
                for r in known
                if r["event"] == "binding_started" and r["mono_ns"] >= target_started_ns
            ]
            note(
                "binding_inventory",
                bindings=[
                    {k: r[k] for k in ("boot", "session", "request", "mono_ns", "source_sha256")}
                    for r in after_target
                ],
            )
    except BaseException as exc:
        failure = str(exc) if isinstance(exc, Refused) else type(exc).__name__
        note("failure", reason=failure)
    finally:
        errors = []
        if denial:
            try:
                denial.restore()
            except Exception as exc:
                errors.append(type(exc).__name__)
        for proc in reversed(processes):
            errors.extend(proc.close())
        provider.shutdown()
        provider.server_close()
        provider_thread.join(timeout=10)
        try:
            scope_stack.close()
        except Exception as exc:
            errors.append(type(exc).__name__)
        remaining = None
        sentinel_survived = False
        sentinel_removed = False
        try:
            sentinel_survived = sentinel_alive()
            leftovers = owned()
            note("harness_leftovers", resources=leftovers)
            for container in leftovers:
                docker("rm", "-f", container)
            remaining = owned()
            if sentinel is not None:
                require(sentinel_alive(), "sentinel_lost")
                docker("rm", "-f", sentinel)
                sentinel_removed = not docker("ps", "-aq", "--filter", "id=" + sentinel)
        except Exception as exc:
            errors.append(type(exc).__name__)
        note(
            "harness_cleanup",
            processes_stopped=all(proc.stopped for proc in processes)
            and not provider_thread.is_alive(),
            remaining=remaining,
            errors=errors,
            sentinel_survived=sentinel_survived,
            sentinel_removed=sentinel_removed,
        )
    value = {
        "case": name,
        "outcome": "fail" if failure else "pass",
        "failure": failure,
        "image": manifest["image"],
        "config_sha256": manifest["config_sha256"],
        "duration": time.monotonic() - started,
        "events": rows(journal),
        "transport": rows(root / "transport.jsonl"),
        "provider": rows(root / "provider.jsonl"),
        "client": rows(root / "client.jsonl"),
    }
    write(root / "result.json", value)
    return value


def main() -> None:
    """Run one selected case using the prepared installed interpreter."""
    parser = argparse.ArgumentParser()
    parser.add_argument("case")
    parser.add_argument("root", type=Path)
    parser.add_argument("prepared", type=Path)
    parser.add_argument("manifest", type=Path)
    args = parser.parse_args()
    manifest = read(args.manifest)
    if args.case == "worker-denied":
        require(
            file_hash(FIXTURES / "openclaw_docker_authz/main.go") == DENIAL_SOURCE,
            "denial_source_changed",
        )
        host = importlib.import_module("openclaw_docker_denial_check")
        DockerRemovalDenial = importlib.import_module("openclaw_docker_denial").DockerRemovalDenial
        run = importlib.import_module("openclaw_docker_denial_check").run

        observed = {}

        def qualify(settings):
            nonlocal observed
            control = DockerRemovalDenial(
                args.root, settings.authz_outer_host, settings.authz_daemon, settings.authz_client
            )
            observed = case(args.root / "gateway", args.prepared, manifest, args.case, control)
            return observed

        setattr(host, "qualify", qualify)
        run(
            SimpleNamespace(
                root=args.root,
                image=manifest["image"],
                daemon_image=DENIAL_IMAGE,
                config=args.root / "unused",
                bicep_config=args.prepared / "bicepconfig.json",
                openclaw=args.prepared / "openclaw/node_modules/openclaw",
            )
        )
        observed["denial"] = {
            **read(args.root / "gateway/denial-decisions.json"),
            "daemon_removed": True,
            "parent_unchanged": True,
            "image": DENIAL_IMAGE,
        }
        write(args.root / "result.json", observed)
    else:
        observed = case(args.root, args.prepared, manifest, args.case)
    require(observed["outcome"] == "pass", "fault_case_failed")


if __name__ == "__main__":
    main()
