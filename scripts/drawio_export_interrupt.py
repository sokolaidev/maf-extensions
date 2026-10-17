"""Hold a native PNG write open before stopping its Electron process for fault injection."""

from __future__ import annotations

import json
import os
import signal
import sys
import time
from pathlib import Path

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def stopped(entry: Path) -> bool:
    """Confirm the signal took effect before changing an output path or reporting success."""
    return any(line.startswith("State:\tT") for line in (entry / "status").read_text().splitlines())


def wait_stopped(entry: Path, deadline: float) -> None:
    """Wait for the kernel to acknowledge a stopped process."""
    while time.monotonic() < deadline:
        if stopped(entry):
            return
        time.sleep(0.01)
    raise RuntimeError("Native process did not stop")


def interrupt(entry: Path, destination: Path, deadline: float) -> dict[str, object]:
    """A small FIFO retains a pending native write after the PNG signature is observed."""
    if sys.platform != "linux":
        raise RuntimeError("Fault injection requires a Linux guest")
    import fcntl

    pid = int(entry.name)
    os.kill(pid, signal.SIGSTOP)
    descriptor = None
    try:
        wait_stopped(entry, deadline)
        os.mkfifo(destination, 0o600)
        owner = destination.parent.stat()
        os.chown(destination, owner.st_uid, owner.st_gid)
        descriptor = os.open(destination, os.O_RDONLY | os.O_NONBLOCK)
        fcntl.fcntl(descriptor, fcntl.F_SETPIPE_SZ, 4096)
        os.kill(pid, signal.SIGCONT)
        signature = bytearray()
        while time.monotonic() < deadline:
            try:
                signature.extend(os.read(descriptor, len(PNG_SIGNATURE) - len(signature)))
            except BlockingIOError:
                # No bytes are ready yet; retain the polling delay below.
                pass
            if len(signature) == len(PNG_SIGNATURE):
                if signature != PNG_SIGNATURE:
                    raise RuntimeError("Native output is not PNG")
                os.kill(pid, signal.SIGSTOP)
                wait_stopped(entry, deadline)
                return {"renderer_pid": pid, "stopped": True, "pngSignature": signature.hex()}
            time.sleep(0.01)
        raise RuntimeError("Native renderer did not produce PNG output")
    except BaseException:
        try:
            os.kill(pid, signal.SIGCONT)
        except ProcessLookupError:
            # An exited renderer no longer needs to be resumed.
            pass
        raise
    finally:
        if descriptor is not None:
            os.close(descriptor)


def observe(proc: Path = Path("/proc"), timeout: float = 40) -> dict[str, object]:
    """Require an export command and native output; startup alone is not readiness."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for entry in proc.iterdir():
            if not entry.name.isdigit():
                continue
            try:
                args = (entry / "cmdline").read_bytes().split(b"\0")
                if not args or args[0] != b"/opt/drawio/drawio":
                    continue
                if b"--export" not in args or any(arg.startswith(b"--type=") for arg in args):
                    continue
                if args[args.index(b"--format") + 1] != b"png":
                    continue
                destination = Path(os.fsdecode(args[args.index(b"--output") + 1]))
                if destination.exists():
                    continue
                return interrupt(entry, destination, deadline)
            except (FileNotFoundError, ProcessLookupError, PermissionError, ValueError, IndexError):
                continue
        time.sleep(0.02)
    raise RuntimeError("No native PNG export observed")


if __name__ == "__main__":
    print(json.dumps(observe()), flush=True)
