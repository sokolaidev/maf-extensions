"""Offline exit-status checks for archived benchmark commands."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

RUNS = Path(__file__).parents[1] / "runs"


def _bash() -> str:
    if os.name == "nt":
        candidate = Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "Git/bin/bash.exe"
        if candidate.is_file():
            return str(candidate)
        pytest.skip("Git Bash is required on Windows")
    executable = shutil.which("bash")
    if executable is None:
        pytest.skip("Bash is unavailable")
    return executable


@pytest.mark.parametrize(
    "name,failure,prerequisite_failure",
    [
        (name, failure, False)
        for name in ("run65-stream.sh", "run65-rerun.sh", "run67-stream.sh", "run67-after.sh")
        for failure in (False, True)
    ]
    + [("run67-after.sh", False, True)],
)
def test_archived_script_propagates_failures(
    tmp_path: Path, name: str, failure: bool, prerequisite_failure: bool
) -> None:
    bash = _bash()
    original = next(RUNS.glob(f"*/{name}"))
    source = original.read_text(encoding="utf-8").replace('S="/tmp"', 'S="$PWD/work"')
    source = source.replace('BIN="cachebench_live"', 'BIN="$PWD/stub.sh"')
    source = source.replace("sleep 5", "sleep 0.01")
    (tmp_path / name).write_text(source, encoding="utf-8", newline="\n")
    status = 17 if failure else 0
    stub = (
        f'#!/usr/bin/env bash\ncase " $* " in *" --seed-offset 0 "*) exit {status};; esac\nexit 0\n'
    )
    (tmp_path / "stub.sh").write_text(stub, encoding="utf-8", newline="\n")
    if name == "run67-after.sh":
        directory = tmp_path / "work/run67"
        directory.mkdir(parents=True)
        (directory / "progress.txt").write_text(
            "".join(
                f"DONE gpt-6-luna f3.0 s{seed} exit={int(prerequisite_failure and seed == 0)}\n"
                for seed in range(5)
            ),
            encoding="utf-8",
        )
        worker = tmp_path / "work/run67-stream.sh"
        worker.write_text(
            f'#!/usr/bin/env bash\necho "$2" >> "$PWD/work/workers.txt"\n'
            f'if [ "$2" = s0 ]; then exit {status}; fi\nexit 0\n',
            encoding="utf-8",
            newline="\n",
        )
        args: list[str] = []
    elif name == "run67-stream.sh":
        args = ["gpt-6-luna", "test", "0.9:0", "0.9:1"]
    else:
        args = ["0", "1"]
    result = subprocess.run(
        [
            bash,
            "-c",
            'chmod +x stub.sh work/run67-stream.sh 2>/dev/null; STRATS=none bash "$@"',
            "--",
            name,
            *args,
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )
    assert result.returncode == (1 if prerequisite_failure else status), result.stderr
    if prerequisite_failure:
        assert not (tmp_path / "work/workers.txt").exists()
        return
    progress = next((tmp_path / "work").glob("*/progress.txt")).read_text(encoding="utf-8")
    if name == "run67-after.sh":
        assert len((tmp_path / "work/workers.txt").read_text(encoding="utf-8").splitlines()) == 5
        assert ("half complete" in progress) is not failure
    else:
        assert f"exit={status}" in progress
        assert progress.count("DONE") == 2
