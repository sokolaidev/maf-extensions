"""Audit consumers can branch exhaustively and reject misspelled cleanup states."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

import maf_sandbox


@pytest.mark.parametrize("misspelled", [False, True])
def test_cleanup_vocabulary_type_checks_for_a_consumer(tmp_path, misspelled):
    consumer = tmp_path / "consumer.py"
    consumer.write_text(
        """from typing import assert_never
from maf_sandbox import ProcessCleanup, ProcessCleanupOutcome, ProcessCleanupReach

def result(event: ProcessCleanup) -> tuple[ProcessCleanupOutcome, ProcessCleanupReach]:
    return event.outcome, event.reach

def describe(outcome: ProcessCleanupOutcome, reach: ProcessCleanupReach) -> str:
    match outcome:
        case "sent" | "absent" | "refused" | "replaced" | "unrecorded" | "unknown":
            pass
        case _:
            assert_never(outcome)
    match reach:
        case "group" | "program" | "nothing":
            return outcome + reach
        case _:
            assert_never(reach)

ProcessCleanup(key=None, instance_id="i", run_id="r", pid=2, pgid=None,
               outcome=OUTCOME, reach=REACH, seconds=0.0)
""".replace("OUTCOME", '"snet"' if misspelled else '"sent"').replace(
            "REACH", '"gruop"' if misspelled else '"group"'
        ),
        encoding="utf-8",
    )
    config = tmp_path / "pyrightconfig.json"
    config.write_text(
        json.dumps(
            {
                "include": [consumer.name],
                "extraPaths": [str(Path(maf_sandbox.__file__).parent.parent)],
                "typeCheckingMode": "strict",
            }
        ),
        encoding="utf-8",
    )
    checked = subprocess.run(
        [
            sys.executable,
            "-m",
            "pyright",
            "--outputjson",
            "--pythonpath",
            sys.executable,
            "-p",
            str(config),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    result = json.loads(checked.stdout)
    errors = [row for row in result["generalDiagnostics"] if row["severity"] == "error"]
    assert checked.returncode == (1 if misspelled else 0), checked.stdout + checked.stderr
    assert len(errors) == (2 if misspelled else 0), errors
    if misspelled:
        assert all(row["rule"] == "reportArgumentType" for row in errors)
        assert any('"outcome"' in row["message"] for row in errors)
        assert any('"reach"' in row["message"] for row in errors)
