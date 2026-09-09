"""Validate live-test fixture resolution without contacting Azure."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def test_live_suite_resolves_its_fixtures_without_running_them():
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "--setup-plan", "-q", "tests/test_acas_e2e.py"],
        cwd=Path(__file__).resolve().parents[1],
        env={**os.environ, "ACAS_SANDBOX_ENDPOINT": "https://sandbox.example.test"},
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert result.returncode == 0, result.stdout + result.stderr
