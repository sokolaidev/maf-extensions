"""Run one explicitly partial official-MXC smoke mode through an installed Gateway."""

from __future__ import annotations

import argparse
import hashlib
import http.client
import importlib
import json
import os
import platform
import secrets
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from .commands import CaptureRefused, capture
from .evidence import Refused, decode, file_hash, require
from .install import REPO, SAMPLE, environment
from .mxc_smoke_process import native_ancestor, sample
from .mxc_smoke_provider import LIMIT, Provider

VERSION = "2026.10.5"
EXECUTOR = "819ecd9b08a8dcb2376d489c0352087468bf56800ab059f57d38901d7743049e"
PROFILE = "official-mxc-smoke-windows-v1"
MODES = ("none", "ro", "rw")
PLUGIN_ASSETS = {
    "package.json": "9841367e9cd38fb54aa104a7bebb467117ad57aeadeb8911258dddc70796c8d6",
    "openclaw.plugin.json": "d4264961060829ecdefb37dcf1887d4d040f0a0ebd638a4b981097f58b9b101f",
    "dist/index.js": "bdc382abc7db2e831479f2f287025e13c1452b802e4293329454baa3efd7fe77",
    "dist/mxc-spawn-launcher.mjs": "4fa0a7005094bcd324f0e58018cb2dad57dee809134bc948b723bfb8e7f98165",
    "node_modules/@microsoft/mxc-sdk/package.json": "4ea38a9d96ee579ebd26535e3ebf37f4094a99d9ec52aefce2756049c9990882",
}


def identities(installation: Path) -> dict[str, str]:
    """Bind the selected published package metadata and execution assets."""
    modules = installation / "node_modules"
    plugin = modules / "@openclaw/mxc-sandbox"
    sdk = plugin / "node_modules/@microsoft/mxc-sdk"
    for root, name, version in (
        (modules / "openclaw", "openclaw", VERSION),
        (plugin, "@openclaw/mxc-sandbox", VERSION),
        (sdk, "@microsoft/mxc-sdk", "0.9.0"),
    ):
        metadata = decode((root / "package.json").read_bytes())
        require(metadata["name"] == name and metadata["version"] == version, "candidate_version")
    require(
        all(file_hash(plugin / name) == expected for name, expected in PLUGIN_ASSETS.items()),
        "plugin_asset_identity",
    )
    paths = [
        installation / "package-lock.json",
        modules / "openclaw/package.json",
        modules / "openclaw/openclaw.mjs",
        plugin / "package.json",
        plugin / "openclaw.plugin.json",
        plugin / "dist/index.js",
        plugin / "dist/mxc-spawn-launcher.mjs",
        sdk / "package.json",
        sdk / "bin/x64/wxc-exec.exe",
    ]
    result = {p.relative_to(installation).as_posix(): file_hash(p) for p in paths}
    require(result[paths[-1].relative_to(installation).as_posix()] == EXECUTOR, "executor_identity")
    return result


def configuration(
    root: Path,
    plugin: Path,
    mode: str,
    port: int,
    token: str,
    provider: Provider,
    *,
    debug: bool = False,
) -> dict[str, Any]:
    """Use the public plugin configuration without extra filesystem grants."""
    require(mode in MODES, "workspace_mode")
    return {
        "agents": {
            "defaults": {
                "workspace": str(root / "agent"),
                "skipBootstrap": True,
                "skills": [],
                "model": {"primary": "smoke/smoke"},
                "heartbeat": {"every": "0m"},
                "sandbox": {
                    "mode": "all",
                    "backend": "mxc",
                    "scope": "agent",
                    "workspaceAccess": mode,
                    "workspaceRoot": str(root / "private"),
                },
            }
        },
        "plugins": {
            "slots": {"memory": "none"},
            "load": {"paths": [str(plugin)]},
            "allow": ["mxc"],
            "entries": {
                "mxc": {
                    "enabled": True,
                    "config": {
                        "mxcBinaryPath": str(
                            plugin / "node_modules/@microsoft/mxc-sdk/bin/x64/wxc-exec.exe"
                        ),
                        **({"debug": True} if debug else {}),
                        "containment": "process",
                        "network": "none",
                        "timeoutSeconds": 30,
                    },
                }
            },
        },
        "gateway": {
            "mode": "local",
            "bind": "loopback",
            "port": port,
            "auth": {"mode": "token", "token": token},
            "controlUi": {"enabled": False},
            "http": {"endpoints": {"chatCompletions": {"enabled": True}}},
        },
        "tools": {
            "codeMode": {"enabled": False},
            "allow": ["exec", "read", "write"],
            "exec": {"host": "sandbox", "timeoutSeconds": 20},
            "elevated": {"enabled": False},
        },
        "skills": {"allowBundled": []},
        "discovery": {"mdns": {"mode": "off"}},
        "models": {
            "mode": "replace",
            "providers": {
                "smoke": {
                    "agentRuntime": {"id": "openclaw"},
                    "api": "openai-completions",
                    "apiKey": provider.token,
                    "baseUrl": provider.url,
                    "models": [
                        {
                            "id": "smoke",
                            "name": "Smoke",
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


def workspace(explanation: dict[str, Any], root: Path, mode: str) -> Path:
    """Accept only the public CLI's sandboxed, campaign-owned workspace."""
    value = explanation["sandbox"]
    require(value["backend"] == "mxc" and value["sessionIsSandboxed"] is True, "sandbox_selection")
    require(value["workspaceAccess"] == mode and value["scope"] == "agent", "sandbox_policy")
    path = Path(value["effectiveHostWorkspaceRoot"]).resolve()
    expected = root / ("agent" if mode == "rw" else "private")
    require(
        path == expected if mode == "rw" else path.is_relative_to(expected) and path != expected,
        "workspace_ownership",
    )
    return path


def native_script(active: Path, agent: Path, outside: Path, nonce: str) -> str:
    """Attempt only finite reads and writes of the campaign's planted markers."""

    def quoted(path: Path) -> str:
        return "'" + str(path).replace("'", "''") + "'"

    statements = [
        "$ErrorActionPreference='Stop'",
        "$PSModuleAutoLoadingPreference='None'",
        f"[Console]::WriteLine('smoke-start={nonce}')",
        "[Console]::WriteLine('guest-pid='+$PID)",
    ]
    for label, path in (
        ("active-read", active / "marker.txt"),
        ("agent-read", agent / "agent-marker.txt"),
    ):
        statements.append(
            f"try {{ [Console]::WriteLine('{label}='+[IO.File]::ReadAllText({quoted(path)})) }} catch {{ [Console]::WriteLine('{label}=denied') }}"
        )
    for label, path in (
        ("active-write", active / "native-write.txt"),
        ("agent-write", agent / "agent-write.txt"),
        ("outside-write", outside),
    ):
        statements.append(
            f"try {{ [IO.File]::WriteAllText({quoted(path)},'{nonce}'); [Console]::WriteLine('{label}=allowed') }} catch {{ [Console]::WriteLine('{label}=denied') }}"
        )
    statements.extend(
        ["[Threading.Thread]::Sleep(5000)", f"[Console]::WriteLine('smoke-end={nonce}')"]
    )
    return "; ".join(statements)


def native_passed(
    result: str, observed: set[int], active: Path, agent: Path, outside: Path, mode: str, nonce: str
) -> bool:
    """Require real execution plus independent positive and negative file controls."""
    import re

    pids = re.findall(r"(?m)^guest-pid=(\d+)\r?$", result)
    expected_write = nonce if mode == "rw" else "before"
    return (
        len(pids) == 1
        and int(pids[0]) in observed
        and f"smoke-start={nonce}" in result
        and f"smoke-end={nonce}" in result
        and f"active-read={nonce}" in result
        and (f"agent-read={nonce}" if mode != "none" else "agent-read=denied") in result
        and (active / "native-write.txt").read_text() == expected_write
        and (agent / "agent-write.txt").read_text() == expected_write
        and outside.read_text() == "outside-before"
        and ("active-write=allowed" if mode == "rw" else "active-write=denied") in result
        and ("agent-write=allowed" if mode == "rw" else "agent-write=denied") in result
        and "outside-write=denied" in result
    )


def native_diagnostics(root: Path) -> list[str]:
    """Extract bounded native events without treating them as independent evidence."""
    folder = root / "temp/mxc-logs"
    if not folder.resolve().is_relative_to(root.resolve()):
        return ["Native diagnostic directory refused."]
    logs = sorted(folder.glob("*.log"))
    lines = []
    for log in logs[:16]:
        if log.is_symlink() or not log.resolve().is_relative_to(root.resolve()):
            lines.append("Native diagnostic path refused.")
            continue
        try:
            with log.open("rb") as stream:
                raw = stream.read(65537)
        except OSError:
            lines.append("Native diagnostic log unavailable; extraction is incomplete.")
            continue
        if len(raw) <= 65536:
            lines.append(f"- Log SHA-256: `{hashlib.sha256(raw).hexdigest()}`.")
        else:
            lines.append("- Diagnostic log exceeded 64 KiB; extraction is incomplete.")
        for line in raw[:65536].decode("utf-8", errors="replace").splitlines():
            if any(
                marker in line
                for marker in (
                    "selected isolation tier:",
                    '"event":"mxc.ProcessExited"',
                    '"event":"mxc.EnforcementDegraded"',
                    '"event":"mxc.SandboxTornDown"',
                )
            ):
                lines.append("> " + line)
    if len(logs) > 16:
        lines.append("Only the first 16 native logs were inspected.")
    return lines or ["No selected native diagnostic events were available."]


class Campaign:
    """Own a fresh Gateway and observe only its finite smoke turns."""

    def __init__(self, root: Path, installation: Path, mode: str, *, debug: bool = False) -> None:
        sys.path.insert(0, str(SAMPLE))
        self.helpers = importlib.import_module("agent")
        self.root, self.installation, self.mode = root, installation, mode
        self.debug = debug
        self.provider = Provider()
        self.processes = self.helpers.Processes(root)
        self.port, self.token = self.helpers.free_port(), secrets.token_hex(32)
        self.end = time.monotonic() + 1200
        self.rows: list[tuple[str, bool, str]] = []
        self.observed: set[int] = set()
        self.cleanup: list[str] = []
        self.manifest: dict[str, str] = {}
        self.config_hash = "not-created"
        self.setup: list[str] = []
        self.prerequisites_only = False
        self.prerequisites_complete = False
        self.native = (
            installation
            / "node_modules/@openclaw/mxc-sandbox/node_modules/@microsoft/mxc-sdk/bin/x64/wxc-exec.exe"
        )

    def check(self) -> None:
        require(time.monotonic() < self.end, "campaign_deadline")
        require(all(p.poll() is None for p, _ in self.processes.children), "gateway_exited")
        log = self.root / "gateway.log"
        require(not log.exists() or log.stat().st_size <= 8 * 1024 * 1024, "gateway_log_limit")

    def turn(self, tool: str, arguments: dict[str, Any]) -> str:
        """Collect the correlated tool result, independently of the final assistant text."""
        self.check()
        self.observed.clear()
        self.provider.select(secrets.token_hex(16), tool, arguments)
        stop = threading.Event()
        errors: list[str] = []
        job = next(iter(self.processes.jobs.values()))

        def observe() -> None:
            try:
                while not stop.is_set():
                    self.check()
                    rows = sample(job, self.native)
                    self.observed.update(
                        pid for pid in rows if native_ancestor(rows, pid, EXECUTOR)
                    )
                    stop.wait(0.2)
            except Exception as exc:
                errors.append(type(exc).__name__)
                job.terminate()

        observer = threading.Thread(target=observe, daemon=True)
        observer.start()
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.port, timeout=min(300, max(1, self.end - time.monotonic()))
        )
        try:
            connection.request(
                "POST",
                "/v1/chat/completions",
                json.dumps(
                    {
                        "model": "openclaw",
                        "user": "smoke-" + self.provider.turn,
                        "stream": False,
                        "messages": [
                            {
                                "role": "user",
                                "content": "Run the fixed smoke call. smoke-turn="
                                + self.provider.turn,
                            }
                        ],
                    }
                ),
                {"Authorization": "Bearer " + self.token, "Content-Type": "application/json"},
            )
            response = connection.getresponse()
            raw = response.read(LIMIT + 1)
            require(response.status == 200 and len(raw) <= LIMIT, "gateway_response")
        finally:
            connection.close()
            stop.set()
            observer.join(timeout=10)
        require(not observer.is_alive() and not errors, "native_observer")
        require(
            self.provider.calls == 1
            and len(self.provider.results) == 1
            and not self.provider.errors,
            "tool_result_missing",
        )
        return str(self.provider.results[0])

    def setup_command(
        self, label: str, args: list[str], env: dict[str, str], timeout: int
    ) -> subprocess.CompletedProcess[bytes]:
        """Retain bounded phase diagnostics without counting them as smoke controls."""
        log = self.root / "setup.log"
        offset = log.stat().st_size if log.exists() else 0
        started = time.monotonic()
        self.setup.extend(["", f"### {label}", f"Deadline: {timeout} seconds."])
        result = None
        partial = b""
        try:
            result = capture(args, self.root, env, timeout, diagnostics=True, stdout_limit=LIMIT)
            return result
        except CaptureRefused as exc:
            partial = exc.stdout
            raise
        finally:
            self.setup.append(f"Elapsed: {time.monotonic() - started:.3f} seconds.")
            self.setup.append(
                f"Exit code: {result.returncode}."
                if result is not None
                else "Command refused; exit status unavailable. Output may be partial."
            )
            error = b""
            if log.exists():
                with log.open("rb") as stream:
                    stream.seek(offset)
                    error = stream.read(16385)
            for channel, raw in (
                ("stdout", result.stdout if result else partial),
                ("stderr", error),
            ):
                if raw:
                    self.setup.extend(
                        [
                            channel + ":",
                            "```text",
                            raw[:16384].decode("utf-8", errors="replace").replace("```", "---"),
                            "```",
                        ]
                    )
                if len(raw) > 16384:
                    self.setup.append(f"{channel} exceeded 16 KiB; extraction is incomplete.")

    def execute(self, *, prerequisites_only: bool = False) -> None:
        """Run the declared selected-mode cases without adapting them to failures."""
        self.prerequisites_only = prerequisites_only
        self.manifest = identities(self.installation)
        plugin = self.installation / "node_modules/@openclaw/mxc-sandbox"
        config = self.root / "state/openclaw.json"
        config.write_text(
            json.dumps(
                configuration(
                    self.root,
                    plugin,
                    self.mode,
                    self.port,
                    self.token,
                    self.provider,
                    debug=self.debug,
                )
            ),
            encoding="utf-8",
        )
        self.config_hash = file_hash(config)
        env = {k: v for k, v in environment(self.root).items() if not k.startswith("DOCKER_")}
        env.update(
            OPENCLAW_CONFIG_PATH=str(config),
            OPENCLAW_STATE_DIR=str(self.root / "state"),
            OPENCLAW_HOME=str(self.root / "home"),
            HOME=str(self.root / "home"),
            USERPROFILE=str(self.root / "home"),
            OPENCLAW_NO_RESPAWN="1",
            OPENCLAW_SKIP_CHANNELS="1",
        )
        node = shutil.which("node")
        require(node is not None, "node_missing")
        assert node is not None
        command = [node, str(self.installation / "node_modules/openclaw/openclaw.mjs")]
        explained = self.setup_command(
            "sandbox explain prerequisite",
            [*command, "sandbox", "explain", "--json", "--agent", "main"],
            env,
            360,
        )
        if explained.returncode != 0:
            self.rows.append(
                (
                    "sandbox explain prerequisite",
                    False,
                    explained.stdout.decode("utf-8", errors="replace"),
                )
            )
        require(explained.returncode == 0, "sandbox_explain")
        active = workspace(decode(explained.stdout), self.root, self.mode)
        active.mkdir(parents=True, exist_ok=True)
        nonce = secrets.token_hex(16)
        for path, value in (
            (active / "marker.txt", nonce),
            (active / "native-write.txt", "before"),
            (active / "host-write.txt", "before"),
            (self.root / "agent/agent-marker.txt", nonce),
            (self.root / "agent/agent-write.txt", "before"),
            (self.root / "outside.txt", "outside-before"),
        ):
            path.write_text(value)
        outside = self.root / "outside.txt"
        script = active / "smoke.ps1"
        script.write_text(
            native_script(active, self.root / "agent", outside, nonce), encoding="utf-8-sig"
        )
        shell = Path(os.environ["SYSTEMROOT"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
        require(all(c not in str(script) + str(shell) for c in '"%&|<>^!'), "shell_path")
        guest_command = (
            f'"{shell}" -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "{script}"'
        )
        try:
            control = self.setup_command(
                "unrestricted fixture prerequisite",
                [
                    str(shell),
                    "-NoProfile",
                    "-NonInteractive",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(script),
                ],
                env,
                30,
            )
            control_text = control.stdout.decode("utf-8", errors="replace")
            permitted = (
                control.returncode == 0
                and f"smoke-end={nonce}" in control_text
                and f"active-read={nonce}" in control_text
                and f"agent-read={nonce}" in control_text
                and "active-write=allowed" in control_text
                and "agent-write=allowed" in control_text
                and "outside-write=allowed" in control_text
                and (active / "native-write.txt").read_text() == nonce
                and (self.root / "agent/agent-write.txt").read_text() == nonce
                and outside.read_text() == nonce
            )
            self.rows.append(("unsandboxed fixture positive control", permitted, control_text))
            require(permitted, "host_fixture_prerequisite")
        finally:
            (active / "native-write.txt").write_text("before")
            (self.root / "agent/agent-write.txt").write_text("before")
            outside.write_text("outside-before")
        self.prerequisites_complete = True
        if prerequisites_only:
            require(identities(self.installation) == self.manifest, "candidate_changed")
            require(file_hash(config) == self.config_hash, "configuration_changed")
            return
        self.provider.start()
        self.processes.start("gateway", [*command, "gateway", "run"], env)
        deadline = time.monotonic() + 360
        while time.monotonic() < deadline:
            self.check()
            try:
                if self.helpers.request(self.port, "/health", self.token)[0] == 200:
                    break
            except OSError:
                pass  # The owned Gateway has not bound its port yet.
            time.sleep(0.25)
        else:
            raise Refused("gateway_readiness")
        print("Gateway ready; executing selected-mode smoke cases", flush=True)
        outside = self.root / "outside.txt"
        result = self.turn(
            "exec",
            {
                "command": guest_command,
                "host": "sandbox",
                "timeoutSeconds": 20,
            },
        )
        self.rows.append(
            (
                "native execution and filesystem controls",
                native_passed(
                    result, self.observed, active, self.root / "agent", outside, self.mode, nonce
                ),
                result + "\nVerified executor descendant PIDs: " + repr(sorted(self.observed)),
            )
        )
        result = self.turn("read", {"path": str(active / "marker.txt")})
        self.rows.append(("host file read positive control", nonce in result, result))
        if self.mode != "ro":
            result = self.turn("write", {"path": str(active / "host-write.txt"), "content": nonce})
            expected = nonce if self.mode == "rw" else "before"
            passed = (active / "host-write.txt").read_text() == expected
            if self.mode == "none":
                passed = (
                    passed
                    and '"status": "error"' in result
                    and "Sandbox path is read-only" in result
                )
            else:
                passed = passed and '"status": "error"' not in result
            self.rows.append(("host file write control", passed, result))
        require(identities(self.installation) == self.manifest, "candidate_changed")
        require(file_hash(config) == self.config_hash, "configuration_changed")

    def report(self, failure: str | None) -> bool:
        """Retain Markdown only; runtime JSON stays in the external private campaign."""
        passed = (
            not self.prerequisites_only
            and len(self.rows) == (3 if self.mode == "ro" else 4)
            and all(ok for _, ok, _ in self.rows)
            and failure is None
            and not self.cleanup
        )
        replacements = [
            (str(self.root), "<campaign>"),
            (str(self.installation), "<installation>"),
            (self.token, "<gateway-token>"),
            (self.provider.token, "<provider-token>"),
            (os.environ.get("USERPROFILE", ""), "<profile>"),
        ]

        def redact(text: str) -> str:
            for old, new in replacements:
                if not old:
                    continue
                text = text.replace(old.replace("\\", "\\\\"), new).replace(old, new)
            return text

        lines = [
            f"# {PROFILE}: {self.mode}",
            "",
            f"Selected-mode smoke: {'passed' if passed else 'unqualified'}.",
            "Prerequisites-only diagnostic; native smoke not run."
            if self.prerequisites_only
            else "Native smoke requested.",
            "",
            f"Host: {platform.system()} {platform.release()} {platform.version()}, {platform.machine()}; Python {platform.python_version()}.",
            "",
            f"Configuration SHA-256: `{self.config_hash}`. Native debug enabled: `{self.debug}`.",
            "",
            f"Failure: {failure or 'none'}. Cleanup errors: {', '.join(self.cleanup) or 'none'}.",
            "",
            "This is a partial, single-mode smoke. It does not qualify the full profile, replacement races, links, rename, output/deadline attacks, arbitrary descendants, multi-session lifecycle, or production use. The ro host write route is excluded because normal tool policy filters it. Gateway logs have a polled 8 MiB stop threshold, not a strict streaming byte cap.",
            "",
            "## Selected assets (pre/post equality required; not a complete dependency-tree attestation)",
            "",
        ]
        lines.extend(f"- `{name}`: `{value}`" for name, value in self.manifest.items())
        for name, ok, result in self.rows:
            lines.extend(
                [
                    "",
                    f"## {name}: {'passed' if ok else 'unqualified'}",
                    "",
                    "```text",
                    redact(result).replace("```", "'''"),
                    "```",
                ]
            )
        if self.setup:
            lines.extend(["", "## Setup diagnostics (not native qualification)"])
            lines.extend(redact(line) for line in self.setup)
        if self.debug:
            lines.extend(["", "## Native diagnostics (not independent execution proof)", ""])
            lines.extend(
                redact(line).replace("```", "'''") for line in native_diagnostics(self.root)
            )
        (self.root / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
        return passed


def main() -> int:
    """Create an external campaign and leave its bounded evidence for inspection."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--installation", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--mode", choices=MODES, required=True)
    parser.add_argument("--debug-native", action="store_true")
    parser.add_argument("--prerequisites-only", action="store_true")
    args = parser.parse_args()
    require(
        os.name == "nt" and platform.machine().lower() in {"amd64", "x86_64"},
        "windows_x64_required",
    )
    require(
        args.output.is_absolute() and args.installation.is_absolute(), "absolute_paths_required"
    )
    root, installation = args.output.resolve(), args.installation.resolve()
    require(
        not root.exists()
        and not root.is_relative_to(REPO)
        and not root.is_relative_to(installation)
        and not any((parent / ".git").exists() for parent in root.parents),
        "fresh_external_output_required",
    )
    root.mkdir()
    for name in ("state", "home", "temp", "agent", "private", "appdata", "local"):
        (root / name).mkdir()
    campaign = Campaign(root, installation, args.mode, debug=args.debug_native)
    failure = None
    try:
        campaign.execute(prerequisites_only=args.prerequisites_only)
    except Exception as exc:
        failure = str(exc) if isinstance(exc, Refused) else type(exc).__name__
    finally:
        campaign.cleanup.extend(campaign.processes.close())
        if not campaign.processes.stopped:
            campaign.cleanup.append("process_cleanup_unconfirmed")
        try:
            campaign.provider.close()
        except Exception as exc:
            campaign.cleanup.append(type(exc).__name__)
    passed = campaign.report(failure)
    if args.prerequisites_only:
        completed = campaign.prerequisites_complete and failure is None and not campaign.cleanup
        print(
            f"Prerequisite diagnostic {'completed' if completed else 'failed'}; native smoke not run."
        )
        return 0 if completed else 1
    print(f"Selected-mode smoke {'passed' if passed else 'unqualified'}; see {root / 'report.md'}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
