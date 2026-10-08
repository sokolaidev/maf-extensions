"""Run one closed-network MXC experiment with atomic local checkpoint/result publication."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import platform
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path

if __package__:
    from .host_store import CHUNK, Refused, Store
else:
    from host_store import CHUNK, Refused, Store


def digest(path: Path) -> str:
    """Hash a runtime artifact without retaining its contents."""
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def atomic_report(path: Path, value: dict[str, object]) -> None:
    """Publish probe readiness separately from guest output."""
    staged = path.with_suffix(".part")
    staged.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")
    staged.replace(path)


def chart_data(combined: str) -> dict[str, str]:
    """Decode bounded experimental chart data; these guest bytes never carry control status."""
    chunks: list[bytes] = []
    total = 0
    finished = False
    for line in combined.splitlines():
        if line.startswith("MAF_CHART:"):
            _, sequence, encoded = line.split(":", 2)
            if finished or sequence != str(len(chunks)):
                raise Refused("chart chunks are duplicate or out of order")
            data = base64.b64decode(encoded, validate=True)
            total += len(data)
            if not data or len(data) > 384 or total > CHUNK // 2:
                raise Refused("chart exceeds limit")
            chunks.append(data)
        elif line.startswith("MAF_CHART_END:"):
            _, size, expected_hash = line.split(":", 2)
            if finished or not chunks:
                raise Refused("unexpected chart trailer")
            chart = b"".join(chunks)
            if size != str(total) or hashlib.sha256(chart).hexdigest() != expected_hash:
                raise Refused("chart hash or size differs")
            if not chart.startswith(b"\x89PNG\r\n\x1a\n"):
                raise Refused("invalid chart signature")
            finished = True
    if chunks and not finished:
        raise Refused("chart transfer is incomplete")
    return {"chart.png": base64.b64encode(b"".join(chunks)).decode("ascii")} if finished else {}


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
    control = json.loads(report.read_bytes())
    if (
        not isinstance(control, dict)
        or set(control) != {"captured", "output"}
        or control["captured"] is not True
    ):
        raise Refused("invalid bounded native completion")
    metadata = control["output"]
    if not isinstance(metadata, dict) or set(metadata) != {
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


def execute(
    helper: Path,
    startup: Path,
    work: Path,
    code: bytes,
    output_limit: int | None = None,
    before_start: Callable[[subprocess.Popen[bytes]], None] = lambda _: None,
    checkpoint_limits: tuple[int, int] | None = None,
    check_active: Callable[[], None] = lambda: None,
) -> bytes:
    """Supervise one helper; opt-in capture keeps payload separate from native control."""
    if checkpoint_limits is not None and (
        output_limit is None
        or any(type(value) is not int or value <= 0 for value in checkpoint_limits)
    ):
        raise Refused("invalid bounded checkpoint configuration")
    helper, startup, work = (path.resolve() for path in (helper, startup, work))
    request = work / "code.py"
    request.write_bytes(code)
    report = work / "native.json"
    env = {key: os.environ[key] for key in ("SystemRoot", "WINDIR") if key in os.environ}
    env.update({key: str(work) for key in ("HOME", "USERPROFILE", "TMP", "TEMP", "TMPDIR")})
    with subprocess.Popen(
        [
            str(helper),
            "call-stored"
            if checkpoint_limits is not None
            else "call-bounded"
            if output_limit is not None
            else "call-owned",
            str(startup),
            str(work / "candidate"),
            str(request),
            str(report),
            *([str(output_limit)] if output_limit is not None else []),
            *([str(value) for value in checkpoint_limits] if checkpoint_limits is not None else []),
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
        if output_limit is None and json.loads(report.read_bytes()) != {"captured": True}:
            raise Refused("invalid native control report")
    if output_limit is not None:
        return json.dumps(bounded_result(report, output_limit), sort_keys=True).encode()
    combined = outputs[0].decode("utf-8", errors="replace")
    artifacts = chart_data(combined)
    return json.dumps(
        {
            "combined_output": combined,
            "native_stderr": outputs[1].decode("utf-8", errors="replace"),
            "artifacts": artifacts,
        },
        sort_keys=True,
    ).encode()


def main() -> int:
    """Use explicit paths and fresh scratch space; fault stops are for the crash probe only."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--helper", type=Path, required=True)
    parser.add_argument("--startup", type=Path, required=True)
    parser.add_argument("--store", type=Path, required=True)
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--code", type=Path, required=True)
    parser.add_argument("--call-id", required=True)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--bounded-output", action="store_true")
    parser.add_argument("--output-limit", type=int, default=CHUNK)
    parser.add_argument(
        "--fault",
        choices=(
            "checkpoint_stored",
            "before_commit",
            "after_commit",
            "before_ack",
        ),
    )
    args = parser.parse_args()
    if args.output_limit < 0:
        parser.error("output limit must be nonnegative")
    helper = args.helper.resolve(strict=True)
    startup = args.startup.resolve(strict=True)
    if args.code.stat().st_size > 65536:
        parser.error("code exceeds probe limit")
    code = args.code.read_bytes()
    profile = {
        "format": "sqlite-chunks-v1",
        "helper": digest(helper),
        "startup_index": digest(startup / "index.json"),
        "platform": f"{platform.system()}-{platform.machine()}",
        "machine": platform.node(),
        "policy": "closed-no-mounts",
        "session": args.session_id,
    }
    if args.bounded_output:
        profile["capture"] = "truncate-continue-v1"
        profile["output_limit"] = str(args.output_limit)
    work = args.work.resolve()
    work.mkdir(parents=True, exist_ok=False)

    def boundary(point: str) -> None:
        if point == args.fault:
            atomic_report(args.report, {"paused": point})
            while True:
                time.sleep(1)

    with Store(args.store, profile) as store:
        result = store.begin(args.call_id, code)
        replayed = result is not None
        if result is None:
            restored = work / "restored"
            previous = store.restore(restored)
            result = execute(
                helper,
                restored if previous else startup,
                work,
                code,
                args.output_limit if args.bounded_output else None,
            )
            store.commit(args.call_id, work / "candidate", result, boundary)
        boundary("before_ack")
        atomic_report(
            args.report,
            {
                "call_id": args.call_id,
                "redelivered": replayed,
                "result_sha256": hashlib.sha256(result).hexdigest(),
                "result": json.loads(result),
            },
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
