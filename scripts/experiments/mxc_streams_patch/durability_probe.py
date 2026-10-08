"""Repeat the format-4 crash matrix with the separate-stream transport."""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

from scripts.experiments.mxc_session_patch import durability_probe as baseline
from scripts.experiments.mxc_streams_patch.native_probe import streams
from scripts.experiments.mxc_streams_patch.shared_call import call
from scripts.experiments.mxc_streams_patch.transport import RESULT_LIMIT


def launch(
    helper: Path,
    startup: Path,
    state: Path,
    call_id: str,
    value: int,
    fault: str = "",
    quota: str = "",
) -> None:
    """Restart the adapted probe in a new process for every crash boundary."""
    with (state / f"{call_id}.log").open("wb") as log:
        child = subprocess.run(
            [
                sys.executable,
                "-m",
                __spec__.name,
                "--helper",
                str(helper),
                "--startup",
                str(startup),
                "--state-dir",
                str(state),
                "--worker",
                "--call-id",
                call_id,
                "--value",
                str(value),
                "--fault",
                fault,
                "--quota",
                quota,
            ],
            stdout=log,
            stderr=subprocess.STDOUT,
            timeout=240,
        )
    assert child.returncode == (74 if fault else 0), (call_id, child.returncode)


def console(result: bytes) -> bytes:
    """Every crash-matrix call must retain both full byte prefixes and omission counts."""
    stdout, stderr = streams(result)
    prefix = stdout.split(b"\n", 1)[0] + b"\n"
    pattern = bytes(range(256)) * 4096
    assert stdout == (prefix + pattern)[: len(pattern)]
    assert stderr == pattern
    metadata = json.loads(result)["streams"]
    assert metadata["stdout"]["omitted_bytes"] == len(prefix)
    assert metadata["stderr"]["omitted_bytes"] == 17
    return prefix


def main() -> int:
    """Retain all original crash and accounting assertions under the new result budget."""
    baseline.LIMITS = replace(baseline.LIMITS, result_bytes=RESULT_LIMIT)
    baseline.SCRATCH = replace(baseline.SCRATCH, entries=baseline.SCRATCH.entries + 4)
    baseline.call = (
        lambda store, call_id, code, helper, startup, scratch, output_limit, boundary=lambda _: None: (
            call(store, call_id, code, helper, startup, scratch, boundary)
        )
    )
    baseline.console = console
    original_program = baseline._program
    baseline._program = lambda value: (
        original_program(value)
        + b"\nimport os\nos.write(1,bytes(range(256))*4096)\nos.write(2,bytes(range(256))*4096+b'Z'*17)"
    )
    baseline._launch = launch
    return baseline.main()


if __name__ == "__main__":
    raise SystemExit(main())
