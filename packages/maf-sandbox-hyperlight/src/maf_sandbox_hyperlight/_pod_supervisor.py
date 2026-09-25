"""Namespace PID 1: owner lifetime, one native worker and an external controller lease."""

from __future__ import annotations

import json
import math
import os
import queue
import select
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
from contextlib import suppress
from pathlib import Path
from types import FrameType

from ._pod import FRAME_LIMIT, frame, unframe, verify_container
from ._pod_config import POD_BINDING, POD_SOCKET, HyperlightPodConfig, PodLaunch
from ._wire import HyperlightWorkerError

LEASE_SECONDS = 5.0
STARTUP_SECONDS = 60.0


def _status(pid: int) -> dict[str, str]:
    return dict(line.split(":", 1) for line in Path(f"/proc/{pid}/status").read_text().splitlines())


def _oom_kills() -> int:
    values = dict(
        line.split() for line in Path("/sys/fs/cgroup/memory.events.local").read_text().splitlines()
    )
    return int(values["oom_kill"])


def verify_init(launch: PodLaunch) -> None:
    """Only an unprivileged private namespace init may use process exit as containment."""
    status = _status(os.getpid())
    if os.getpid() != 1 or os.getuid() == 0:
        raise HyperlightWorkerError("pod supervisor must be non-root PID 1")
    if (
        int(status["CapEff"], 16) != 0
        or int(status["NoNewPrivs"]) != 1
        or int(status["Seccomp"]) != 2
    ):
        raise HyperlightWorkerError("pod supervisor requires no capabilities and RuntimeDefault")
    verify_container(launch.memory_limit_bytes)


class Supervisor:
    """The controller acknowledges every deadline before the owner can submit native work."""

    def __init__(self, launch: PodLaunch, command: list[str]) -> None:
        self.launch = launch
        self.command = command
        self.owner: subprocess.Popen[bytes] | None = None
        self.worker: int | None = None
        self.worker_fd: int | None = None
        self.closing = False
        self.policy: str | None = None
        self.sequence = 0
        self.deadline: float | None = None
        self.ack = threading.Event()
        self.retired = threading.Event()
        self.reason = ""
        self.guard = threading.Lock()
        self.incoming: queue.Queue[dict[str, object]] = queue.Queue(maxsize=32)
        self.outgoing: queue.Queue[dict[str, object]] = queue.Queue(maxsize=32)
        self.lease = time.monotonic() + STARTUP_SECONDS
        self.connected = False
        self.oom_kills = _oom_kills()

    def retire(self, reason: str) -> None:
        """Revoke admission before the namespace init exits."""
        self.reason = reason
        self.retired.set()
        self.ack.set()

    def emit(self, event: str, **fields: object) -> None:
        """Bound control traffic independently of the application's diagnostics."""
        try:
            self.outgoing.put_nowait(
                {
                    "event": event,
                    "pod_uid": self.launch.pod_uid,
                    "generation": self.launch.generation,
                    **fields,
                }
            )
        except queue.Full:
            self.retire("controller is not consuming lifecycle events")

    def read_controller(self) -> None:
        """A broken or malformed authenticated attach stream retires the owner."""
        try:
            while raw := sys.stdin.buffer.readline(FRAME_LIMIT + 1):
                self.incoming.put_nowait(unframe(raw))
        except (OSError, ValueError, queue.Full, HyperlightWorkerError) as error:
            self.retire(f"controller stream failed: {type(error).__name__}")
        else:
            self.retire("controller stream closed")

    def start_owner(self, binding: HyperlightPodConfig) -> None:
        """Publish the owner PID before permitting application imports and backend construction."""
        directory = Path(POD_BINDING).parent
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(directory, 0o700)
        for location in ("/work/home", "/work/cache", "/work/tmp"):
            Path(location).mkdir(mode=0o700, parents=True, exist_ok=True)
        ready, publish = os.pipe2(os.O_CLOEXEC)
        try:
            environment = dict(os.environ)
            environment.pop("MAF_HYPERLIGHT_POD_BINDING", None)
            self.owner = subprocess.Popen(
                [sys.executable, "-I", "-u", "-m", "maf_sandbox_hyperlight._pod_owner", str(ready)]
                + self.command,
                stdin=subprocess.DEVNULL,
                stdout=sys.stderr,
                stderr=sys.stderr,
                env=environment,
                pass_fds=(ready,),
            )
            record = {**binding.mapping(), "owner_pid": self.owner.pid}
            with open(POD_BINDING, "x", encoding="utf-8") as target:
                json.dump(record, target)
            os.chmod(POD_BINDING, 0o400)
            self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.listener.bind(POD_SOCKET)
            os.chmod(POD_SOCKET, 0o600)
            self.listener.listen(1)
            threading.Thread(target=self.serve_owner, daemon=True).start()
            self.emit("ready", owner_pid=self.owner.pid)
            os.write(publish, b"1")
        finally:
            os.close(ready)
            os.close(publish)

    def serve_owner(self) -> None:
        """Only the pinned application process can control its native worker."""
        while not self.retired.is_set():
            connection, _ = self.listener.accept()
            with connection:
                connection.settimeout(3)
                try:
                    peer = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
                    pid, uid, _ = struct.unpack("3i", peer)
                    if self.owner is None or pid != self.owner.pid or uid != os.getuid():
                        raise HyperlightWorkerError("caller is not the pod's owning process")
                    with connection.makefile("rb") as stream:
                        message = unframe(stream.readline(FRAME_LIMIT + 1))
                    self.check_identity(message)
                    self.handle(message)
                    connection.sendall(frame({"ok": True}))
                except (OSError, ValueError, HyperlightWorkerError) as error:
                    with suppress(OSError):
                        connection.sendall(frame({"error": str(error)[:1024]}))

    def check_identity(self, message: dict[str, object]) -> None:
        """Refuse messages replayed from another pod or controller generation."""
        if (
            message.get("pod_uid") != self.launch.pod_uid
            or message.get("generation") != self.launch.generation
        ):
            raise HyperlightWorkerError("pod ownership generation differs")

    def handle(self, message: dict[str, object]) -> None:
        """Keep worker allocation and deadlines monotonic within the owner lifetime."""
        if self.retired.is_set() or not self.connected or time.monotonic() >= self.lease:
            raise HyperlightWorkerError("pod session is retired or its controller lease expired")
        operation = message.get("op")
        if operation == "validate":
            verify_container(self.launch.memory_limit_bytes)
        elif operation == "policy":
            digest = message.get("digest")
            if not isinstance(digest, str) or len(digest) != 64:
                raise HyperlightWorkerError("invalid execution policy digest")
            if self.policy is not None and self.policy != digest:
                raise HyperlightWorkerError("a pod's execution policy cannot change")
            self.policy = digest
        elif operation == "register":
            self.register_worker(message.get("pid"))
        elif operation == "begin":
            deadline, expires_at = message.get("deadline"), message.get("expires_at")
            if (
                not isinstance(deadline, (int, float))
                or isinstance(deadline, bool)
                or not isinstance(expires_at, (int, float))
                or isinstance(expires_at, bool)
                or not math.isfinite(deadline)
                or not math.isfinite(expires_at)
                or float(deadline) <= time.monotonic()
                or self.worker is None
                or self.deadline is not None
            ):
                raise HyperlightWorkerError("invalid or overlapping native operation")
            self.ack.clear()
            self.sequence += 1
            self.deadline = float(deadline)
            self.emit("begin", sequence=self.sequence, expires_at=expires_at)
            if not self.ack.wait(min(3, max(0, self.deadline - time.monotonic()))):
                self.retire("controller did not acknowledge the deadline")
            if self.retired.is_set():
                raise HyperlightWorkerError("pod retired while registering its deadline")
        elif operation == "end":
            if self.deadline is None:
                raise HyperlightWorkerError("no native operation is active")
            self.emit("end", sequence=self.sequence)
            self.deadline = None
        elif operation == "release":
            if message.get("pid") != self.worker or self.deadline is not None:
                raise HyperlightWorkerError("cannot release an active or different worker")
            self.release_worker()
        elif operation == "retire":
            self.retire("application retired the session")
        else:
            raise HyperlightWorkerError("unknown pod lifecycle operation")

    def register_worker(self, value: object) -> None:
        """Pin the sole direct worker child with a pidfd before native execution."""
        if self.worker is not None or self.policy is None or type(value) is not int or value <= 1:
            raise HyperlightWorkerError("the pod permits one resident native worker")
        descriptor = os.pidfd_open(value)
        try:
            status = _status(value)
            if self.owner is None or int(status["PPid"]) != self.owner.pid:
                raise HyperlightWorkerError("worker is not a child of the owning application")
            if int(status["Uid"].split()[0]) != os.getuid():
                raise HyperlightWorkerError("worker identity differs from its owner")
            self.worker, self.worker_fd = value, descriptor
        except BaseException:
            os.close(descriptor)
            raise

    def release_worker(self) -> None:
        """Reap worker descendants within this dedicated PID namespace before releasing capacity."""
        assert self.owner is not None
        self.closing = True
        deadline = time.monotonic() + 2
        try:
            while True:
                live: list[int] = []
                for entry in Path("/proc").iterdir():
                    if not entry.name.isdigit() or int(entry.name) in (1, self.owner.pid):
                        continue
                    pid = int(entry.name)
                    try:
                        descriptor = os.pidfd_open(pid)
                    except ProcessLookupError:
                        continue
                    try:
                        if not select.select([descriptor], [], [], 0)[0]:
                            with suppress(ProcessLookupError):
                                signal.pidfd_send_signal(descriptor, signal.SIGKILL)
                            live.append(pid)
                    finally:
                        os.close(descriptor)
                if not live:
                    break
                if time.monotonic() >= deadline:
                    raise HyperlightWorkerError("pod worker cleanup could not confirm termination")
                time.sleep(0.01)
            with self.guard:
                if self.worker_fd is not None:
                    os.close(self.worker_fd)
                self.worker = self.worker_fd = None
        except BaseException:
            self.retire("worker cleanup failed")
            raise
        finally:
            self.closing = False

    def controller_message(self, message: dict[str, object]) -> None:
        """An attach credential permits lifecycle control, never guest source submission."""
        self.check_identity(message)
        if self.retired.is_set() or time.monotonic() >= self.lease:
            self.retire("controller lease expired")
            raise HyperlightWorkerError("an expired controller lease cannot be renewed")
        operation = message.get("op")
        if operation == "hello" and not self.connected:
            binding = self.launch.bind(message)
            self.connected = True
            self.lease = time.monotonic() + LEASE_SECONDS
            self.start_owner(binding)
        elif operation == "ping" and self.connected:
            self.lease = time.monotonic() + LEASE_SECONDS
        elif operation == "ack" and message.get("sequence") == self.sequence:
            self.ack.set()
        elif operation == "stop":
            self.retire("controller retired the session")
        else:
            raise HyperlightWorkerError("invalid controller lifecycle transition")

    def run(self) -> int:
        """Exit PID 1 on any uncertain lifetime; Linux then kills the entire namespace."""
        threading.Thread(target=self.read_controller, daemon=True).start()
        os.set_blocking(sys.stdout.fileno(), False)
        while not self.retired.is_set():
            try:
                while not self.incoming.empty():
                    self.controller_message(self.incoming.get_nowait())
                while not self.outgoing.empty():
                    payload = frame(self.outgoing.get_nowait())
                    if os.write(sys.stdout.fileno(), payload) != len(payload):
                        raise HyperlightWorkerError("controller lifecycle write was incomplete")
                now = time.monotonic()
                deadline = self.deadline
                if now >= self.lease or (deadline is not None and now >= deadline):
                    self.retire("controller lease or native deadline expired")
                if self.retired.is_set():
                    break
                if self.owner is not None and self.owner.poll() is not None:
                    return self.owner.returncode or 0
                if self.owner is not None:
                    while True:
                        try:
                            child, state = os.waitpid(-1, os.WNOHANG)
                        except ChildProcessError:
                            break
                        if child == self.owner.pid:
                            return os.waitstatus_to_exitcode(state)
                        if child == 0:
                            break
                with self.guard:
                    if (
                        not self.closing
                        and self.worker_fd is not None
                        and select.select([self.worker_fd], [], [], 0)[0]
                    ):
                        self.retire("native worker exited unexpectedly")
                if _oom_kills() != self.oom_kills:
                    self.retire("container OOM killed a process")
            except (OSError, ValueError, HyperlightWorkerError) as error:
                self.retire(str(error))
            time.sleep(0.01)
        return 70


def main() -> None:
    """Run an application as the sole owner under the controller's immutable pod binding."""
    try:
        fields = json.loads(os.environ["MAF_HYPERLIGHT_POD_BINDING"])
        launch = PodLaunch(
            fields.get("owner"),
            os.environ["MAF_HYPERLIGHT_POD_UID"],
            fields.get("generation"),
            fields.get("memory_limit_bytes"),
        )
        verify_init(launch)
        command = sys.argv[1:]
        if not command:
            raise ValueError("an application command is required")
        supervisor = Supervisor(launch, command)

        def terminate(signum: int, current: FrameType | None) -> None:
            supervisor.retire("pod termination requested")

        signal.signal(signal.SIGTERM, terminate)
        signal.signal(signal.SIGINT, terminate)
        status = supervisor.run()
        if supervisor.reason:
            print(supervisor.reason, file=sys.stderr, flush=True)
    except BaseException as error:
        print(f"pod supervisor refused startup: {error}", file=sys.stderr, flush=True)
        status = 71
    # Python shutdown can wait on application-owned resources; namespace exit must not.
    os._exit(status if 0 <= status <= 255 else 70)


if __name__ == "__main__":
    main()
