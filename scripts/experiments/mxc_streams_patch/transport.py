"""Bounded byte-stream result transport for the separate-kernel experiment."""

from __future__ import annotations

import base64
import json
import os
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path

from scripts.experiments.mxc_session_patch.host_store import (
    CHUNK,
    MAX_CHECKPOINT,
    MAX_FILES,
    Refused,
)

STREAM_LIMIT = CHUNK
RESULT_LIMIT = 3 * CHUNK
CONTROL_LIMIT = 1024
FORMAT = "mxc-byte-streams-v1"


def unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """Reject duplicate control keys rather than accepting the last value."""
    result = {}
    for key, value in pairs:
        if key in result:
            raise Refused("duplicate native control key")
        result[key] = value
    return result


def read_result(report: Path, checkpoint: bool) -> bytes:
    """Only a bounded native control file can authorize reading the separate payloads."""
    with report.open("rb") as stream:
        raw = stream.read(CONTROL_LIMIT + 1)
    if len(raw) > CONTROL_LIMIT:
        raise Refused("oversized native control")
    try:
        control = json.loads(raw, object_pairs_hook=unique)
    except (ValueError, UnicodeError) as error:
        raise Refused("malformed native control") from error
    if (
        not isinstance(control, dict)
        or set(control) != {"format", "completed", "checkpoint", "limit_bytes", "streams"}
        or control["format"] != FORMAT
        or control["completed"] is not True
        or control["checkpoint"] is not checkpoint
        or type(control["limit_bytes"]) is not int
        or control["limit_bytes"] != STREAM_LIMIT
        or not isinstance(control["streams"], dict)
        or set(control["streams"]) != {"stdout", "stderr"}
    ):
        raise Refused("invalid native completion")
    result = {"format": FORMAT, "limit_bytes": STREAM_LIMIT, "streams": {}}
    for name in ("stdout", "stderr"):
        meta = control["streams"][name]
        if (
            not isinstance(meta, dict)
            or set(meta) != {"retained_bytes", "omitted_bytes", "omitted_bytes_saturated"}
            or type(meta["retained_bytes"]) is not int
            or not 0 <= meta["retained_bytes"] <= STREAM_LIMIT
            or type(meta["omitted_bytes"]) is not int
            or not 0 <= meta["omitted_bytes"] <= 2**64 - 1
            or type(meta["omitted_bytes_saturated"]) is not bool
            or (meta["omitted_bytes_saturated"] and meta["omitted_bytes"] != 2**64 - 1)
            or (meta["omitted_bytes"] and meta["retained_bytes"] != STREAM_LIMIT)
        ):
            raise Refused("invalid stream metadata")
        with report.with_suffix(f".{name}.bin").open("rb") as stream:
            data = stream.read(STREAM_LIMIT + 1)
        if len(data) != meta["retained_bytes"]:
            raise Refused("stream length differs from native control")
        result["streams"][name] = {**meta, "base64": base64.b64encode(data).decode("ascii")}
    encoded = json.dumps(result, sort_keys=True, separators=(",", ":")).encode()
    if len(encoded) > RESULT_LIMIT:
        raise Refused("serialized streams exceed result allowance")
    return encoded


def execute(
    helper: Path,
    startup: Path,
    work: Path,
    code: bytes,
    *,
    checkpoint: bool = True,
    checkpoint_limits: tuple[int, int] = (MAX_CHECKPOINT, MAX_FILES),
    before_start: Callable[[subprocess.Popen[bytes]], None] = lambda _: None,
    cancel: threading.Event | None = None,
    timeout: float = 90,
) -> bytes:
    """Supervise the native owner, bounding diagnostics independently from payloads."""
    if len(code) > 65536 or not 0 < timeout <= 90:
        raise Refused("invalid code size or deadline")
    if (
        any(type(value) is not int or value <= 0 for value in checkpoint_limits)
        or checkpoint_limits[0] > MAX_CHECKPOINT
        or checkpoint_limits[1] > MAX_FILES
    ):
        raise Refused("invalid checkpoint allowance")
    helper, startup, work = (path.resolve() for path in (helper, startup, work))
    request = work / "code.py"
    request.write_bytes(code)
    report = work / "native.json"
    if any(work.glob("native*")) or (work / "candidate").exists():
        raise Refused("native destinations must be fresh")
    env = {key: os.environ[key] for key in ("SystemRoot", "WINDIR") if key in os.environ}
    env.update({key: str(work) for key in ("HOME", "USERPROFILE", "TMP", "TEMP", "TMPDIR")})
    with subprocess.Popen(
        [
            str(helper),
            "call-streams" if checkpoint else "execute-streams",
            str(startup),
            str(work / "candidate"),
            str(request),
            str(report),
            *(str(value) for value in checkpoint_limits),
        ],
        env=env,
        cwd=work,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ) as child:
        outputs = [bytearray(), bytearray()]
        overflow = threading.Event()

        def drain(index: int) -> None:
            pipe = child.stdout if index == 0 else child.stderr
            assert pipe is not None
            while data := pipe.read(65536):
                if len(outputs[index]) + len(data) > CHUNK:
                    overflow.set()
                    return
                outputs[index].extend(data)

        readers = [threading.Thread(target=drain, args=(i,), daemon=True) for i in range(2)]
        for reader in readers:
            reader.start()
        try:
            before_start(child)
            assert child.stdin is not None
            child.stdin.write(b"MXCOWN1\n")
            child.stdin.flush()
            deadline = time.monotonic() + timeout
            while child.poll() is None:
                if cancel is not None and cancel.is_set():
                    raise Refused("call cancelled")
                if overflow.is_set() or time.monotonic() >= deadline:
                    raise Refused("native diagnostics or deadline exceeded")
                time.sleep(0.02)
        finally:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=15)
            for reader in readers:
                reader.join(timeout=5)
            for name, data in zip(("stdout", "stderr"), outputs, strict=True):
                (work / f"native.{name}").write_bytes(data)
        if overflow.is_set() or any(reader.is_alive() for reader in readers):
            raise Refused("native diagnostics exceeded bounds")
        if cancel is not None and cancel.is_set():
            raise Refused("call cancelled")
        if child.returncode != 0:
            raise Refused(f"native call failed: {child.returncode}")
    return read_result(report, checkpoint)
