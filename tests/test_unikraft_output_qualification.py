"""Qualification must distinguish executed controls from a successful empty test run."""

import importlib
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))
try:
    _execute = importlib.import_module("scripts.experiments.unikraft_output.qualify")._execute
finally:
    sys.path.remove(str(Path(__file__).parents[1]))


@pytest.mark.parametrize(
    ("code", "stdout"),
    [
        (0, "test result: ok. 0 passed; 0 failed; 0 ignored;"),
        (0, "test result: ok. 5 passed; 0 failed; 1 ignored;"),
        (1, "test result: ok. 5 passed; 0 failed; 0 ignored;"),
        (0, "test result: FAILED. 4 passed; 1 failed; 0 ignored;"),
    ],
)
def test_refuse_missing_skipped_or_failed_controls(tmp_path, monkeypatch, code, stdout):
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **kw: subprocess.CompletedProcess(a, code, stdout, ""),
    )
    with pytest.raises(RuntimeError, match="did not execute"):
        _execute(tmp_path, tmp_path, "capture", ["cargo", "test"], 5)
    assert (tmp_path / "capture.log").read_text() == stdout


def test_retain_executed_controls(tmp_path: Path, monkeypatch):
    stdout = "test result: ok. 5 passed; 0 failed; 0 ignored;"
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **kw: subprocess.CompletedProcess(a, 0, stdout, "diagnostic"),
    )
    assert _execute(tmp_path, tmp_path, "capture", ["cargo", "test"], 5) == stdout + "diagnostic"
