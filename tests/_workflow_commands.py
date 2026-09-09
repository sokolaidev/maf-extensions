"""Read Python command arguments from the production workflow's thin shell wrappers."""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def command_arguments(block: str, script: str, variables: dict[str, str]) -> list[str]:
    """Expand only named environment variables, without interpreting any shell code."""
    commands = block.replace("\\\n", " ").splitlines()
    command = next(
        line for line in commands if script in line and not line.lstrip().startswith("#")
    )
    tokens = shlex.split(command)

    def expand(match: re.Match[str]) -> str:
        return variables[match[1] or match[2]]

    return [re.sub(r"\$\{(\w+)\}|\$(\w+)", expand, token) for token in tokens[2:]]


def run_release(tmp_path: Path, arguments: list[str]) -> subprocess.CompletedProcess[bytes]:
    """Execute the production release entry point with isolated Actions output files."""
    for name in ("out.txt", "summary.md"):
        (tmp_path / name).touch()
    return subprocess.run(
        [sys.executable, str(ROOT / "scripts/release_workflow.py"), *arguments],
        capture_output=True,
        cwd=tmp_path,
        env=os.environ
        | {
            "PYTHONUTF8": "1",
            "GITHUB_OUTPUT": str(tmp_path / "out.txt"),
            "GITHUB_STEP_SUMMARY": str(tmp_path / "summary.md"),
        },
    )
