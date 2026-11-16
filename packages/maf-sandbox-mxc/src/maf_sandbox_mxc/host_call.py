"""Supervise bounded native helper execution and validate captured output."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from maf_sandbox_mxc._records import is_record

from .host_store import CHUNK
from .host_store import Refused as Refused


def digest(path: Path) -> str:
    """Hash a runtime artifact without retaining its contents."""
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def bounded_result_size(limit: int) -> int:
    """Maximum serialized console envelope, including base64 expansion and counters."""
    envelope = {
        "console_base64": "",
        "output": {
            "limit_bytes": limit,
            "retained_bytes": limit,
            "omitted_bytes": 2**64 - 1,
            "omitted_bytes_saturated": False,
            "truncated": True,
        },
    }
    return len(json.dumps(envelope, sort_keys=True).encode()) + 4 * ((limit + 2) // 3)


def bounded_result(report: Path, limit: int) -> dict[str, object]:
    """Validate host control separately from the bounded console payload."""
    if report.stat().st_size > 1024:
        raise Refused("oversized native control report")
    control: dict[str, Any] = json.loads(report.read_bytes())
    if (
        not is_record(control)
        or set(control) != {"captured", "output"}
        or control["captured"] is not True
    ):
        raise Refused("invalid bounded native completion")
    metadata: dict[str, Any] = control["output"]
    if not is_record(metadata) or set(metadata) != {
        "limit_bytes",
        "retained_bytes",
        "omitted_bytes",
        "omitted_bytes_saturated",
        "truncated",
    }:
        raise Refused("invalid output metadata")
    if any(
        type(metadata[key]) is not int for key in ("limit_bytes", "retained_bytes", "omitted_bytes")
    ):
        raise Refused("invalid output counters")
    if (
        metadata["limit_bytes"] != limit
        or not 0 <= metadata["retained_bytes"] <= limit
        or not 0 <= metadata["omitted_bytes"] <= 2**64 - 1
        or type(metadata["truncated"]) is not bool
        or type(metadata["omitted_bytes_saturated"]) is not bool
        or metadata["truncated"] != (metadata["omitted_bytes"] > 0)
        or (metadata["omitted_bytes_saturated"] and metadata["omitted_bytes"] != 2**64 - 1)
    ):
        raise Refused("inconsistent output metadata")
    with report.with_suffix(".output").open("rb") as stream:
        console = stream.read(limit + 1)
    if len(console) != metadata["retained_bytes"]:
        raise Refused("console length differs from native report")
    console.decode("utf-8", errors="strict")
    return {"console_base64": base64.b64encode(console).decode("ascii"), "output": metadata}


def _run(
    helper: Path,
    startup: Path,
    work: Path,
    code: bytes,
    command: str,
    limits: tuple[int, ...],
    before_start: Callable[[subprocess.Popen[bytes]], None] = lambda _: None,
    check_active: Callable[[], None] = lambda: None,
    before_spawn: Callable[[], None] = lambda: None,
) -> tuple[Path, bytes, bytes]:
    """Drain bounded diagnostics and require a separate native control report."""
    helper, startup, work = (path.resolve() for path in (helper, startup, work))
    request = work / "code.py"
    request.write_bytes(code)
    report = work / "native.json"
    env = {key: os.environ[key] for key in ("SystemRoot", "WINDIR") if key in os.environ}
    env.update({key: str(work) for key in ("HOME", "USERPROFILE", "TMP", "TEMP", "TMPDIR")})
    before_spawn()
    with subprocess.Popen(
        [
            str(helper),
            command,
            str(startup),
            str(work / "candidate"),
            str(request),
            str(report),
            *[str(value) for value in limits],
        ],
        env=env,
        cwd=work,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ) as child:
        assert child.stdin is not None
        try:
            before_start(child)
            check_active()
            child.stdin.write(b"MXCOWN1\n")
            child.stdin.flush()
        except BaseException:
            child.kill()
            child.wait(timeout=15)
            raise
        outputs = [bytearray(), bytearray()]
        overflow = threading.Event()

        def drain(index: int) -> None:
            stream = child.stdout if index == 0 else child.stderr
            assert stream is not None
            while data := stream.read(65536):
                if len(outputs[index]) + len(data) > CHUNK:
                    overflow.set()
                    return
                outputs[index].extend(data)

        readers = [threading.Thread(target=drain, args=(i,), daemon=True) for i in range(2)]
        for reader in readers:
            reader.start()
        deadline = time.monotonic() + 90
        try:
            while child.poll() is None:
                check_active()
                if overflow.is_set() or time.monotonic() >= deadline:
                    raise Refused("native output limit or deadline exceeded")
                time.sleep(0.02)
        finally:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=15)
            for reader in readers:
                reader.join(timeout=5)
        if overflow.is_set() or any(reader.is_alive() for reader in readers):
            raise Refused("native output was not completely bounded and drained")
        (work / "native.stdout").write_bytes(outputs[0])
        (work / "native.stderr").write_bytes(outputs[1])
        if child.returncode != 0 or not report.exists():
            raise Refused(f"native execution/capture failed: {child.returncode}")
        if report.stat().st_size > 1024:
            raise Refused("oversized native control report")
    return report, bytes(outputs[0]), bytes(outputs[1])


def execute(
    helper: Path,
    startup: Path,
    work: Path,
    code: bytes,
    output_limit: int,
    before_start: Callable[[subprocess.Popen[bytes]], None] = lambda _: None,
    checkpoint_limits: tuple[int, int] | None = None,
    check_active: Callable[[], None] = lambda: None,
    before_spawn: Callable[[], None] = lambda: None,
) -> bytes:
    """Supervise one helper with bounded output kept separate from native control."""
    if type(output_limit) is not int or output_limit < 0:
        raise Refused("output limit must be a nonnegative integer")
    if checkpoint_limits is not None and any(
        type(value) is not int or value <= 0 for value in checkpoint_limits
    ):
        raise Refused("invalid bounded checkpoint configuration")
    report, _, _ = _run(
        helper,
        startup,
        work,
        code,
        "call-stored" if checkpoint_limits is not None else "call-bounded",
        (output_limit, *(checkpoint_limits or ())),
        before_start,
        check_active,
        before_spawn,
    )
    return json.dumps(bounded_result(report, output_limit), sort_keys=True).encode()
