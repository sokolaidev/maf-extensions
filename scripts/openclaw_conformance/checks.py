"""Installed-process adapters for the existing finite qualification checkers."""

from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import os
import platform
import secrets
import subprocess
import sys
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from .evidence import LIMITS, WHEEL, file_hash, read, require
from .install import FIXTURES, SAMPLE, environment, npm_tree

sys.path.insert(0, str(FIXTURES))
sys.path.insert(0, str(SAMPLE))


def installed(prepared: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    """Verify loaded origins and installed artifact bytes."""
    installed_environment = importlib.import_module(
        "openclaw_installed_worker_check"
    ).installed_environment
    verify_wheel = importlib.import_module("openclaw_installed_worker_check").verify_wheel

    from .assets import verify as verify_assets

    assets = verify_assets(prepared, manifest["runtime_assets"])
    found = installed_environment(manifest["versions"])
    require(platform.python_version() == manifest["host"]["python"], "python_changed")
    wheel = prepared / "maf_sandbox_mcp-0.1.0-py3-none-any.whl"
    require(file_hash(wheel) == WHEEL, "worker_artifact_changed")
    checked = verify_wheel(wheel)
    modules = {Path(k).name: v for k, v in checked["module_sha256"].items()}
    tree = npm_tree(prepared / "openclaw-2026.9.7.tgz", prepared / "openclaw/node_modules/openclaw")
    return {
        "versions": found["versions"],
        "wheel_sha256": checked["wheel_sha256"],
        "worker_modules": modules,
        "npm_tree": tree,
        "origins_verified": True,
        "assets_verified": True,
        "runtime_assets": assets,
    }


DISCOVERY = r"""
import atexit,json,os,sys
attempts=[]
emitted=False
os.environ['PYTHONDONTWRITEBYTECODE']='1'
sys.dont_write_bytecode=True
def guard(event,args):
    global emitted
    denied=event in {'subprocess.Popen','os.system','socket.connect','socket.bind','os.mkdir','os.remove','os.rename'}
    if event=='open':
        mode=args[1]
        flags=args[2]
        denied=(isinstance(mode,str) and any(c in mode for c in 'wax+')) or (isinstance(flags,int) and bool(flags & (os.O_WRONLY|os.O_RDWR|os.O_CREAT|os.O_TRUNC)))
    if denied:
        attempts.append(event)
        if emitted: os._exit(3)
        raise RuntimeError('discovery_side_effect')
if len(sys.argv)>1:
    def late():
        import subprocess
        try: subprocess.run([sys.executable,'-c','pass'])
        except RuntimeError: pass
    atexit.register(late)
def report():
    global emitted
    emitted=True
    print(json.dumps({'imports':['maf_sandbox_mcp.server','maf_sandbox_mcp.workload_http'],'side_effect_attempts':attempts}),flush=True)
atexit.register(report)
sys.addaudithook(guard)
import maf_sandbox_mcp.server,maf_sandbox_mcp.workload_http
"""


def discovery(root: Path) -> dict[str, Any]:
    """Observe import effects and prove late guard refusal in a separate control."""
    results = []
    for extra in ([], ["late-control"]):
        completed = subprocess.run(
            [sys.executable, "-I", "-B", "-c", DISCOVERY, *extra],
            cwd=root,
            env=environment(root),
            capture_output=True,
            timeout=60,
        )
        require(len(completed.stdout) < LIMITS["summary"], "discovery_output_limit")
        value = json.loads(completed.stdout)
        require(value["side_effect_attempts"] == [], "import_side_effect")
        results.append((completed.returncode, value))
    require(results[0][0] == 0 and results[1][0] == 3, "late_guard_control")
    return {**results[0][1], "exit_code": 0, "late_guard_control": True}


async def _input_refusals(root: Path, prepared: Path, manifest: dict[str, Any]) -> bool:
    import socket
    from unittest.mock import patch

    import httpx
    from maf_sandbox_docker import DockerSandboxBackend
    from maf_sandbox_mcp import server, workload_http

    creations = 0
    original = DockerSandboxBackend._invoke

    async def observe(backend, *args, **kwargs):
        nonlocal creations
        if args and args[0] in ("create", "run"):
            creations += 1
        return await original(backend, *args, **kwargs)

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    token = secrets.token_hex(32)
    with server.ownership(root / "input-owner") as scope:
        app = await server.http_application(
            manifest["image"], (prepared / "bicepconfig.json").read_text(), scope, token, port
        )
        host = workload_http.server(app)
        task = asyncio.create_task(host.serve(sockets=[sock]))
        observer = patch.object(DockerSandboxBackend, "_invoke", observe)
        observer.start()
        try:
            async with asyncio.timeout(30):
                while not host.started:
                    require(not task.done(), "input_service_failed")
                    await asyncio.sleep(0.05)
            async with httpx.AsyncClient(
                base_url=f"http://127.0.0.1:{port}",
                headers={
                    "Authorization": "Bearer " + token,
                    "Accept": "application/json, text/event-stream",
                },
                timeout=180,
            ) as client:
                init = await client.post(
                    "/mcp",
                    json={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "initialize",
                        "params": {
                            "protocolVersion": "2025-11-25",
                            "capabilities": {},
                            "clientInfo": {"name": "conformance", "version": "1"},
                        },
                    },
                )
                require(init.status_code == 200, "input_initialize")
                client.headers["mcp-session-id"] = init.headers["mcp-session-id"]
                ack = await client.post(
                    "/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"}
                )
                require(ack.status_code == 202, "input_ack")
                malformed = [
                    {"files": []},
                    {"files": [{"path": "../escape", "content": "x"}]},
                    {"files": [{"path": "main.bicep", "content": "x"}], "capability": "exec"},
                ]
                for arguments in malformed:
                    response = await client.post(
                        "/mcp",
                        json={
                            "jsonrpc": "2.0",
                            "id": 2,
                            "method": "tools/call",
                            "params": {"name": "bicep_validate", "arguments": arguments},
                        },
                    )
                    require(
                        response.status_code == 200
                        and response.json()["result"].get("isError") is True
                        and creations == 0,
                        "input_allocated_or_accepted",
                    )
                positive = await client.post(
                    "/mcp",
                    json={
                        "jsonrpc": "2.0",
                        "id": 3,
                        "method": "tools/call",
                        "params": {
                            "name": "bicep_validate",
                            "arguments": {
                                "files": [
                                    {"path": "main.bicep", "content": "output x string = 'ok'"}
                                ]
                            },
                        },
                    },
                )
                result = positive.json()["result"]["structuredContent"]
                require(
                    result["completed"] is True
                    and result["verdict"] == "valid"
                    and result["cleanup"] == "confirmed"
                    and creations > 0,
                    "input_positive_control",
                )
                await client.delete("/mcp")
        finally:
            observer.stop()
            host.should_exit = True
            await asyncio.wait_for(task, 60)
            sock.close()
        require(
            not subprocess.check_output(
                ["docker", "ps", "-aq", "--filter", "label=maf-sandbox.scope=" + scope], timeout=30
            ).strip(),
            "input_cleanup",
        )
    return True


def policy(root: Path, prepared: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    """Exercise operator refusal and malformed calls through installed entry points."""
    console = Path(sys.executable).with_name(
        "maf-bicep-mcp.exe" if os.name == "nt" else "maf-bicep-mcp"
    )
    config = root / "oversized.json"
    config.write_bytes(b" " * (65536 + 1))
    observations = {}
    owner = root / "refused-owner"
    for name, image, path in (
        ("missing_config", manifest["image"], root / "absent.json"),
        ("oversized_config", manifest["image"], config),
        ("mutable_image", "mutable:tag", prepared / "bicepconfig.json"),
    ):
        value = subprocess.run(
            [str(console), "--image", image, "--config", str(path), "--state-dir", str(owner)],
            cwd=root,
            env=environment(root),
            capture_output=True,
            timeout=30,
        )
        observations[name] = value.returncode != 0 and not owner.exists()
    observations["malformed_input"] = asyncio.run(_input_refusals(root, prepared, manifest))
    observations["no_allocation"] = not owner.exists()
    return observations


def gateway(root: Path, prepared: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    """Exercise two real Gateway sessions through the installed worker console."""
    Processes = importlib.import_module("agent").Processes
    configuration = importlib.import_module("agent").configuration
    free_port = importlib.import_module("agent").free_port
    handler = importlib.import_module("openclaw_gateway_provider").handler
    console_gateway = importlib.import_module("openclaw_installed_worker_check").console_gateway

    for name in ("home", "state", "workspace", "temp"):
        (root / name).mkdir()
    (root / "home/AppData/Local/OpenClaw/locks/openclaw-state-owners").mkdir(parents=True)
    evidence = root / "provider.jsonl"
    provider = ThreadingHTTPServer(("127.0.0.1", 0), handler(evidence))
    thread = threading.Thread(target=provider.serve_forever, daemon=True)
    thread.start()
    processes = Processes(root)
    worker_port, gateway_port = free_port(), free_port()
    require(worker_port != gateway_port, "port_collision")
    worker_token, gateway_token = secrets.token_hex(32), secrets.token_hex(32)
    token = root / "token"
    token.write_text(worker_token, encoding="utf-8")
    token.chmod(0o600)
    config = root / "state/openclaw.json"
    config.write_text(
        json.dumps(
            configuration(
                root,
                SimpleNamespace(token="fixture", url=f"http://127.0.0.1:{provider.server_port}/v1"),
                worker_port,
                gateway_port,
                worker_token,
                gateway_token,
            )
        ),
        encoding="utf-8",
    )
    config.chmod(0o600)
    claw = prepared / "openclaw/node_modules/openclaw"
    args = SimpleNamespace(
        config=config,
        bicep_config=prepared / "bicepconfig.json",
        image=manifest["image"],
        owner=root / "owner",
        evidence=evidence,
        expected_versions=manifest["versions"],
    )
    value: dict[str, Any] = {}
    try:
        console = Path(sys.executable).with_name(
            "maf-bicep-mcp.exe" if os.name == "nt" else "maf-bicep-mcp"
        )
        processes.start(
            "worker",
            [
                str(console),
                "--transport",
                "http",
                "--port",
                str(worker_port),
                "--token-file",
                str(token),
                "--image",
                manifest["image"],
                "--config",
                str(args.bicep_config),
                "--state-dir",
                str(args.owner),
            ],
            environment(root),
        )
        processes.ready(worker_port, "/ready", worker_token)
        env = environment(root)
        env.update(
            OPENCLAW_CONFIG_PATH=str(config),
            OPENCLAW_STATE_DIR=str(root / "state"),
            OPENCLAW_HOME=str(root / "home"),
            HOME=str(root / "home"),
            USERPROFILE=str(root / "home"),
            OPENCLAW_NO_RESPAWN="1",
            OPENCLAW_SKIP_CHANNELS="1",
        )
        processes.start("gateway", ["node", str(claw / "openclaw.mjs"), "gateway", "run"], env)
        processes.ready(gateway_port, "/health", gateway_token)
        value = console_gateway(args)
    finally:
        errors = processes.close()
        provider.shutdown()
        provider.server_close()
        thread.join(timeout=10)
        value.update(
            cleanup_errors=errors,
            processes_stopped=processes.stopped and not thread.is_alive(),
            owner_lock_released=False,
        )
        from maf_sandbox_mcp.server import ownership

        with ownership(args.owner):
            value["owner_lock_released"] = True
        owner = (args.owner / "owner").read_text().strip()
        require(len(owner) == 32 and all(c in "0123456789abcdef" for c in owner), "owner_identity")
        require(
            not subprocess.check_output(
                [
                    "docker",
                    "ps",
                    "-aq",
                    "--filter",
                    "label=maf-sandbox.scope=openclaw-bicep-" + owner,
                ],
                timeout=30,
            ).strip(),
            "gateway_owned_survivors",
        )
    return value


def run(group: str, root: Path, prepared: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    """Execute the selected profile without reusing prior campaign evidence."""
    installed_environment = importlib.import_module(
        "openclaw_installed_worker_check"
    ).installed_environment
    networking = importlib.import_module("openclaw_installed_worker_check").networking
    transport = importlib.import_module("openclaw_installed_worker_check").transport

    installed_environment(manifest["versions"])
    if group == "installed":
        return installed(prepared, manifest)
    if group == "discovery":
        return discovery(root)
    if group == "policy":
        return policy(root, prepared, manifest)
    if group == "transport":
        os.environ["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
        from .install import REPO

        value = transport(root, REPO)
        direct = importlib.import_module("openclaw_published_transport_check").DIRECT_ASGI
        value["evidence_classes"] = {
            name: "direct-ASGI" if name.split("[")[0] in direct else "socket"
            for name in value["cases"]
        }
        return value
    if group == "ownership":
        check = importlib.import_module("openclaw_bicep_ownership_check").check

        return asyncio.run(
            check(
                SimpleNamespace(
                    control=read(root.parent / "resources.json"),
                    installed=True,
                    expected_versions=manifest["versions"],
                    root=root,
                    image=manifest["image"],
                    config=prepared / "bicepconfig.json",
                )
            )
        )
    if group == "gateway":
        return gateway(root, prepared, manifest)
    if group == "network":
        return networking(manifest["image"], read(root.parent / "resources.json"))
    raise ValueError("unknown_group")


def main() -> None:
    """Dispatch the explicit qualification command."""
    parser = argparse.ArgumentParser()
    parser.add_argument("group")
    parser.add_argument("root", type=Path)
    parser.add_argument("prepared", type=Path)
    parser.add_argument("manifest", type=Path)
    args = parser.parse_args()
    args.root.mkdir()
    os.chdir(args.root)
    manifest = read(args.manifest)
    value = run(args.group, args.root, args.prepared, manifest)
    raw = json.dumps(value, allow_nan=False).encode()
    require(len(raw) <= LIMITS["events"], "observation_limit")
    (args.root / "observations.json").write_bytes(raw)


if __name__ == "__main__":
    main()
