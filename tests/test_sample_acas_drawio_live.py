"""Opt-in model/ACAS verification of the actual draw.io sample and its cleanup evidence."""

from __future__ import annotations

import json
import math
import os
import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.skipif(
    os.environ.get("MAF_ACAS_DRAWIO_LIVE") != "1", reason="requires explicit ACAS/model opt-in"
)
def test_live_acas_drawio_repair(capsys):
    result = subprocess.run(
        [sys.executable, str(_ROOT / "samples/experimental/acas_drawio_repair/agent.py")],
        cwd=_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=900,
    )
    records = [
        json.loads(line.strip().removeprefix("[measured] "))
        for line in result.stdout.splitlines()
        if line.strip().startswith("[measured] {")
    ]
    calls = [record for record in records if record["stage"] == "tool_call_ended"]
    # Keep successful-call evidence visible even when pytest captures passing tests.
    with capsys.disabled():
        for call in calls:
            print("[measured] " + json.dumps(call), flush=True)
    assert result.returncode == 0, result.stdout + result.stderr
    stages = {record["stage"]: record for record in records}
    assert stages["configuration"]["backend"] == "acas"
    assert stages["configuration"]["guest_egress"] == "closed"
    assert stages["configuration"]["allowed_hosts"] == []
    assert stages["rejected"]["delivered"] == 0
    assert "must reference a vertex" in stages["rejected"]["diagnostic"]
    assert stages["saved_and_read"]["bytes"] > 0
    assert 1 <= stages["saved_and_read"]["attempt"] <= 3
    assert len(calls) == 1 + stages["saved_and_read"]["attempt"]
    assert len({call["call"] for call in calls}) == len(calls)
    for call in calls:
        assert call["tool"] == "create_drawio" and call["kind"] == "drawio"
        assert math.isfinite(call["seconds"]) and call["seconds"] > 0
        assert call["failure"] is None and call["unclean"] == 0
    assert stages["storage_cleanup"]["failures"] == 0
    assert stages["storage_cleanup"]["attempted"] == 1
    assert stages["sandbox_cleanup"]["complete"] is True
    assert records[-1]["stage"] == "complete"
