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


def installed_environment() -> dict[str, Any]:
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
        all(versions.get(k) == v for k, v in BASELINE.items()),
        "Requalify changed dependency baseline explicitly",
    )
    for name, module_name in MODULES.items():
        module = importlib.import_module(module_name)
        location = Path(module.__file__).resolve()
        root = Path(str(importlib.metadata.distribution(name).locate_file(""))).resolve()
        require(
            location.is_relative_to(root) and "site-packages" in location.parts,
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


def networking(image: str) -> dict[str, Any]:
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
            name = "mcp-network-" + uuid.uuid4().hex
            created.append(name)
            command = [
                "docker",
                "run",
                "--name",
                name,
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
        for name in created:
            subprocess.run(
                ["docker", "rm", "-f", name], check=True, capture_output=True, timeout=30
            )
        listener.shutdown()
        listener.server_close()
        thread.join(timeout=5)
    require(not thread.is_alive(), "Control server did not stop")
    return {
        "protocol": "HTTP over IPv4 TCP",
        "destination": "host-gateway ephemeral listener",
        "observations": observations,
        "cleanup": True,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("wheel", "root", "config"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--image", required=True)
    args = parser.parse_args()
    args.wheel = args.wheel.resolve()
    args.config = args.config.resolve()
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
                "transport_cases": len(report["transport"]["cases"]),
                "retirements": len(report["transport"]["retirements"]),
                "ownership_cases": list(report["ownership"]["cases"]),
            }
        )
    )


if __name__ == "__main__":
    main()
