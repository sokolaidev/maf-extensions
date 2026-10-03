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
from pathlib import Path

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


def execute(helper: Path, startup: Path, work: Path, code: bytes) -> bytes:
    """Bound parent-side output and wall time; native buffering remains unqualified."""
    request = work / "code.py"
    request.write_bytes(code)
    report = work / "native.json"
    env = {key: os.environ[key] for key in ("SystemRoot", "WINDIR") if key in os.environ}
    env.update({key: str(work) for key in ("HOME", "USERPROFILE", "TMP", "TEMP", "TMPDIR")})
    with subprocess.Popen(
        [
            str(helper),
            "call-owned",
            str(startup),
            str(work / "candidate"),
            str(request),
            str(report),
        ],
        env=env,
        cwd=work,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ) as child:
        assert child.stdin is not None
        child.stdin.write(b"MXCOWN1\n")
        child.stdin.flush()
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
        if child.returncode != 0 or not report.exists():
            raise Refused(f"native execution/capture failed: {child.returncode}")
        if json.loads(report.read_text()) != {"captured": True}:
            raise Refused("invalid native control report")
    (work / "native.stdout").write_bytes(outputs[0])
    (work / "native.stderr").write_bytes(outputs[1])
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
            result = execute(helper, restored if previous else startup, work, code)
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
