"""Run a live sample again only when its checker reports model non-convergence."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

from check_live_fix_loop_sample import MODEL_DID_NOT_CONVERGE as FIX_LOOP_RETRY
from check_live_host_tools_call_sample import MODEL_DID_NOT_CONVERGE as HOST_TOOLS_RETRY

# The budget belongs to each workflow step; the checker owns the retryable status.
PROFILES = {
    "sample13": ("sample 13", "13_bicep_fix_loop", "check_live_fix_loop_sample.py", FIX_LOOP_RETRY),
    "sample15": (
        "sample 15",
        "15_acas_codeact_host_tools",
        "check_live_host_tools_call_sample.py",
        HOST_TOOLS_RETRY,
    ),
    "sample15-docker": (
        "sample 15 on docker",
        "15_acas_codeact_host_tools",
        "check_live_host_tools_call_sample.py",
        HOST_TOOLS_RETRY,
    ),
}


def run_sample(command: list[str], output: Path) -> int:
    """Tee stdout to a UTF-8 log while preserving the sample's exit status."""
    with output.open("w", encoding="utf-8", newline="\n") as log:
        with subprocess.Popen(
            command, stdout=subprocess.PIPE, text=True, encoding="utf-8"
        ) as process:
            assert process.stdout is not None
            for line in process.stdout:
                sys.stdout.write(line)
                sys.stdout.flush()
                log.write(line)
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
    label, directory, _, retryable = PROFILES[profile]
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
                f"::error title={label} did not run::the sample exited {status}, so the check saw nothing"
            )
            break
        try:
            status = check()
        except OSError as error:
            print(str(error), file=sys.stderr)
            status = 127 if isinstance(error, FileNotFoundError) else 126
        if status < 0:
            status = 128 - status
        if status != retryable:
            break
        if attempts < allowed:
            action = "two-turn loop" if profile == "sample13" else "walk"
            print(
                f"::warning title={label} retried::the model's half did not converge on "
                f"attempt {attempts} of {allowed} and every other measurement passed, "
                f"so the {action} runs again (#421)"
            )
    suffix = " on docker" if profile == "sample15-docker" else ""
    said = f"samples/{directory}{suffix}: exit {status} after {attempts} attempt(s), {allowed} allowed."
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
    _, directory, checker, _ = PROFILES[args.profile]
    sample_command = ["uv", "run", "--no-project", f"samples/{directory}/agent.py"]
    check_command = [sys.executable, str(Path(__file__).with_name(checker))]
    if args.profile == "sample15-docker":
        check_command.append("--docker")
    check_command.append(str(args.output))
    return retry(
        args.profile,
        args.allowed,
        Path(os.environ["GITHUB_STEP_SUMMARY"]),
        lambda: run_sample(sample_command, args.output),
        lambda: subprocess.run(check_command).returncode,
    )


if __name__ == "__main__":
    raise SystemExit(main())
