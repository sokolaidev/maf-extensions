"""Opt-in antivirus conformance through the same checks used for image release."""

import asyncio
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from antivirus_qualification import check


@pytest.mark.parametrize("backend", ["docker", "acas"])
def test_antivirus_backend_conformance(backend, tmp_path):
    image = os.environ.get(f"MAF_ANTIVIRUS_{backend.upper()}_IMAGE")
    if not image:
        pytest.skip(f"requires MAF_ANTIVIRUS_{backend.upper()}_IMAGE opt-in")
    result = asyncio.run(check(image, provider=backend, directory=tmp_path))
    assert len(result["checks"]) >= 4
    assert result["containersRemaining"] == []
