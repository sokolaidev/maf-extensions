"""Repeat the format-4 crash matrix with the bounded-file transport."""

from __future__ import annotations

import base64
import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

from scripts.experiments.mxc_files_patch.request import FileLimits, Request
from scripts.experiments.mxc_files_patch.shared_call import call
from scripts.experiments.mxc_files_patch.transport import result_limit
from scripts.experiments.mxc_session_patch import durability_probe as baseline
from scripts.experiments.mxc_streams_patch.native_probe import streams


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
    assert stderr == b""
    artifacts = json.loads(result)["artifacts"]
    assert len(artifacts) == 1 and artifacts[0]["name"] == "result.bin"
    assert base64.b64decode(artifacts[0]["base64"], validate=True) == stdout.strip()
    return stdout


def main() -> int:
    """Retain all original crash and accounting assertions under the new result budget."""

    def request(code: bytes) -> Request:
        return Request(code, (), ("result.bin",), FileLimits())

    baseline.LIMITS = replace(baseline.LIMITS, result_bytes=result_limit(request(b"pass")))
    baseline.SCRATCH = replace(
        baseline.SCRATCH,
        bytes=baseline.SCRATCH.bytes + 80 * 1024**2,
        store_bytes=baseline.SCRATCH.store_bytes + 160 * 1024**2,
        entries=baseline.SCRATCH.entries + 12,
    )
    baseline.call = (
        lambda store, call_id, code, helper, startup, scratch, output_limit, boundary=lambda _: None: (
            call(store, call_id, request(code), helper, startup, scratch, boundary)
        )
    )
    baseline.console = console
    original_program = baseline._program

    def program(value: int) -> bytes:
        before = (
            ""
            if value == 1
            else f"assert open(guest_session_path+'/value','rb').read()=={str(value - 1).encode()!r}\n"
        )
        after = f"\nopen(guest_session_path+'/value','wb').write({str(value).encode()!r})\nopen('result.bin','wb').write({str(value).encode()!r})"
        return before.encode() + original_program(value) + after.encode()

    baseline._program = program
    baseline._launch = launch
    return baseline.main()


if __name__ == "__main__":
    raise SystemExit(main())
