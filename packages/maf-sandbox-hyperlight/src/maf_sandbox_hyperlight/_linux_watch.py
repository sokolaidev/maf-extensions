"""Own the complete Linux worker lifecycle outside its memory cgroup."""

from __future__ import annotations

import os
import select
import subprocess
import sys
import time
from contextlib import suppress

from ._linux import kill_group, read_control, write_control
from ._wire import HyperlightWorkerError


def _cleanup(directory: int, parent: int, name: str, timeout: float) -> None:
    """Retain ownership until the kernel confirms the entire group has disappeared."""
    while True:
        try:
            if directory < 0:
                with suppress(FileNotFoundError):
                    os.rmdir(name, dir_fd=parent)
            else:
                kill_group(directory, parent, name, time.monotonic() + timeout)
        except (OSError, HyperlightWorkerError):
            time.sleep(0.1)
        else:
            return


def main() -> None:
    """Create the contained worker and clean it on host exit, worker exit or close."""
    owner, parent = map(int, sys.argv[1:3])
    name, memory, timeout = sys.argv[3], int(sys.argv[4]), float(sys.argv[5])
    control, readiness = map(int, sys.argv[6:8])
    directory = -1
    created = False
    worker: subprocess.Popen[bytes] | None = None
    poller = select.poll()
    poller.register(owner, select.POLLIN)
    poller.register(control, select.POLLIN)
    try:
        if poller.poll(0):
            return
        os.mkdir(name, mode=0o700, dir_fd=parent)
        created = True
        directory = os.open(
            name, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW, dir_fd=parent
        )
        for key, value in (
            ("memory.max", str(memory)),
            ("memory.swap.max", "0"),
            ("memory.oom.group", "1"),
        ):
            write_control(directory, key, value)
            if read_control(directory, key).strip() != value:
                raise HyperlightWorkerError("Linux worker memory enforcement was not confirmed")
        kill = os.open("cgroup.kill", os.O_WRONLY | os.O_CLOEXEC, dir_fd=directory)
        os.close(kill)
        if poller.poll(0):
            return
        worker = subprocess.Popen(
            [
                sys.executable,
                "-I",
                "-u",
                "-m",
                "maf_sandbox_hyperlight._linux_entry",
                str(directory),
                *sys.argv[8:],
            ],
            pass_fds=(directory,),
        )
        handle = os.pidfd_open(worker.pid)
        try:
            poller.register(handle, select.POLLIN)
            os.write(readiness, str(worker.pid).encode("ascii"))
            poller.poll()
        finally:
            os.close(handle)
    except BaseException as error:
        print(f"Linux worker supervision failed: {error}", file=sys.stderr, flush=True)
    finally:
        # The bootstrap may still be outside the group, but cannot fork before joining it.
        if worker is not None:
            with suppress(ProcessLookupError):
                worker.kill()
        if created:
            _cleanup(directory, parent, name, timeout)
            if directory >= 0:
                os.close(directory)
        if worker is not None:
            worker.wait()


if __name__ == "__main__":
    main()
