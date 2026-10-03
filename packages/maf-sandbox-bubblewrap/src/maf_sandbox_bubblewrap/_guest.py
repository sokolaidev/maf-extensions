"""Private PID-namespace broker; runs under the same filesystem authority as guest commands."""

from __future__ import annotations

import base64
import ctypes
import errno
import json
import os
import selectors
import signal
import stat
import subprocess
import sys
import time
from pathlib import PurePosixPath
from typing import Any

FILE_LIMIT = 8 * 1024 * 1024
FRAME_LIMIT = 12 * 1024 * 1024
BASE = "/maf-sandbox/work"


class TransferCapExceeded(ValueError):
    """Identify file transfer overflow separately from path confinement refusals."""


def parts(path: str, directory: str) -> tuple[str, ...]:
    """Normalize within two confinement boundaries, without following filesystem links."""

    def relative(value: str, root: PurePosixPath) -> tuple[str, ...]:
        candidate = PurePosixPath(value)
        if "\x00" in value or "\\" in value or ".." in candidate.parts:
            raise ValueError("Path escapes its boundary")
        if candidate.is_absolute():
            candidate = candidate.relative_to(root)
        return candidate.parts

    work = PurePosixPath(directory)
    if "\x00" in directory or "\\" in directory or ".." in work.parts:
        raise ValueError("Working directory escapes its boundary")
    if not work.is_absolute():
        work = PurePosixPath(BASE) / work
    return work.parts[1:] + relative(path, work)


def parent(path: str, directory: str, *, create: bool = False) -> tuple[int, str]:
    """Hold no-follow descriptors from the storage base to the operand's parent."""
    components = parts(path, directory)
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for name in components[:-1]:
            if create:
                try:
                    os.mkdir(name, mode=0o700, dir_fd=fd)
                except FileExistsError:
                    # The no-follow open below still validates the existing component.
                    pass
            try:
                child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            except OSError:
                if stat.S_ISLNK(os.stat(name, dir_fd=fd, follow_symlinks=False).st_mode):
                    raise ValueError("A path component is a symlink") from None
                raise
            os.close(fd)
            fd = child
        return fd, components[-1] if components else "."
    except BaseException:
        os.close(fd)
        raise


def kill_children() -> None:
    """Kill every other process in this PID namespace, including detached descendants."""
    try:
        os.kill(-1, signal.SIGKILL)
    except ProcessLookupError:
        # Children may exit before namespace cleanup sends the signal.
        pass


def execute(request: dict[str, Any]) -> dict[str, Any]:
    """Drain both streams with bounded storage and remove all descendants before replying."""
    directory = request["directory"]
    fd, leaf = parent(".", directory)
    try:
        cwd = os.open(leaf, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
    finally:
        os.close(fd)
    command = request["command"]
    if isinstance(command, str):
        command = ["/bin/sh", "-c", command]
    streams = [bytearray(), bytearray()]
    deadline = time.monotonic() + request["timeout"]
    try:
        with subprocess.Popen(
            command,
            cwd=f"/proc/self/fd/{cwd}",
            pass_fds=(cwd,),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        ) as process:
            assert process.stdout is not None and process.stderr is not None
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ, 0)
                selector.register(process.stderr, selectors.EVENT_READ, 1)
                try:
                    while selector.get_map():
                        if time.monotonic() >= deadline:
                            raise TimeoutError("Guest command exceeded deadline")
                        if process.poll() is not None:
                            kill_children()
                        for key, _ in selector.select(0.02):
                            data = os.read(key.fd, 65536)
                            if not data:
                                selector.unregister(key.fd)
                            else:
                                streams[int(key.data)].extend(data)
                                if sum(map(len, streams)) > request["output_limit"]:
                                    raise ValueError("Guest command exceeded output limit")
                    while process.poll() is None:
                        if time.monotonic() >= deadline:
                            raise TimeoutError("Guest command exceeded deadline")
                        time.sleep(0.01)
                finally:
                    kill_children()
            return {
                "exit_code": process.wait(),
                "stdout": base64.b64encode(streams[0]).decode(),
                "stderr": base64.b64encode(streams[1]).decode(),
            }
    finally:
        os.close(cwd)
        kill_children()
        while True:
            try:
                os.waitpid(-1, 0)
            except ChildProcessError:
                break


def dispatch(request: dict[str, Any]) -> dict[str, Any]:
    """Serve only bounded file operations and command execution."""
    operation = request["op"]
    if operation == "exec":
        try:
            return execute(request)
        except FileNotFoundError as error:
            return {
                "exit_code": 127,
                "stdout": "",
                "stderr": base64.b64encode(str(error).encode()).decode(),
            }
    if operation == "write" and parts(request["path"], request["directory"]) == parts(
        ".", request["directory"]
    ):
        raise ValueError("Cannot replace the working directory")
    fd, leaf = parent(request["path"], request["directory"], create=operation == "write")
    try:
        if operation == "stat":
            try:
                info = os.stat(leaf, dir_fd=fd, follow_symlinks=False)
            except FileNotFoundError:
                return {"missing": True}
            kind = (
                "file"
                if stat.S_ISREG(info.st_mode)
                else "directory"
                if stat.S_ISDIR(info.st_mode)
                else "symlink"
                if stat.S_ISLNK(info.st_mode)
                else "other"
            )
            return {"kind": kind, "size": info.st_size if kind == "file" else None}
        flags = os.O_NOFOLLOW | os.O_NONBLOCK
        flags |= os.O_WRONLY | os.O_CREAT if operation == "write" else os.O_RDONLY
        with os.fdopen(
            os.open(leaf, flags, 0o600, dir_fd=fd), "wb" if operation == "write" else "rb"
        ) as file:
            if not stat.S_ISREG(os.fstat(file.fileno()).st_mode):
                raise OSError("Only regular files may cross the boundary")
            if operation == "write":
                data = base64.b64decode(request["data"], validate=True)
                if len(data) > FILE_LIMIT:
                    raise TransferCapExceeded("File exceeds transfer limit")
                file.truncate(0)
                file.write(data)
                return {}
            if operation != "read":
                raise ValueError("Unknown operation")
            limit = min(request["max_bytes"], FILE_LIMIT)
            data = file.read(limit + 1)
            if len(data) > limit:
                raise TransferCapExceeded("File exceeds transfer limit")
            return {"data": base64.b64encode(data).decode()}
    except OSError as error:
        if error.errno == errno.ELOOP and operation == "write":
            raise ValueError("The file is a symlink") from None
        raise
    finally:
        os.close(fd)


def main() -> None:
    """Keep control pipes private from guest commands and serve framed requests."""
    if os.getpid() != 1:
        raise RuntimeError("Broker requires a private PID namespace")
    if ctypes.CDLL(None).prctl(4, 0, 0, 0, 0) != 0:
        raise RuntimeError("Could not protect broker process descriptors")
    os.makedirs(BASE, mode=0o700, exist_ok=True)
    print('{"ready":1}', flush=True)
    while True:
        frame = sys.stdin.buffer.readline(FRAME_LIMIT + 1)
        if not frame:
            return
        if len(frame) > FRAME_LIMIT or not frame.endswith(b"\n"):
            raise ValueError("Invalid request frame")
        request = json.loads(frame)
        try:
            result = dispatch(request)
            response = {"id": request["id"], "result": result}
        except (OSError, ValueError) as error:
            response = {
                "id": request["id"],
                "error": type(error).__name__,
                "detail": str(error)[:1024],
            }
        print(json.dumps(response, separators=(",", ":")), flush=True)
        while True:
            try:
                pid, _ = os.waitpid(-1, os.WNOHANG)
                if pid == 0:
                    break
            except ChildProcessError:
                break


if __name__ == "__main__":
    main()
