"""Opt-in model/ACAS verification of the actual draw.io sample and its cleanup evidence."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.skipif(
    os.environ.get("MAF_ACAS_DRAWIO_LIVE") != "1", reason="requires explicit ACAS/model opt-in"
)
def test_live_acas_drawio_repair():
    result = subprocess.run(
        [sys.executable, str(_ROOT / "samples/experimental/acas_drawio_repair/agent.py")],
        cwd=_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=900,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    records = [
        json.loads(line.strip().removeprefix("[measured] "))
        for line in result.stdout.splitlines()
        if line.strip().startswith("[measured] {")
    ]
    stages = {record["stage"]: record for record in records}
    assert stages["configuration"]["backend"] == "acas"
    assert stages["configuration"]["guest_egress"] == "closed"
    assert stages["configuration"]["allowed_hosts"] == []
    assert stages["rejected"]["delivered"] == 0
    assert "must reference a vertex" in stages["rejected"]["diagnostic"]
    assert stages["saved_and_read"]["bytes"] > 0
    assert 1 <= stages["saved_and_read"]["attempt"] <= 3
    assert stages["storage_cleanup"]["failures"] == 0
    assert stages["storage_cleanup"]["attempted"] == 1
    assert stages["sandbox_cleanup"]["complete"] is True
    assert records[-1]["stage"] == "complete"
