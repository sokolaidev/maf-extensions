"""Supervised file transport with host-owned completion and bounded artifact payloads."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path

from scripts.experiments.mxc_files_patch.request import Request
from scripts.experiments.mxc_session_patch.host_store import (
    CHUNK,
    MAX_CHECKPOINT,
    MAX_FILES,
    Refused,
)
from scripts.experiments.mxc_streams_patch.transport import STREAM_LIMIT, unique

FORMAT = "mxc-files-result-v1"
CONTROL_LIMIT = 16 * CHUNK


def result_limit(request: Request) -> int:
    """Reserve base64 payloads and bounded names without borrowing checkpoint capacity."""
    return 4 * ((2 * STREAM_LIMIT + request.limits.artifact_bytes + 2) // 3) + 4 * CHUNK


def prepare(work: Path, request: Request, restoring: bool) -> None:
    """Stage only host-selected bytes under fixed private host filenames."""
    metadata = json.loads(request.identity())
    metadata.update(token=uuid.uuid4().hex, restoring=restoring)
    offset = 0
    inputs = []
    with (work / "inputs.bin").open("xb") as stream:
        for item in request.inputs:
            inputs.append(
                {
                    "name": item.name,
                    "offset": offset,
                    "bytes": len(item.data),
                    "lifecycle": item.lifecycle,
                    "replace": item.replace,
                }
            )
            stream.write(item.data)
            offset += len(item.data)
    metadata["inputs"] = inputs
    metadata["limits"] = asdict(request.limits)
    raw = json.dumps(metadata, sort_keys=True, separators=(",", ":")).encode()
    if len(raw) > CONTROL_LIMIT:
        raise Refused("file request metadata exceeds allowance")
    with (work / "request.json").open("xb") as stream:
        stream.write(raw)


def _bounded(path: Path, limit: int) -> bytes:
    with path.open("rb") as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise Refused("native file exceeds allowance")
    return data


def _integer(value: object, maximum: int) -> bool:
    return type(value) is int and 0 <= value <= maximum


def read_result(report: Path, checkpoint: bool, request: Request) -> bytes:
    """Refuse any incomplete artifact set before authorizing durable publication."""
    try:
        control = json.loads(_bounded(report, CONTROL_LIMIT), object_pairs_hook=unique)
    except (ValueError, UnicodeError) as error:
        raise Refused("malformed native file completion") from error
    if (
        not isinstance(control, dict)
        or set(control)
        != {
            "format",
            "completed",
            "checkpoint",
            "limit_bytes",
            "streams",
            "artifacts",
            "workspace_bytes",
            "workspace_files",
        }
        or control["format"] != FORMAT
        or control["completed"] is not True
        or control["checkpoint"] is not checkpoint
        or type(control["limit_bytes"]) is not int
        or control["limit_bytes"] != STREAM_LIMIT
        or not isinstance(control["streams"], dict)
        or set(control["streams"]) != {"stdout", "stderr"}
        or not isinstance(control["artifacts"], list)
        or len(control["artifacts"]) != len(request.artifacts)
        or not _integer(control["workspace_bytes"], request.workspace.bytes)
        or not _integer(control["workspace_files"], request.workspace.files)
    ):
        raise Refused("invalid native file completion")
    result = {"format": FORMAT, "limit_bytes": STREAM_LIMIT, "streams": {}, "artifacts": []}
    for name in ("stdout", "stderr"):
        meta = control["streams"][name]
        if (
            not isinstance(meta, dict)
            or set(meta) != {"retained_bytes", "omitted_bytes", "omitted_bytes_saturated"}
            or not _integer(meta["retained_bytes"], STREAM_LIMIT)
            or not _integer(meta["omitted_bytes"], 2**64 - 1)
            or type(meta["omitted_bytes_saturated"]) is not bool
            or (meta["omitted_bytes_saturated"] and meta["omitted_bytes"] != 2**64 - 1)
            or (meta["omitted_bytes"] and meta["retained_bytes"] != STREAM_LIMIT)
        ):
            raise Refused("invalid stream metadata")
        data = _bounded(report.with_suffix(f".{name}.bin"), STREAM_LIMIT)
        if len(data) != meta["retained_bytes"]:
            raise Refused("stream size differs")
        result["streams"][name] = {**meta, "base64": base64.b64encode(data).decode("ascii")}
    payload = _bounded(report.with_suffix(".artifacts.bin"), request.limits.artifact_bytes)
    offset = 0
    for expected, meta in zip(sorted(request.artifacts), control["artifacts"], strict=True):
        if (
            not isinstance(meta, dict)
            or set(meta) != {"name", "offset", "bytes"}
            or meta["name"] != expected
            or not _integer(meta["offset"], len(payload))
            or meta["offset"] != offset
            or not _integer(meta["bytes"], request.limits.file_bytes)
            or meta["bytes"] > len(payload) - offset
        ):
            raise Refused("artifact inventory differs from request or payload")
        data = payload[offset : offset + meta["bytes"]]
        offset += len(data)
        result["artifacts"].append(
            {
                "name": expected,
                "bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
                "base64": base64.b64encode(data).decode("ascii"),
            }
        )
    if offset != len(payload):
        raise Refused("unreferenced artifact bytes")
    encoded = json.dumps(result, sort_keys=True, separators=(",", ":")).encode()
    if len(encoded) > result_limit(request):
        raise Refused("serialized file result exceeds allowance")
    return encoded


def execute(
    helper: Path,
    startup: Path,
    work: Path,
    file_request: Request,
    *,
    checkpoint: bool = True,
    restoring: bool = False,
    checkpoint_limits: tuple[int, int] = (MAX_CHECKPOINT, MAX_FILES),
    before_start: Callable[[subprocess.Popen[bytes]], None] = lambda _: None,
    cancel: threading.Event | None = None,
    timeout: float = 90,
) -> bytes:
    """Supervise the native owner, bounding diagnostics independently from payloads."""
    code = file_request.code
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
    prepare(work, file_request, restoring)
    report = work / "native.json"
    if any(work.glob("native*")) or (work / "candidate").exists():
        raise Refused("native destinations must be fresh")
    env = {key: os.environ[key] for key in ("SystemRoot", "WINDIR") if key in os.environ}
    env.update({key: str(work) for key in ("HOME", "USERPROFILE", "TMP", "TEMP", "TMPDIR")})
    with subprocess.Popen(
        [
            str(helper),
            "call-files" if checkpoint else "execute-files",
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
    return read_result(report, checkpoint, file_request)
