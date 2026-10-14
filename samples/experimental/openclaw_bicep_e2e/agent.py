"""Run a real OpenClaw Gateway against an installed Bicep MCP worker."""

# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import time
import uuid
import zipfile
from pathlib import Path
from typing import Any, TextIO
from urllib.parse import urlsplit

from _evidence import INVALID, check_turn, require
from _provider import Provider
from _windows_job import Job

OPENCLAW = "2026.9.7"
HERE = Path(__file__).resolve().parent
FLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def execute(command: list[str], root: Path, *, timeout: int = 1200) -> str:
    """Run setup with private diagnostics and a finite deadline."""
    with (root / "setup.log").open("a", encoding="utf-8") as log:
        result = subprocess.run(
            command,
            cwd=root,
            stdout=subprocess.PIPE,
            stderr=log,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            creationflags=FLAGS,
            check=True,
        )
    return result.stdout.strip()


def program(name: str) -> str:
    """Resolve a required host executable."""
    result = shutil.which(name)
    require(result, f"Install {name} and place it on PATH")
    assert result is not None
    return result


def free_port() -> int:
    """Choose an available loopback port; startup still checks the binding."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def request(
    port: int, path: str, token: str, body: dict[str, Any] | None = None
) -> tuple[int, bytes]:
    """Send a bounded authenticated request to a local sample service."""
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=300 if body else 2)
    try:
        connection.request(
            "POST" if body else "GET",
            path,
            json.dumps(body) if body else None,
            {"Authorization": "Bearer " + token, "Content-Type": "application/json"},
        )
        response = connection.getresponse()
        payload = response.read(4 * 1024 * 1024 + 1)
        require(len(payload) <= 4 * 1024 * 1024, "Gateway response exceeds bounds")
        return response.status, payload
    finally:
        connection.close()


class Processes:
    """Own the Gateway and worker processes started by this sample."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.children: list[tuple[subprocess.Popen[bytes], TextIO]] = []
        self.jobs: dict[int, Job] = {}
        self.stopped = False

    def start(self, name: str, command: list[str], env: dict[str, str] | None = None) -> None:
        log = (self.root / (name + ".log")).open("w", encoding="utf-8")
        job = None
        child = None
        try:
            if os.name == "nt":
                job = Job()
                command = [
                    sys.executable,
                    "-I",
                    "-c",
                    "import subprocess,sys; sys.exit(subprocess.call(sys.argv[1:], "
                    "stdin=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW) "
                    "if sys.stdin.buffer.read(1)==b'1' else 1)",
                    *command,
                ]
            child = subprocess.Popen(
                command,
                cwd=self.root,
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
                stdin=subprocess.PIPE if job else subprocess.DEVNULL,
                creationflags=FLAGS,
                start_new_session=os.name != "nt",
            )
            self.children.append((child, log))
            if job is not None:
                self.jobs[child.pid] = job
                job.assign(child.pid)
                assert child.stdin is not None
                child.stdin.write(b"1")
                child.stdin.close()
        except BaseException:
            if child is None:
                log.close()
                if job is not None:
                    job.close()
            raise

    def ready(self, port: int, path: str, token: str) -> None:
        end = time.monotonic() + 120
        while time.monotonic() < end:
            require(
                all(child.poll() is None for child, _ in self.children),
                "A sample process exited; inspect its private log",
            )
            try:
                if request(port, path, token)[0] == 200:
                    return
            except OSError:
                # The listener may not be bound yet; retain the delay between probes.
                pass
            time.sleep(0.25)
        raise RuntimeError("Readiness timed out; inspect private logs")

    def group_exists(self, child: subprocess.Popen[bytes]) -> bool:
        """Count the ownership unit, including descendants after the leader exits."""
        if os.name == "nt":
            return self.jobs[child.pid].active() != 0
        try:
            os.killpg(child.pid, 0)
        except ProcessLookupError:
            return False
        return True

    def wait_empty(self, child: subprocess.Popen[bytes], timeout: float) -> bool:
        end = time.monotonic() + timeout
        while True:
            child.poll()
            if not self.group_exists(child):
                return True
            if time.monotonic() >= end:
                return False
            time.sleep(0.05)

    def close(self) -> list[str]:
        errors = []
        self.stopped = False
        for child, log in reversed(self.children):
            try:
                if os.name == "nt":
                    self.jobs[child.pid].terminate()
                    # An assignment failure leaves only the gated wrapper outside the job.
                    if child.poll() is None:
                        child.kill()
                else:
                    try:
                        os.killpg(child.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass  # An empty ownership group is already settled.
                if not self.wait_empty(child, 30):
                    if os.name != "nt":
                        try:
                            os.killpg(child.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass  # The group can settle between the probe and escalation.
                    require(self.wait_empty(child, 10), "Owned processes remain after termination")
                child.wait(timeout=10)
            except Exception as exc:
                errors.append(type(exc).__name__)
            finally:
                if child.stdin is not None:
                    try:
                        child.stdin.close()
                    except OSError as exc:
                        errors.append(type(exc).__name__)
                log.close()
                if os.name == "nt":
                    try:
                        self.jobs[child.pid].close()
                    except Exception as exc:
                        errors.append(type(exc).__name__)
        self.stopped = not errors and all(child.poll() is not None for child, _ in self.children)
        return errors


def worker_environment() -> dict[str, str]:
    """Keep source-path overrides out of the installed console's imports."""
    return {
        **{key: value for key, value in os.environ.items() if not key.upper().startswith("PYTHON")},
        "PYTHONNOUSERSITE": "1",
    }


def install(args: argparse.Namespace, root: Path) -> tuple[Path, Path, dict[str, Any]]:
    """Install the wheel outside the checkout and provision the pinned Gateway."""
    uv, node = program("uv"), program("node")
    environment = root / "worker"
    execute([uv, "venv", "--python", "3.13", str(environment)], root)
    python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    execute(
        [
            uv,
            "pip",
            "install",
            "--python",
            str(python),
            str(args.wheel),
            "maf-sandbox==0.48.0",
            "maf-sandbox-bicep==0.22.2",
            "maf-sandbox-docker==0.27.0",
            "agent-framework-core==1.20.0",
        ],
        root,
    )
    probe = """import hashlib,importlib.metadata as m,json,pathlib,sys
import maf_sandbox_mcp
p=pathlib.Path(maf_sandbox_mcp.__file__).resolve().parent
assert p.is_relative_to(pathlib.Path(sys.prefix).resolve())
for d in m.distributions():
 u=d.read_text('direct_url.json')
 if u:
  v=json.loads(u)
  assert not v.get('dir_info',{}).get('editable')
  assert d.metadata['Name']=='maf-sandbox-mcp', 'Unexpected source dependency'
print(json.dumps({'versions':{d.metadata['Name']:d.version for d in m.distributions()},
 'modules':{f.name:hashlib.sha256(f.read_bytes()).hexdigest() for f in p.glob('*.py')}}))
"""
    identity = json.loads(execute([str(python), "-I", "-c", probe], root))
    with zipfile.ZipFile(args.wheel) as wheel:
        expected = {
            Path(name).name: hashlib.sha256(wheel.read(name)).hexdigest()
            for name in wheel.namelist()
            if name.startswith("maf_sandbox_mcp/") and name.endswith(".py")
        }
    require(
        set(expected) == {"__init__.py", "server.py", "workload_service.py", "workload_http.py"},
        "Incomplete worker wheel",
    )
    require(identity["modules"] == expected, "Installed modules differ from the wheel")
    identity["wheel_sha256"] = hashlib.sha256(args.wheel.read_bytes()).hexdigest()
    claw = args.openclaw
    if claw is None:
        target = root / "openclaw"
        execute(
            [
                program("npm.cmd" if os.name == "nt" else "npm"),
                "install",
                "--prefix",
                str(target),
                "--ignore-scripts",
                "--no-audit",
                "--no-fund",
                "openclaw@" + OPENCLAW,
            ],
            root,
        )
        claw = target / "node_modules/openclaw"
        execute([node, str(claw / "scripts/postinstall-bundled-plugins.mjs")], root)
    require(
        json.loads((claw / "package.json").read_text(encoding="utf-8"))["version"] == OPENCLAW,
        "Use the pinned OpenClaw version; qualify upgrades separately",
    )
    return python, claw, identity


def configuration(
    root: Path,
    provider: Provider,
    worker_port: int,
    gateway_port: int,
    worker_token: str,
    gateway_token: str,
) -> dict[str, Any]:
    """Keep all model-visible authority in the single Bicep tool."""
    return {
        "agents": {
            "defaults": {
                "workspace": str(root / "workspace"),
                "skipBootstrap": True,
                "model": {"primary": "sample/sample"},
                "heartbeat": {"every": "0m"},
            }
        },
        "mcp": {
            "servers": {
                "bicep": {
                    "url": f"http://127.0.0.1:{worker_port}/mcp",
                    "transport": "streamable-http",
                    "headers": {"Authorization": "Bearer " + worker_token},
                    "enabled": True,
                    "connectionTimeoutMs": 60000,
                    "requestTimeoutMs": 240000,
                }
            }
        },
        "gateway": {
            "controlUi": {"enabled": False},
            "mode": "local",
            "bind": "loopback",
            "port": gateway_port,
            "auth": {"mode": "token", "token": gateway_token},
            "http": {"endpoints": {"chatCompletions": {"enabled": True}}},
        },
        "plugins": {"enabled": False},
        "tools": {"allow": ["bicep__bicep_validate"]},
        "discovery": {"mdns": {"mode": "off"}},
        "models": {
            "mode": "replace",
            "providers": {
                "sample": {
                    "api": "openai-completions",
                    "apiKey": provider.token,
                    "baseUrl": provider.url,
                    "models": [
                        {
                            "id": "sample",
                            "name": "Sample provider",
                            "contextWindow": 128000,
                            "maxTokens": 2048,
                            "input": ["text"],
                            "reasoning": False,
                        }
                    ],
                }
            },
        },
    }


def model_settings(mode: str) -> tuple[str, str, str]:
    """Read model credentials only for explicitly selected model mode."""
    if mode == "smoke":
        return "", "", ""
    base = os.environ.get("OPENAI_BASE_URL", "")
    model = os.environ.get("OPENAI_MODEL", "")
    key = os.environ.get("OPENAI_API_KEY", "")
    url = urlsplit(base)
    local = url.hostname in {"localhost", "127.0.0.1", "::1"}
    require(
        url.hostname
        and model
        and not url.username
        and not url.password
        and not url.query
        and not url.fragment,
        "Set OPENAI_BASE_URL and OPENAI_MODEL; keep credentials out of the URL",
    )
    require(
        url.scheme == "https" or (url.scheme == "http" and local),
        "Remote model endpoints require HTTPS",
    )
    require(key or local, "Set OPENAI_API_KEY for the selected remote provider")
    return base, model, key or "local"


def run(args: argparse.Namespace) -> dict[str, Any]:
    """Execute two turns and retain a report even when acceptance fails."""
    upstream, model, key = model_settings(args.mode)
    root = args.output
    root.mkdir(parents=True, mode=0o700)
    for part in ("home", "state", "workspace"):
        (root / part).mkdir(mode=0o700)
    (root / "home/AppData/Local/OpenClaw/locks/openclaw-state-owners").mkdir(parents=True)
    python: Path | None = None
    provider: Provider | None = None
    processes = Processes(root)
    report: dict[str, Any] = {
        "passed": False,
        "mode": args.mode,
        "openclaw": OPENCLAW,
        "actual_model": args.mode == "model",
        "cleanup_errors": [],
    }
    scope = ""
    docker = program("docker")

    def containers() -> list[str]:
        return (
            execute(
                [docker, "ps", "-aq", "--filter", "label=maf-sandbox.scope=" + scope],
                root,
                timeout=30,
            ).split()
            if scope
            else []
        )

    try:
        python, claw, report["artifact"] = install(args, root)
        image = json.loads(execute([docker, "image", "inspect", args.image], root, timeout=30))[0][
            "Id"
        ]
        report["image"] = image
        config_file = root / "bicepconfig.json"
        config_file.write_bytes((HERE / "bicepconfig.json").read_bytes())
        config_hash = hashlib.sha256(config_file.read_bytes()).hexdigest()
        provider = Provider(root / "tool-evidence.jsonl", upstream, model, key)
        provider.start()
        worker_port, gateway_port = free_port(), free_port()
        require(worker_port != gateway_port, "Port allocation collided; rerun with fresh output")
        worker_token, gateway_token = secrets.token_hex(32), secrets.token_hex(32)
        token_file = root / "worker-token"
        token_file.write_text(worker_token, encoding="utf-8")
        token_file.chmod(0o600)
        worker = python.with_name("maf-bicep-mcp.exe" if os.name == "nt" else "maf-bicep-mcp")
        processes.start(
            "worker",
            [
                str(worker),
                "--transport",
                "http",
                "--port",
                str(worker_port),
                "--token-file",
                str(token_file),
                "--image",
                image,
                "--config",
                str(config_file),
                "--state-dir",
                str(root / "owner"),
            ],
            worker_environment(),
        )
        processes.ready(worker_port, "/ready", worker_token)
        owner = (root / "owner/owner").read_text().strip()
        require(re.fullmatch(r"[0-9a-f]{32}", owner), "Invalid worker owner")
        scope = "openclaw-bicep-" + owner
        config = root / "state/openclaw.json"
        config.write_text(
            json.dumps(
                configuration(
                    root, provider, worker_port, gateway_port, worker_token, gateway_token
                )
            ),
            encoding="utf-8",
        )
        config.chmod(0o600)
        env = {
            k: v
            for k, v in os.environ.items()
            if not k.startswith("OPENCLAW_")
            and k not in {"OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL"}
        }
        env.update(
            OPENCLAW_CONFIG_PATH=str(config),
            OPENCLAW_STATE_DIR=str(root / "state"),
            OPENCLAW_HOME=str(root / "home"),
            HOME=str(root / "home"),
            USERPROFILE=str(root / "home"),
            OPENCLAW_NO_RESPAWN="1",
            OPENCLAW_SKIP_CHANNELS="1",
        )
        processes.start(
            "gateway", [program("node"), str(claw / "openclaw.mjs"), "gateway", "run"], env
        )
        processes.ready(gateway_port, "/health", gateway_token)
        session = "sample-" + uuid.uuid4().hex
        source = (HERE / "main.bicep").read_text(encoding="utf-8").strip()
        require(source == INVALID, "The sample fixture must retain its deliberate type error")
        prompts = [
            f"Use the Bicep validation tool to validate main.bicep containing exactly:\n```bicep\n{source}\n```\nDo not change the source. Report the compiler result.",
            "Repair the type error in the previous main.bicep, preserving the greeting hello. Keep the repair to a single output declaration, validate it with the Bicep tool, and report the compiler result.",
        ]
        report["turns"] = []
        for index, prompt in enumerate(prompts):
            turn = uuid.uuid4().hex
            status, data = request(
                gateway_port,
                "/v1/chat/completions",
                gateway_token,
                {
                    "model": "openclaw",
                    "user": session,
                    "messages": [{"role": "user", "content": prompt + " sample-turn=" + turn}],
                },
            )
            require(status == 200, "Gateway turn failed; inspect private logs")
            answer = json.loads(data)["choices"][0]["message"]["content"]
            (root / f"answer-{index + 1}.json").write_text(json.dumps(answer), encoding="utf-8")
            require(not provider.errors, "Provider failed during the turn")
            measured = check_turn(
                provider.records, turn, repaired=index == 1, image=image, config_hash=config_hash
            )
            require(not containers(), "Owned containers remain after a completed turn")
            report["turns"].append(measured)
            print("[measured] " + json.dumps({"turn": index + 1, **measured}), flush=True)
        report["passed"] = True
    except Exception as exc:
        report["error"] = str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__
    finally:
        report["cleanup_errors"].extend(processes.close())
        if provider is not None:
            try:
                provider.close()
            except Exception as exc:
                report["cleanup_errors"].append("Provider cleanup: " + type(exc).__name__)
        try:
            if not scope and (root / "owner/owner").exists():
                owner = (root / "owner/owner").read_text().strip()
                require(re.fullmatch(r"[0-9a-f]{32}", owner), "Invalid retained owner")
                scope = "openclaw-bicep-" + owner
            remaining = containers()
            if remaining:
                report["cleanup_errors"].append(
                    "Worker stopped with owned containers; emergency purge required"
                )
                execute([docker, "rm", "-f", *remaining], root, timeout=60)
            report["owned_empty"] = not containers()
            if scope and python is not None:
                execute(
                    [
                        str(python),
                        "-I",
                        "-c",
                        "from pathlib import Path; from maf_sandbox_mcp.server import ownership; import sys; c=ownership(Path(sys.argv[1])); c.__enter__(); c.__exit__(None,None,None)",
                        str(root / "owner"),
                    ],
                    root,
                    timeout=30,
                )
                report["owner_lock_released"] = True
        except Exception as exc:
            report["cleanup_errors"].append(type(exc).__name__)
        report["processes_stopped"] = processes.stopped
        report["stop_method"] = (
            "Windows job termination with zero active processes"
            if os.name == "nt"
            else "POSIX process-group termination with confirmed absence"
        )
        report["passed"] = bool(
            report["passed"]
            and not report["cleanup_errors"]
            and report.get("owned_empty")
            and report.get("owner_lock_released")
            and report["processes_stopped"]
        )
        (root / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return report


def main() -> None:
    """Validate operator inputs before creating the isolated sample environment."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("smoke", "model"), default="smoke")
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument(
        "--image",
        required=True,
        help="Already pulled immutable Docker image ID or repository digest",
    )
    parser.add_argument(
        "--output", type=Path, required=True, help="New private directory outside the checkout"
    )
    parser.add_argument(
        "--openclaw", type=Path, help="Reuse this pinned OpenClaw package instead of installing it"
    )
    args = parser.parse_args()
    args.wheel, args.output = args.wheel.resolve(), args.output.resolve()
    if args.openclaw:
        args.openclaw = args.openclaw.resolve()
    require(args.wheel.is_file(), "Build the worker wheel first")
    require(not args.output.exists(), "Use a new output directory")
    require(
        not args.output.is_relative_to(HERE.parents[2]), "Keep private output outside the checkout"
    )
    require(
        re.fullmatch(r"(?:sha256:|[^\s]+@sha256:)[0-9a-f]{64}", args.image),
        "Supply an immutable image identity",
    )
    report = run(args)
    print(
        "[measured] "
        + json.dumps(
            {
                k: report[k]
                for k in ("passed", "mode", "actual_model", "processes_stopped", "cleanup_errors")
            }
        )
    )
    if not report["passed"]:
        raise SystemExit("Sample failed; inspect report.json and private logs in --output")


if __name__ == "__main__":
    main()
