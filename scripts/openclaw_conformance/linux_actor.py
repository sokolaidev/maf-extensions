"""Identify and kill one Linux actor without signalling a reused PID."""

from __future__ import annotations

import os
import select
import signal
import sys
from pathlib import Path
from typing import Any

from .evidence import Refused, require


def stop_processes(processes: list[Any]) -> list[str]:
    """Finish bounded process-group teardown before reporting deferred interrupts."""
    if sys.platform != "linux":
        raise Refused("linux_required")
    signal.alarm(0)
    errors = []

    def interrupted(_signum, _frame):
        errors.append("KeyboardInterrupt")

    previous = signal.signal(signal.SIGINT, interrupted)
    try:
        for proc in reversed(processes):
            try:
                errors.extend(proc.close())
            except Exception as exc:
                errors.append(type(exc).__name__)
            if proc.stopped:
                continue
            stopped = True
            for child, _ in reversed(proc.children):
                try:
                    try:
                        os.killpg(child.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    require(proc.wait_empty(child, 10), "owned_processes_remain")
                    child.wait(timeout=10)
                except Exception as exc:
                    stopped = False
                    errors.append(type(exc).__name__)
            proc.stopped = stopped
    finally:
        signal.signal(signal.SIGINT, previous)
    return errors


def identity(pid: int) -> dict[str, int]:
    """Read process ownership and birth identity from one procfs snapshot."""
    raw = Path(f"/proc/{pid}/stat").read_text()
    fields = raw[raw.rindex(")") + 2 :].split()
    return {"pid": pid, "pgrp": int(fields[2]), "session": int(fields[3]), "start": int(fields[19])}


class LinuxActor:
    """Hold a pidfd only after binding the actor to the live supervised session."""

    def __init__(self, processes: Any, pid: int):
        if sys.platform != "linux":
            raise Refused("linux_required")
        require(type(pid) is int and pid > 0, "linux_actor")
        child = processes.children[-1][0]
        require(child.poll() is None, "actor_supervisor_exited")
        before = identity(pid)
        self.fd: int | None = os.pidfd_open(pid)
        try:
            after = identity(pid)
            require(
                before == after
                and after["pgrp"] == after["session"] == child.pid
                and child.poll() is None
                and self.alive(),
                "actor_not_owned",
            )
            self.evidence = {
                **after,
                "supervisor_pid": child.pid,
                "pidfd_bound": True,
                "signal": "SIGKILL",
            }
        except BaseException:
            self.close()
            raise

    def alive(self) -> bool:
        require(self.fd is not None, "actor_handle_closed")
        assert self.fd is not None
        return not select.select([self.fd], [], [], 0)[0]

    def kill(self) -> None:
        if sys.platform != "linux":
            raise Refused("linux_required")
        require(self.alive(), "actor_exited_before_fault")
        assert self.fd is not None
        signal.pidfd_send_signal(self.fd, signal.SIGKILL)
        require(bool(select.select([self.fd], [], [], 10)[0]), "actor_did_not_exit")

    def close(self) -> None:
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
