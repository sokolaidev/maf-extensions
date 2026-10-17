"""Bound command output while owning the producer and its descendants."""

from __future__ import annotations

import importlib.util
import io
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from .evidence import LIMITS, require


def capture(
    args: list[str],
    root: Path,
    env: dict[str, str],
    timeout: int,
    *,
    diagnostics: bool,
    stdout_limit: int | None = None,
) -> subprocess.CompletedProcess[bytes]:
    """Stop the owned tree on deadline or overflow; cleanup may discard diagnostics."""
    log = (root / "setup.log").open("ab") if diagnostics else None
    stdout = bytearray()
    failure: list[str] = []
    lock = threading.Lock()
    readers: list[threading.Thread] = []
    process: subprocess.Popen[bytes] | None = None
    job: Any = None

    def refuse(reason: str) -> None:
        with lock:
            if not failure:
                failure.append(reason)

    def drain(stream: io.BufferedReader, output: bool) -> None:
        size = 0 if output or log is None else log.tell()
        limit = (
            (LIMITS["events"] if stdout_limit is None else min(stdout_limit, LIMITS["events"]))
            if output
            else LIMITS["log"]
        )
        try:
            while chunk := stream.read1(65536):
                available = max(0, limit - size)
                if output:
                    stdout.extend(chunk[:available])
                elif log is not None:
                    log.write(chunk[:available])
                    log.flush()
                size += len(chunk)
                if size > limit:
                    refuse("setup_output_limit" if output else "setup_log_limit")
                    return
        except (OSError, ValueError):
            refuse("setup_output_read")

    try:
        require(log is None or log.tell() <= LIMITS["log"], "setup_log_limit")
        if os.name == "nt":
            from .install import SAMPLE

            spec = importlib.util.spec_from_file_location(
                "_qualification_job", SAMPLE / "_windows_job.py"
            )
            assert spec is not None and spec.loader is not None
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            job = module.Job()
            args = [
                sys.executable,
                "-I",
                "-B",
                "-c",
                "import subprocess,sys; sys.exit(subprocess.call(sys.argv[1:], stdin=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW) if sys.stdin.buffer.read(1)==b'1' else 1)",
                *args,
            ]
        process = subprocess.Popen(
            args,
            cwd=root,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.PIPE if job is not None else subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            start_new_session=os.name != "nt",
        )
        if job is not None:
            job.assign(process.pid)
            assert process.stdin is not None
            process.stdin.write(b"1")
            process.stdin.close()
        assert isinstance(process.stdout, io.BufferedReader)
        assert isinstance(process.stderr, io.BufferedReader)
        readers = [
            threading.Thread(target=drain, args=(stream, output), daemon=True)
            for stream, output in ((process.stdout, True), (process.stderr, False))
        ]
        for reader in readers:
            reader.start()
        end = time.monotonic() + timeout
        while process.poll() is None or any(reader.is_alive() for reader in readers):
            if failure:
                break
            if time.monotonic() >= end:
                refuse("setup_command_timeout")
                break
            time.sleep(0.01)
    finally:
        try:
            if job is not None:
                try:
                    if job.active():
                        job.terminate()
                finally:
                    job.close()
            elif process is not None and os.name != "nt":
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass  # The owned group may already have exited.
        finally:
            if process is not None:
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=30)
            for reader in readers:
                reader.join(timeout=5)
            if log is not None:
                log.close()
            if process is not None:
                for stream in (process.stdout, process.stderr):
                    if stream is not None and not any(reader.is_alive() for reader in readers):
                        stream.close()
    require(not any(reader.is_alive() for reader in readers), "setup_process_cleanup")
    require(not failure, failure[0] if failure else "setup_output_read")
    assert process is not None
    return subprocess.CompletedProcess(args, process.returncode, bytes(stdout))


def run(
    args: list[str], root: Path, env: dict[str, str], timeout: int, *, diagnostics: bool
) -> str:
    """Require success from a supervised command with bounded output."""
    result = capture(args, root, env, timeout, diagnostics=diagnostics)
    require(result.returncode == 0, "setup_command_failed")
    return result.stdout.decode("utf-8").strip()
