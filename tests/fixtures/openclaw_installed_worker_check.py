"""Qualify an installed MCP wheel outside the checkout with bounded runtime evidence."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib
import importlib.metadata
import json
import os
import platform
import shutil
import subprocess
import sys
import threading
import time
import uuid
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from openclaw_gateway_http_check import require
from openclaw_published_transport_check import CASES, Results

BASELINE = {
    "agent-framework-core": "1.20.0",
    "maf-sandbox": "0.48.0",
    "maf-sandbox-bicep": "0.22.2",
    "maf-sandbox-docker": "0.27.0",
    "mcp": "1.28.1",
    "uvicorn": "0.54.0",
    "starlette": "1.7.0",
}
MODULES = {
    "agent-framework-core": "agent_framework",
    "maf-sandbox": "maf_sandbox",
    "maf-sandbox-bicep": "maf_sandbox_bicep",
    "maf-sandbox-docker": "maf_sandbox_docker",
    "maf-sandbox-mcp": "maf_sandbox_mcp",
    "mcp": "mcp",
    "uvicorn": "uvicorn",
}


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def installed_environment(expected: dict[str, str] | None = None) -> dict[str, Any]:
    """Require index dependencies and one non-editable candidate wheel with installed imports."""
    versions: dict[str, str] = {}
    locations: dict[str, str] = {}
    for dist in importlib.metadata.distributions():
        name = dist.metadata["Name"].lower().replace("_", "-")
        require(name not in versions, "Duplicate distribution")
        direct = json.loads(dist.read_text("direct_url.json") or "{}")
        if direct:
            require(
                name == "maf-sandbox-mcp" and "archive_info" in direct and "dir_info" not in direct,
                "Non-wheel candidate or local dependency",
            )
        versions[name] = dist.version
    require(
        versions == expected
        if expected is not None
        else all(versions.get(k) == v for k, v in BASELINE.items()),
        "Requalify changed dependency baseline explicitly",
    )
    for name, module_name in MODULES.items():
        module = importlib.import_module(module_name)
        location = Path(module.__file__).resolve()
        root = Path(str(importlib.metadata.distribution(name).locate_file(""))).resolve()
        require(
            location.is_relative_to(root)
            and root.is_relative_to(Path(sys.prefix).resolve())
            and "site-packages" in location.parts,
            "Import escaped installed distribution",
        )
        locations[name] = str(location)
    return {"versions": dict(sorted(versions.items())), "locations": locations}


def verify_wheel(wheel: Path) -> dict[str, Any]:
    """Bind installed runtime bytes to the candidate artifact, including all runtime modules."""
    import maf_sandbox_mcp

    root = Path(maf_sandbox_mcp.__file__).parent
    hashes = {}
    with zipfile.ZipFile(wheel) as archive:
        names = [
            n for n in archive.namelist() if n.startswith("maf_sandbox_mcp/") and n.endswith(".py")
        ]
        require(
            set(Path(n).name for n in names)
            == {"__init__.py", "server.py", "workload_http.py", "workload_service.py"},
            "Incomplete runtime wheel",
        )
        for name in names:
            data = archive.read(name)
            require(
                (root / Path(name).name).read_bytes() == data,
                "Installed runtime differs from wheel",
            )
            hashes[name] = sha(data)
    return {"wheel_sha256": sha(wheel.read_bytes()), "module_sha256": hashes}


def transport(root: Path, repo: Path) -> dict[str, Any]:
    """Run the finite suite with no checkout module, conftest, or external pytest plugin."""
    import pytest

    suite = root / "test_transport.py"
    shutil.copyfile(repo / "tests/test_mcp_workload_service.py", suite)
    config = root / "pytest.ini"
    config.write_text("[pytest]\n", encoding="utf-8")
    results = Results()
    exit_code = pytest.main(
        [
            "-q",
            "--noconftest",
            "-p",
            "no:cacheprovider",
            "-c",
            str(config),
            *[f"{suite}::{case}" for case in CASES],
        ],
        plugins=[results],
    )
    results.verify(exit_code)
    return {
        "cases": results.collected,
        "phases": results.reports,
        "retirements": results.retirements,
        "final_state": results.final_state,
    }


def networking(image: str, control: dict[str, Any] | None = None) -> dict[str, Any]:
    """Compare one HTTP/TCP destination from bridge and closed guests of the same image."""
    nonce = uuid.uuid4().hex.encode()

    class Control(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(nonce)

        def log_message(self, *args):
            pass

    listener = ThreadingHTTPServer(("0.0.0.0", 0), Control)
    thread = threading.Thread(target=listener.serve_forever, daemon=True)
    thread.start()
    port = listener.server_port
    created: list[str] = []
    observations = []
    try:
        for mode in ("bridge", "none"):
            name = control["names"][mode] if control else "mcp-network-" + uuid.uuid4().hex
            created.append(name)
            command = [
                "docker",
                "run",
                "--name",
                name,
                *(["--label", control["label"]] if control else []),
                "--network",
                mode,
                "--add-host",
                "probe-control.test:host-gateway",
                "--entrypoint",
                "curl",
                image,
                "--noproxy",
                "*",
                "--silent",
                "--show-error",
                "--connect-timeout",
                "3",
                "--max-time",
                "5",
                f"http://probe-control.test:{port}/",
            ]
            result = subprocess.run(command, capture_output=True, timeout=30)
            config = json.loads(subprocess.check_output(["docker", "inspect", name], text=True))[0]
            require(config["HostConfig"]["NetworkMode"] == mode, "Network mode differs")
            require(config["Image"] == image, "Network probe image differs")
            if mode == "bridge":
                require(
                    result.returncode == 0 and result.stdout == nonce, "Reachable control failed"
                )
            else:
                require(
                    result.returncode in (7, 28) and result.stdout == b"",
                    "Closed guest did not refuse TCP connection",
                )
            observations.append(
                {
                    "mode": mode,
                    "exit_code": result.returncode,
                    "control_body_matches": result.stdout == nonce,
                }
            )
    finally:
        cleanup_errors = []
        for name in created:
            try:
                subprocess.run(
                    ["docker", "rm", "-f", name], check=True, capture_output=True, timeout=30
                )
            except Exception as exc:
                cleanup_errors.append(type(exc).__name__)
        listener.shutdown()
        listener.server_close()
        thread.join(timeout=5)
    require(not cleanup_errors and not thread.is_alive(), "Control cleanup did not finish")
    return {
        "protocol": "HTTP over IPv4 TCP",
        "destination": "host-gateway ephemeral listener",
        "observations": observations,
        "cleanup": True,
    }


def console_gateway(args: argparse.Namespace) -> dict[str, Any]:
    """Verify two Gateway sessions and prepared-module execution against the installed CLI."""
    import http.client

    from agent_framework import Content, MCPStreamableHTTPTool
    from openclaw_gateway_http_check import projected_result, records, verify_outcome
    from openclaw_http_observer import resolve_image

    installed_environment(getattr(args, "expected_versions", None))
    config = json.loads(args.config.read_text(encoding="utf-8-sig"))
    image = resolve_image(args.image)
    config_digest = sha(args.bicep_config.read_bytes())
    owner = "openclaw-bicep-" + (args.owner / "owner").read_text().strip()

    def empty() -> bool:
        return not subprocess.check_output(
            ["docker", "ps", "-aq", "--filter", f"label=maf-sandbox.scope={owner}"], text=True
        ).strip()

    report: dict[str, Any] = {
        "sessions": [],
        "session_ids": [],
        "entry_point": "maf-bicep-mcp",
        "provider": "deterministic-local-fixture",
    }
    for _ in range(2):
        session = uuid.uuid4().hex
        report["session_ids"].append(session)
        results = {}
        for case in ("valid", "invalid", "incomplete"):
            turn = uuid.uuid4().hex
            connection = http.client.HTTPConnection(
                "127.0.0.1", config["gateway"]["port"], timeout=180
            )
            try:
                connection.request(
                    "POST",
                    "/v1/chat/completions",
                    json.dumps(
                        {
                            "model": "openclaw",
                            "user": session,
                            "messages": [
                                {
                                    "role": "user",
                                    "content": f"qualification {case} qualification-turn={turn}",
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
                payload = json.loads(response.read())
                require(
                    response.status == 200
                    and payload["choices"][0]["message"]["content"]
                    == "QUALIFICATION_TOOL_RESULT_RECEIVED",
                    "Gateway turn did not finish",
                )
            finally:
                connection.close()
            results[case] = verify_outcome(
                projected_result(records(args.evidence), turn, case), case, config_digest, image
            )
            require(empty(), "Console call left owned resources")
        report["sessions"].append(results)

    async def prepared():
        server = config["mcp"]["servers"]["bicep"]
        from openclaw_sources import PREPARED_SOURCE, source_digest

        source = PREPARED_SOURCE
        async with MCPStreamableHTTPTool(
            name="prepared",
            url=server["url"],
            static_headers=server["headers"],
            load_prompts=False,
            request_timeout=240,
        ) as client:
            content = await client.call_tool(
                "bicep_validate", files=[{"path": "main.bicep", "content": source}]
            )
            if (
                not isinstance(content, list)
                or len(content) != 1
                or not isinstance(content[0], Content)
                or not isinstance(content[0].text, str)
            ):
                raise RuntimeError("Unexpected MAF result projection")
            result = json.loads(content[0].text)
            require(
                result["completed"] is True
                and result["verdict"] == "valid"
                and result["cleanup"] == "confirmed",
                "Prepared module did not validate",
            )
            require(
                result["image"] == image
                and result["config_sha256"] == config_digest
                and result.get("source_sha256") == source_digest(PREPARED_SOURCE),
                "Prepared identity differs",
            )
            require(empty(), "Prepared call left owned resources")
            return {
                k: result[k]
                for k in (
                    "completed",
                    "verdict",
                    "status",
                    "cleanup",
                    "source_sha256",
                    "config_sha256",
                    "image",
                )
            }

    report["prepared_module"] = asyncio.run(prepared())
    report["owned_empty"] = empty()
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("wheel", "root", "config"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--gateway-config", type=Path)
    parser.add_argument("--provider-evidence", type=Path)
    parser.add_argument("--owner", type=Path)
    parser.add_argument("--openclaw", type=Path)
    args = parser.parse_args()
    args.wheel = args.wheel.resolve()
    args.config = args.config.resolve()
    for name in ("gateway_config", "provider_evidence", "owner", "openclaw"):
        value = getattr(args, name)
        if value is not None:
            setattr(args, name, value.resolve())
    repo = Path(__file__).resolve().parents[2]
    root = args.root.resolve()
    require(not root.is_relative_to(repo), "Evidence must be outside checkout")
    require(not root.exists(), "Use a fresh evidence directory")
    require(
        not subprocess.check_output(
            ["git", "-C", str(repo), "status", "--porcelain"], text=True
        ).strip(),
        "Commit the candidate before qualification",
    )
    require(
        sys.flags.optimize == 0 and not os.environ.get("PYTHONPATH"),
        "Refuse optimized or source-injected execution",
    )
    root.mkdir(parents=True)
    os.chdir(root)
    os.environ["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    os.environ.pop("PYTEST_ADDOPTS", None)
    os.environ.pop("PYTEST_PLUGINS", None)
    report: dict[str, Any] = {
        "candidate": subprocess.check_output(
            ["git", "-C", str(repo), "rev-parse", "HEAD"], text=True
        ).strip(),
        "python": platform.python_version(),
        "platform": platform.system(),
        "passed": False,
    }
    started = time.monotonic()
    try:
        report["environment"] = installed_environment()
        report["artifact"] = verify_wheel(args.wheel.resolve())
        if args.gateway_config is not None:
            if args.owner is None or args.provider_evidence is None or args.openclaw is None:
                raise RuntimeError(
                    "Gateway mode needs owner, provider evidence and pinned OpenClaw"
                )
            require(
                json.loads((args.openclaw / "package.json").read_text())["version"] == "2026.9.7",
                "Requalify changed OpenClaw baseline",
            )
            args.bicep_config = args.config
            args.config = args.gateway_config
            args.evidence = args.provider_evidence
            report["gateway"] = console_gateway(args)
        else:
            report["transport"] = transport(root, repo)
            from openclaw_bicep_ownership_check import check
            from openclaw_http_observer import resolve_image

            args.root = root / "ownership"
            args.root.mkdir()
            args.installed = True
            report["ownership"] = asyncio.run(check(args))
            report["network"] = networking(resolve_image(args.image))
        require(
            report["artifact"] == verify_wheel(args.wheel.resolve()),
            "Installed artifact changed during execution",
        )
        report["passed"] = True
    finally:
        report["elapsed_seconds"] = time.monotonic() - started
        (root / "attempt.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    (root / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "passed": True,
                "checks": sorted(set(report) & {"transport", "ownership", "network", "gateway"}),
            }
        )
    )


if __name__ == "__main__":
    main()
