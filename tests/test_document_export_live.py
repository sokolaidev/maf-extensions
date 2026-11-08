"""Opt-in exact-image PDF export qualification."""

import asyncio
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from document_export_qualification import check


def test_document_export_docker(tmp_path):
    image = os.environ.get("MAF_DOCUMENT_EXPORT_IMAGE")
    if not image:
        pytest.skip("requires MAF_DOCUMENT_EXPORT_IMAGE opt-in")
    result = asyncio.run(check(image, directory=tmp_path))
    assert result["containersRemaining"] == []
    assert result["mermaidFallbackVerified"] is True
