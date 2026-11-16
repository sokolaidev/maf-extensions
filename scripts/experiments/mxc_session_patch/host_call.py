"""Run a legacy single-owner MXC checkpoint publication and crash probe."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import platform
import time
from pathlib import Path

from maf_sandbox_mxc.host_call import _run, digest
from maf_sandbox_mxc.host_call import execute as bounded_execute
from maf_sandbox_mxc.host_store import CHUNK
from maf_sandbox_mxc.host_store import Refused as Refused

from .host_store import Store


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


def execute(
    helper: Path, startup: Path, work: Path, code: bytes, output_limit: int | None = None
) -> bytes:
    """Retain the legacy chart protocol for the single-owner publication probe."""
    if output_limit is not None:
        return bounded_execute(helper, startup, work, code, output_limit)
    report, stdout, stderr = _run(helper, startup, work, code, "call-owned", ())
    if json.loads(report.read_bytes()) != {"captured": True}:
        raise Refused("invalid native control report")
    combined = stdout.decode("utf-8", errors="replace")
    artifacts = chart_data(combined)
    return json.dumps(
        {
            "combined_output": combined,
            "native_stderr": stderr.decode("utf-8", errors="replace"),
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
