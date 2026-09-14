"""Loop-independent process I/O; native execution never occupies an asyncio thread."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import threading
import time
from typing import BinaryIO, cast

from ._config import HyperlightSandboxConfig
from ._lifetime import create_job
from ._wire import HyperlightOutputLimitExceeded, HyperlightWorkerError, decode, encode

_STDERR_LIMIT = 64 * 1024


class Worker:
    """One process and its lifetime job; callers serialize requests and may interrupt with close."""

    def __init__(self, config: HyperlightSandboxConfig) -> None:
        self._owner_pid = os.getpid()
        self._config = config
        self._closing = threading.Lock()
        self._stderr = bytearray()
        self._stderr_guard = threading.Lock()
        self._closed = False
        self._job = create_job(config)
        allowed_environment = (
            {"PATH", "HOME", "XDG_CACHE_HOME", "TMPDIR", "LANG", "LC_ALL"}
            if sys.platform == "linux"
            else {"SYSTEMROOT", "WINDIR", "PATH", "TEMP", "TMP", "COMSPEC", "LOCALAPPDATA"}
        )
        environment = {
            key: value
            for key, value in os.environ.items()
            if (key.upper() if sys.platform == "win32" else key) in allowed_environment
        }
        environment["HYPERLIGHT_MAX_SURROGATES"] = "0"
        cleanup_deadline: float | None = None
        try:
            self.process = subprocess.Popen(
                self.command(),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=tempfile.gettempdir(),
                env=environment,
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
            )
            try:
                # The worker waits for init before loading Hyperlight or creating a VM.
                self._job.assign(self.process.pid)
            except BaseException:
                cleanup_deadline = time.monotonic() + config.cleanup_timeout
                try:
                    self.process.kill()
                    self.process.wait(timeout=max(0, cleanup_deadline - time.monotonic()))
                finally:
                    for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
                        if stream is not None:
                            stream.close()
                raise
        except BaseException:
            self._job.close(deadline=cleanup_deadline)
            raise
        assert self.process.stdin is not None
        assert self.process.stdout is not None
        assert self.process.stderr is not None
        self._input = cast("BinaryIO", self.process.stdin)
        self._output = cast("BinaryIO", self.process.stdout)
        self._errors = cast("BinaryIO", self.process.stderr)
        self._drainer = threading.Thread(target=self._drain, daemon=True)
        self._drainer.start()

    @staticmethod
    def command() -> list[str]:
        return [sys.executable, "-I", "-u", "-m", "maf_sandbox_hyperlight._worker"]

    @property
    def alive(self) -> bool:
        return os.getpid() == self._owner_pid and not self._closed and self.process.poll() is None

    def _drain(self) -> None:
        while chunk := self._errors.read(8192):
            with self._stderr_guard:
                self._stderr.extend(chunk[: max(0, _STDERR_LIMIT - len(self._stderr))])

    def request(self, message: dict[str, object], *, deadline: float) -> dict[str, object]:
        """Make one bounded exchange; close from another thread interrupts a native hang."""
        if os.getpid() != self._owner_pid:
            raise HyperlightWorkerError("a forked process cannot use another owner's worker")
        self._job.ready(deadline=deadline)
        try:
            self._input.write(encode(message))
            self._input.flush()
            response = decode(self._output.readline(6 * self._config.max_output_bytes + 32768))
        except (OSError, ValueError, HyperlightWorkerError) as error:
            with self._stderr_guard:
                detail = self._stderr.decode("utf-8", errors="replace")
            raise HyperlightWorkerError(
                f"worker communication failed: {error}; {detail}"
            ) from error
        if response.get("error") == "output_limit":
            raise HyperlightOutputLimitExceeded("guest stdout/stderr exceeded max_output_bytes")
        if "error" in response:
            raise HyperlightWorkerError(f"native worker failed: {response.get('detail', '')}")
        return response

    def close(self) -> None:
        """Terminate, reap and close all pipes within the configured cleanup allowance."""
        if os.getpid() != self._owner_pid:
            raise HyperlightWorkerError("a forked process cannot dispose another owner's worker")
        deadline = time.monotonic() + self._config.cleanup_timeout
        if not self._closing.acquire(timeout=self._config.cleanup_timeout):
            raise HyperlightWorkerError("worker cleanup is already in progress")
        try:
            if self._closed:
                return
            self._job.close(deadline=deadline)
            if self.process.poll() is None:
                self.process.kill()
            self.process.wait(timeout=max(0, deadline - time.monotonic()))
            self._drainer.join(timeout=max(0, deadline - time.monotonic()))
            if self._drainer.is_alive():
                raise HyperlightWorkerError("worker diagnostic pipe did not close")
            for stream in (self._input, self._output, self._errors):
                stream.close()
            self._closed = True
        finally:
            self._closing.release()
