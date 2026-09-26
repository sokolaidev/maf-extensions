"""Connect a native worker to the container's PID 1 supervisor."""

from __future__ import annotations

import json
import math
import os
import socket
import struct
import subprocess
import time
from pathlib import Path
from typing import cast

from ._pod_config import POD_SOCKET, HyperlightPodConfig
from ._wire import HyperlightWorkerError

FRAME_LIMIT = 8192
PLATFORM_EXIT = 78
PLATFORM_REFUSAL = "maf-hyperlight: unsupported platform: "
TERMINATION_LOG = "/dev/termination-log"


def frame(message: dict[str, object]) -> bytes:
    """Encode a bounded lifecycle message, never source or guest output."""
    encoded = json.dumps(message, allow_nan=False, separators=(",", ":")).encode() + b"\n"
    if len(encoded) > FRAME_LIMIT:
        raise HyperlightWorkerError("pod lifecycle message exceeds its limit")
    return encoded


def unframe(raw: bytes) -> dict[str, object]:
    """Refuse truncated, oversized or non-object lifecycle messages."""
    if len(raw) > FRAME_LIMIT or not raw.endswith(b"\n"):
        raise HyperlightWorkerError("invalid pod lifecycle frame")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise HyperlightWorkerError("pod lifecycle message must be an object")
    return cast("dict[str, object]", value)


def container_controls(root: Path = Path("/sys/fs/cgroup")) -> dict[str, str]:
    """Read the effective cgroup v2 controls the kernel applies to this container."""
    if not (root / "cgroup.controllers").is_file():
        raise HyperlightWorkerError("pod containment requires cgroup v2")
    controls: dict[str, str] = {}
    for name in ("memory.max", "memory.swap.max", "cpu.max", "pids.max"):
        try:
            controls[name] = (root / name).read_text().strip()
        except OSError as error:
            raise HyperlightWorkerError(f"pod containment cannot read cgroup {name}") from error
    return controls


def verify_container(memory_limit: int, *, root: Path = Path("/sys/fs/cgroup")) -> dict[str, str]:
    """Require finite container controls without requesting writable cgroup delegation."""
    controls = container_controls(root)
    if controls["memory.max"] != str(memory_limit):
        raise HyperlightWorkerError("container memory.max differs from its declared pod budget")
    if controls["memory.swap.max"] != "0":
        raise HyperlightWorkerError("pod containment requires swap disabled")
    cpu = controls["cpu.max"].split()
    if len(cpu) != 2 or any(not item.isdigit() or int(item) <= 0 for item in cpu):
        raise HyperlightWorkerError("pod containment requires a finite CPU limit")
    if not controls["pids.max"].isdigit() or int(controls["pids.max"]) <= 0:
        raise HyperlightWorkerError("pod containment requires a finite PID limit")
    return controls


class PodJob:
    """One worker registered with the namespace-init supervisor before native initialization."""

    def __init__(self, binding: HyperlightPodConfig, timeout: float) -> None:
        self.binding = binding
        self.timeout = timeout
        self.owner = os.getpid()
        self.process: subprocess.Popen[bytes] | None = None
        self.closed = False
        self.request("validate")

    def request(self, operation: str, **fields: object) -> dict[str, object]:
        """Authenticate the local supervisor and bind every request to this pod generation."""
        if os.getpid() != self.owner:
            raise HyperlightWorkerError("a forked process cannot use another pod owner")
        message = {
            "op": operation,
            "pod_uid": self.binding.pod_uid,
            "generation": self.binding.generation,
            **fields,
        }
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(self.timeout)
            connection.connect(POD_SOCKET)
            peer = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
            pid, uid, _ = struct.unpack("3i", peer)
            if pid != 1 or uid != os.getuid():
                raise HyperlightWorkerError("pod control socket is not owned by namespace PID 1")
            connection.sendall(frame(message))
            with connection.makefile("rb") as stream:
                reply = unframe(stream.readline(FRAME_LIMIT + 1))
        if reply.get("ok") is not True:
            raise HyperlightWorkerError(str(reply.get("error", "pod supervisor refused request")))
        return reply

    def spawn(
        self, command: list[str], *, environment: dict[str, str], cwd: str, cleanup_timeout: float
    ) -> subprocess.Popen[bytes]:
        """The worker cannot execute guest code until registration and the first begin succeed."""
        self.process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            env=environment,
            cwd=cwd,
        )
        try:
            self.request("register", pid=self.process.pid)
        except BaseException:
            self.process.kill()
            self.process.wait(timeout=cleanup_timeout)
            raise
        return self.process

    def ready(self, *, deadline: float) -> None:
        """Check the binding on every exchange, including use after controller disconnect."""
        self.request("validate")

    def begin(self, deadline: float) -> None:
        """Register the native operation with both supervisors before writing to the worker."""
        remaining = deadline - time.monotonic()
        if not math.isfinite(remaining) or remaining <= 0:
            raise TimeoutError("pod operation expired before submission")
        self.request("begin", deadline=deadline, expires_at=time.time() + remaining)

    def end(self) -> None:
        """Clear the registered operation after the worker has replied."""
        self.request("end")

    def abort(self) -> None:
        """Retire the entire container; its owner cannot continue serving this session."""
        self.request("retire")

    def close(self, *, deadline: float | None = None) -> None:
        """Confirm normal worker disposal without changing the pod's fixed ownership binding."""
        if self.closed:
            return
        if self.process is not None:
            self.request("release", pid=self.process.pid)
            remaining = self.timeout if deadline is None else max(0, deadline - time.monotonic())
            self.process.wait(timeout=remaining)
        self.closed = True
