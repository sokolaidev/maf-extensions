"""Fixed offline validation launcher; copied into an immutable Linux sandbox image."""

from __future__ import annotations

import hashlib
import json
import math
import os
import selectors
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

OUTPUT_LIMIT = 128 * 1024
INSTALL = Path("/opt/maf-terraform")


def clean_environment(private: Path) -> dict[str, str]:
    """Create a complete environment, inheriting no CLI flags, variables, or credentials."""
    return {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": str(private / "home"),
        "TMPDIR": str(private / "tmp"),
        "TF_DATA_DIR": str(private / "data"),
        "TF_CLI_CONFIG_FILE": str(INSTALL / "terraform.rc"),
        "TF_INPUT": "0",
        "TF_IN_AUTOMATION": "1",
        "CHECKPOINT_DISABLE": "1",
        "LANG": "C.UTF-8",
    }


class Supervisor:
    """Share one output allowance and deadline across all CLI processes."""

    def __init__(self, timeout: float, environment: dict[str, str]) -> None:
        self.deadline = time.monotonic() + timeout
        self.remaining = OUTPUT_LIMIT
        self.environment = environment

    def execute_phase(self, command: list[str], cwd: Path) -> dict[str, Any]:
        """Bound both streams, kill the process group on every exit, and reap the child."""
        if time.monotonic() >= self.deadline:
            raise TimeoutError("deadline")
        process = subprocess.Popen(
            command,
            cwd=cwd,
            env=self.environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        output = {"stdout": bytearray(), "stderr": bytearray()}
        assert process.stdout is not None and process.stderr is not None
        try:
            with selectors.DefaultSelector() as selector:
                for stream, name in ((process.stdout, "stdout"), (process.stderr, "stderr")):
                    os.set_blocking(stream.fileno(), False)
                    selector.register(stream, selectors.EVENT_READ, name)
                while selector.get_map():
                    left = self.deadline - time.monotonic()
                    if left <= 0:
                        raise TimeoutError("deadline")
                    for key, _events in selector.select(min(left, 0.1)):
                        chunk = os.read(key.fd, 16384)
                        if not chunk:
                            selector.unregister(key.fileobj)
                            continue
                        self.remaining -= len(chunk)
                        if self.remaining < 0:
                            raise RuntimeError("output limit")
                        output[key.data].extend(chunk)
                left = self.deadline - time.monotonic()
                if left <= 0:
                    raise TimeoutError("deadline")
                code = process.wait(timeout=left)
        finally:
            # Includes descendants that retained a pipe after the parent exited. A process
            # escaping this group still belongs to the disposable sandbox, not a warm session.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                # An already-exited process group needs no further signal.
                pass
            process.stdout.close()
            process.stderr.close()
            process.wait(timeout=1)
        return {
            "exit_code": code,
            **{name: data.decode("utf-8", errors="strict") for name, data in output.items()},
        }


def execute(engine: str, root_module: str, timeout: float) -> dict[str, Any]:
    """Initialize, validate, and check formatting using only fixed command arguments."""
    result: dict[str, Any] = {
        "protocol": 1,
        "engine": engine,
        "version": "0.0.0",
        "phases": {},
        "error": None,
    }
    try:
        if (
            engine not in ("terraform", "opentofu")
            or not math.isfinite(timeout)
            or not 0 < timeout <= 600
        ):
            raise ValueError("unsupported request")
        metadata = json.loads((INSTALL / "engine.json").read_text())
        if metadata["engine"] != engine:
            raise ValueError("wrong image engine")
        result["version"] = metadata["version"]
        binary = Path("/usr/local/bin") / ("terraform" if engine == "terraform" else "tofu")
        with binary.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != metadata["binary_sha256"]:
                raise ValueError("engine digest mismatch")
        call = Path.cwd()
        project = call / "project"
        root = (project / root_module).resolve(strict=True)
        if not root.is_relative_to(project.resolve(strict=True)) or not root.is_dir():
            raise ValueError("root outside project")
        private = call / ".runner"
        for name in ("home", "tmp", "data"):
            (private / name).mkdir(parents=True, exist_ok=False)
        supervisor = Supervisor(timeout, clean_environment(private))
        version = supervisor.execute_phase([str(binary), "version", "-json"], private)
        if (
            version["exit_code"] != 0
            or json.loads(version["stdout"])["terraform_version"] != metadata["version"]
        ):
            raise ValueError("engine version mismatch")
        lock = root / ".terraform.lock.hcl"
        original_lock = lock.read_bytes() if lock.is_file() else None
        init = [str(binary), "init", "-backend=false", "-input=false", "-no-color"]
        if original_lock is not None:
            init.append("-lockfile=readonly")
        phases = result["phases"]
        phases["init"] = supervisor.execute_phase(init, root)
        if original_lock is not None and lock.read_bytes() != original_lock:
            raise ValueError("supplied lock changed")
        if phases["init"]["exit_code"] == 0:
            phases["validate"] = supervisor.execute_phase([str(binary), "validate", "-json"], root)
            phases["fmt"] = supervisor.execute_phase(
                [str(binary), "fmt", "-check", "-recursive", "-no-color"], project
            )
    except Exception:
        # The host renders this as incomplete regardless of any completed phase's verdict.
        result["error"] = "launcher could not complete the bounded execution"
    return result


def main() -> None:
    """Emit exactly one bounded protocol object; never consume model-authored flags."""
    if len(sys.argv) != 4:
        raise SystemExit(2)
    print(json.dumps(execute(sys.argv[1], sys.argv[2], float(sys.argv[3])), ensure_ascii=True))


if __name__ == "__main__":
    main()
