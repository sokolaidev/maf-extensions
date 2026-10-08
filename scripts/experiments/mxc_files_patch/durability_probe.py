"""Repeat the format-4 crash matrix with the bounded-file transport."""

from __future__ import annotations

import base64
import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

from scripts.experiments.mxc_files_patch.native_probe import IO
from scripts.experiments.mxc_files_patch.request import FileLimits, Request
from scripts.experiments.mxc_files_patch.shared_call import call
from scripts.experiments.mxc_files_patch.transport import result_limit
from scripts.experiments.mxc_session_patch import durability_probe as baseline
from scripts.experiments.mxc_session_patch.host_store import Refused
from scripts.experiments.mxc_session_patch.native_journal import NativeJournal
from scripts.experiments.mxc_session_patch.shared_store import SharedStore
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
    """Require the saved artifact to match the restored guest value on every replay."""
    stdout, stderr = streams(result)
    assert stderr == b""
    artifacts = json.loads(result)["artifacts"]
    assert len(artifacts) == 1 and artifacts[0]["name"] == "result.bin"
    assert base64.b64decode(artifacts[0]["base64"], validate=True) == stdout.strip()
    return stdout


def main() -> int:
    """Retain all original crash and accounting assertions under the new result budget."""

    def request(code: bytes) -> Request:
        return Request(IO + code, (), ("result.bin",), FileLimits())

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
            else f"assert read_file(guest_session_path+'/value')=={str(value - 1).encode()!r}\n"
        )
        after = f"\nwrite_file(guest_session_path+'/value',{str(value).encode()!r})\nwrite_file('result.bin',{str(value).encode()!r})"
        return before.encode() + original_program(value) + after.encode()

    baseline._program = program
    original_qualify = baseline.qualify

    def qualify(helper: Path, startup: Path, state: Path) -> dict:
        report = original_qualify(helper, startup, state)
        root = state / "artifact-failure"
        profile = baseline._profile_for(helper, startup)
        with SharedStore(root, "one", profile, baseline.LIMITS) as store:
            saved = call(store, "seed", request(program(1)), helper, startup, baseline.SCRATCH)
            bad = Request(
                IO + b"mxc_durable_value=999; write_file(guest_session_path+'/value',b'999')",
                (),
                ("missing",),
                FileLimits(),
            )
            try:
                call(store, "failed", bad, helper, startup, baseline.SCRATCH)
            except Refused:
                pass
            else:
                raise AssertionError("missing artifact published success")
        with SharedStore(root, "one", profile, baseline.LIMITS) as store:
            NativeJournal(store).reclaim("failed")
            recovered = call(
                store, "verify", request(program(2)), helper, startup, baseline.SCRATCH
            )
            assert console(recovered) == b"2\n"
            assert (
                call(
                    store,
                    "seed",
                    request(program(1)),
                    state / "absent",
                    state / "absent",
                    baseline.SCRATCH,
                )
                == saved
            )
            baseline.reconcile(store)
        report["artifact_failure_preserved_previous_python_and_files"] = True
        return report

    baseline.qualify = qualify
    baseline._launch = launch
    return baseline.main()


if __name__ == "__main__":
    raise SystemExit(main())
