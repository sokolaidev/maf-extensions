"""Opt-in smoke coverage of the actual sample process on the native Hyperlight host."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_AGENT = _ROOT / "samples" / "experimental" / "hyperlight_acas_codeact" / "agent.py"


@pytest.mark.skipif(
    os.environ.get("MAF_HYPERLIGHT_LIVE") != "1", reason="requires opt-in and WHP/KVM"
)
def test_native_hyperlight_sample_process():
    # The CI contract is Linux; a developer's live test uses its native platform.
    if os.environ.get("GITHUB_ACTIONS") == "true":
        assert sys.platform == "linux", "the sample's live CI job must use Linux"
    env = dict(os.environ)
    env.update(APP_ENV="DEV", CI="true" if sys.platform == "linux" else "false")
    for key in list(env):
        if key.startswith(("AZURE_OPENAI_", "ACAS_SANDBOX_")):
            del env[key]
    completed = subprocess.run(
        [sys.executable, str(_AGENT), "--smoke"],
        cwd=_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "[measured] Backend: hyperlight" in completed.stdout
    assert f"[measured] Hyperlight host: {sys.platform}" in completed.stdout
    assert "354224848179261915075" in completed.stdout
    assert "[measured] Disposed " in completed.stdout
