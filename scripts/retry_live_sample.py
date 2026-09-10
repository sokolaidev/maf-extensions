"""Run a live sample again only when its checker reports model non-convergence."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from io import BufferedReader
from pathlib import Path

from check_live_fix_loop_sample import MODEL_DID_NOT_CONVERGE as FIX_LOOP_RETRY
from check_live_host_tools_call_sample import MODEL_DID_NOT_CONVERGE as HOST_TOOLS_RETRY


@dataclass(frozen=True)
class Profile:
    """Commands and reporting for one live sample route; the workflow owns its budget."""

    label: str
    directory: str
    checker: str
    retryable: int
    action: str
    suffix: str = ""
    checker_args: tuple[str, ...] = ()


PROFILES = {
    "sample13": Profile(
        "sample 13",
        "13_bicep_fix_loop",
        "check_live_fix_loop_sample.py",
        FIX_LOOP_RETRY,
        "two-turn loop",
    ),
    "sample15": Profile(
        "sample 15",
        "15_acas_codeact_host_tools",
        "check_live_host_tools_call_sample.py",
        HOST_TOOLS_RETRY,
        "walk",
    ),
    "sample15-docker": Profile(
        "sample 15 on docker",
        "15_acas_codeact_host_tools",
        "check_live_host_tools_call_sample.py",
        HOST_TOOLS_RETRY,
        "walk",
        " on docker",
        ("--docker",),
    ),
}


def run_sample(command: list[str], output: Path) -> int:
    """Tee stdout byte-for-byte while preserving the sample's exit status."""
    sys.stdout.flush()
    with output.open("wb") as log:
        with subprocess.Popen(command, stdout=subprocess.PIPE) as process:
            assert isinstance(process.stdout, BufferedReader)
            while chunk := process.stdout.read1(65536):
                sys.stdout.buffer.write(chunk)
                sys.stdout.buffer.flush()
                log.write(chunk)
            return process.wait()


def retry(
    profile: str,
    allowed: int,
    summary: Path,
    sample: Callable[[], int],
    check: Callable[[], int],
) -> int:
    """Spend the attempt budget only on checker verdicts about the model's half."""
    if allowed < 1:
        raise ValueError("allowed must be positive")
    config = PROFILES[profile]
    attempts = 0
    status = 0
    while attempts < allowed:
        attempts += 1
        try:
            status = sample()
        except OSError as error:
            print(str(error), file=sys.stderr)
            status = 127 if isinstance(error, FileNotFoundError) else 126
        if status < 0:
            status = 128 - status
        if status:
            print(
                f"::error title={config.label} did not run::the sample exited {status}, so the check saw nothing"
            )
            break
        try:
            status = check()
        except OSError as error:
            print(str(error), file=sys.stderr)
            status = 127 if isinstance(error, FileNotFoundError) else 126
        if status < 0:
            status = 128 - status
        if status != config.retryable:
            break
        if attempts < allowed:
            print(
                f"::warning title={config.label} retried::the model's half did not converge on "
                f"attempt {attempts} of {allowed} and every other measurement passed, "
                f"so the {config.action} runs again (#421)"
            )
    said = f"samples/{config.directory}{config.suffix}: exit {status} after {attempts} attempt(s), {allowed} allowed."
    print(said)
    with summary.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(said + "\n")
    return status


def main(argv: list[str] | None = None) -> int:
    """Resolve the production commands and execute the selected sample's retry policy."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("profile", choices=PROFILES)
    parser.add_argument("--allowed", type=int, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.allowed < 1:
        parser.error("--allowed must be positive")
    config = PROFILES[args.profile]
    sample_command = ["uv", "run", "--no-project", f"samples/{config.directory}/agent.py"]
    check_command = [
        sys.executable,
        str(Path(__file__).with_name(config.checker)),
        *config.checker_args,
        str(args.output),
    ]
    return retry(
        args.profile,
        args.allowed,
        Path(os.environ["GITHUB_STEP_SUMMARY"]),
        lambda: run_sample(sample_command, args.output),
        lambda: subprocess.run(check_command).returncode,
    )


if __name__ == "__main__":
    raise SystemExit(main())
