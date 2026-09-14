"""Opt-in model/ACAS verification of the actual draw.io sample and its cleanup evidence."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_SPEC = importlib.util.spec_from_file_location(
    "check_live_drawio_sample", _ROOT / "scripts/check_live_drawio_sample.py"
)
assert _SPEC and _SPEC.loader
check = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(check)


@pytest.mark.skipif(
    os.environ.get("MAF_ACAS_DRAWIO_LIVE") != "1", reason="requires explicit ACAS/model opt-in"
)
def test_live_acas_drawio_repair(capsys):
    result = subprocess.run(
        [sys.executable, str(_ROOT / "samples/18_acas_drawio_repair/agent.py")],
        cwd=_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=900,
    )
    records = check.records(result.stdout)
    calls = [record for record in records if record["stage"] == "tool_call_ended"]
    # Keep successful-call evidence visible even when pytest captures passing tests.
    with capsys.disabled():
        for call in calls:
            print("[measured] " + json.dumps(call), flush=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert check.assess(result.stdout) == []
