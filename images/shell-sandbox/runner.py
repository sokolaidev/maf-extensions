"""Bounded, offline shell analysis with source supplied only through anonymous pipes."""

from __future__ import annotations

import hashlib
import json
import math
import os
import selectors
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

INSTALL = Path("/opt/maf-shell")
OUTPUT_LIMIT = 1024 * 1024
INPUT_LIMIT = 16 * 1024 * 1024
IDENTITY = {
    "launcher": "maf-shell-v1",
    "bash": "5.2.15-2+b13",
    "dash": "0.5.12-2",
    "shellcheck": "0.11.0",
    "powershell": "7.6.7",
    "psscriptanalyzer": "1.25.0",
}


def clean_environment(private: Path) -> dict[str, str]:
    """Keep profiles, user modules, credentials and inherited shell flags out of analysis."""
    for name in ("home", "tmp", "cache", "config"):
        (private / name).mkdir()
    return {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": str(private / "home"),
        "TMPDIR": str(private / "tmp"),
        "XDG_CACHE_HOME": str(private / "cache"),
        "XDG_CONFIG_HOME": str(private / "config"),
        "PSModulePath": "/opt/maf-shell/dependencies/modules:/opt/powershell/Modules",
        "POWERSHELL_TELEMETRY_OPTOUT": "1",
        "DOTNET_CLI_TELEMETRY_OPTOUT": "1",
        "LANG": "C.UTF-8",
    }


class Supervisor:
    """Share a deadline and output budget, including descendants retaining an output pipe."""

    def __init__(self, timeout: float, environment: dict[str, str], cwd: Path) -> None:
        self.deadline = time.monotonic() + timeout
        self.remaining = OUTPUT_LIMIT
        self.environment = environment
        self.cwd = cwd

    def execute(self, command: list[str], data: bytes = b"") -> dict[str, Any]:
        """Feed bounded input without deadlocking against concurrent stdout or stderr."""
        if time.monotonic() >= self.deadline:
            raise TimeoutError("deadline")
        process = subprocess.Popen(
            command,
            cwd=self.cwd,
            env=self.environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        assert process.stdin is not None and process.stdout is not None
        assert process.stderr is not None
        output = {"stdout": bytearray(), "stderr": bytearray()}
        pending = memoryview(data)
        try:
            with selectors.DefaultSelector() as selector:
                for stream, name in ((process.stdout, "stdout"), (process.stderr, "stderr")):
                    os.set_blocking(stream.fileno(), False)
                    selector.register(stream, selectors.EVENT_READ, name)
                os.set_blocking(process.stdin.fileno(), False)
                selector.register(process.stdin, selectors.EVENT_WRITE, "stdin")
                while selector.get_map():
                    left = self.deadline - time.monotonic()
                    if left <= 0:
                        raise TimeoutError("deadline")
                    for key, _events in selector.select(min(left, 0.1)):
                        if key.data == "stdin":
                            if pending:
                                try:
                                    pending = pending[os.write(key.fd, pending[:8192]) :]
                                except BrokenPipeError:
                                    pending = memoryview(b"")
                            if not pending:
                                selector.unregister(key.fileobj)
                                process.stdin.close()
                            continue
                        chunk = os.read(key.fd, 16384)
                        if not chunk:
                            selector.unregister(key.fileobj)
                            continue
                        self.remaining -= len(chunk)
                        if self.remaining < 0:
                            raise ValueError("output limit")
                        output[key.data].extend(chunk)
                left = self.deadline - time.monotonic()
                if left <= 0:
                    raise TimeoutError("deadline")
                code = process.wait(timeout=left)
        finally:
            try:
                # Windows stdlib stubs omit the Linux image's process-group API.
                os.killpg(process.pid, signal.SIGKILL)  # pyright: ignore[reportAttributeAccessIssue]
            except ProcessLookupError:
                # The process group can exit before cleanup reaches it.
                pass
            process.stdin.close()
            process.stdout.close()
            process.stderr.close()
            process.wait(timeout=1)
        return {
            "exit_code": code,
            **{name: value.decode("utf-8", errors="strict") for name, value in output.items()},
        }


def dependency_inventory() -> str:
    """Verify the host's complete dependency tree before allowing command discovery."""
    raw = (INSTALL / "dependencies.json").read_bytes()
    inventory = json.loads(raw)
    root = INSTALL / "dependencies"
    actual: dict[str, str] = {}
    for path in root.rglob("*"):
        if path.is_symlink():
            raise ValueError("dependency symlink")
        if path.is_file():
            actual[path.relative_to(root).as_posix()] = hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
    if actual != inventory:
        raise ValueError("dependency inventory mismatch")
    return hashlib.sha256(raw).hexdigest()


def checked_json(phase: dict[str, Any]) -> Any:
    """Require a successful analyzer with no unstructured error stream."""
    if phase["exit_code"] != 0 or phase["stderr"]:
        raise ValueError("analyzer failed")
    return json.loads(phase["stdout"])


def analyze(request: dict[str, Any], supervisor: Supervisor) -> list[dict[str, Any]]:
    """Keep every model-authored source off the filesystem used by command discovery."""
    sources = request["files"]
    if request["flavor"] == "bash":
        for interpreter in ("bash", "dash"):
            phase = supervisor.execute(["/usr/bin/dpkg-query", "-W", "-f=${Version}", interpreter])
            if phase != {"exit_code": 0, "stdout": IDENTITY[interpreter], "stderr": ""}:
                raise ValueError("interpreter identity mismatch")
        syntax = []
        for source in sources:
            command = (
                ["/bin/bash", "--noprofile", "--norc", "-n"]
                if source["dialect"] == "bash"
                else ["/bin/dash", "-n"]
            )
            syntax.append(supervisor.execute(command, source["text"].encode("utf-8")))
        report = checked_json(
            supervisor.execute(
                ["/usr/local/bin/maf-shell-audit"], json.dumps({"files": sources}).encode("utf-8")
            )
        )
        if report["protocol"] != 1 or len(report["files"]) != len(sources):
            raise ValueError("unsupported audit report")
        for item, phase in zip(report["files"], syntax, strict=True):
            item["syntax"] = phase
        return report["files"]
    if request["flavor"] != "pwsh":
        raise ValueError("unsupported flavor")
    reports = []
    for source in sources:
        item = checked_json(
            supervisor.execute(
                [
                    "/opt/powershell/pwsh",
                    "-NoLogo",
                    "-NoProfile",
                    "-NonInteractive",
                    "-File",
                    str(INSTALL / "analyze.ps1"),
                ],
                json.dumps(source).encode("utf-8"),
            )
        )
        if any(item[key] != IDENTITY[key] for key in ("powershell", "psscriptanalyzer")):
            raise ValueError("analyzer identity mismatch")
        item["syntax"] = {"exit_code": 0 if item["parsed"] else 1, "stdout": "", "stderr": ""}
        reports.append(item)
    return reports


def main() -> None:
    """Emit one bounded protocol envelope, including fixed failure status on exhaustion."""
    envelope: dict[str, Any] = {
        "protocol": 1,
        "identity": IDENTITY,
        "flavor": "",
        "error": "incomplete",
        "files": [],
        "dependency_inventory_sha256": "",
    }
    try:
        bundle = Path("request.json")
        with bundle.open("rb") as handle:
            raw = handle.read(INPUT_LIMIT + 1)
        bundle.unlink()
        if len(raw) > INPUT_LIMIT:
            raise ValueError("input limit")
        request = json.loads(raw)
        envelope["flavor"] = request["flavor"]
        timeout = float(sys.argv[1])
        if not math.isfinite(timeout) or not 0 < timeout <= 600:
            raise ValueError("timeout")
        envelope["dependency_inventory_sha256"] = dependency_inventory()
        with tempfile.TemporaryDirectory(prefix="maf-shell-") as directory:
            private = Path(directory)
            supervisor = Supervisor(timeout, clean_environment(private), private)
            envelope["files"] = analyze(request, supervisor)
        envelope["error"] = None
    except Exception:
        envelope["error"] = "incomplete"
    encoded = json.dumps(envelope, ensure_ascii=True).encode("utf-8")
    if len(encoded) > OUTPUT_LIMIT:
        envelope["files"] = []
        envelope["error"] = "output_limit"
        encoded = json.dumps(envelope).encode("utf-8")
    sys.stdout.buffer.write(encoded + b"\n")


if __name__ == "__main__":
    main()
