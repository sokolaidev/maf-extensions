"""Repeat the format-4 crash matrix with the separate-stream transport."""

from __future__ import annotations

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


def main() -> int:
    """Retain all original crash and accounting assertions under the new result budget."""
    baseline.LIMITS = replace(baseline.LIMITS, result_bytes=RESULT_LIMIT)
    baseline.SCRATCH = replace(baseline.SCRATCH, entries=baseline.SCRATCH.entries + 4)
    baseline.call = (
        lambda store, call_id, code, helper, startup, scratch, output_limit, boundary=lambda _: None: (
            call(store, call_id, code, helper, startup, scratch, boundary)
        )
    )
    baseline.console = lambda result: streams(result)[0]
    baseline._launch = launch
    return baseline.main()


if __name__ == "__main__":
    raise SystemExit(main())
