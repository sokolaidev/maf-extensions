"""The bicep CLI download is checked against one pinned digest before it is installed."""

from __future__ import annotations

import re

import pytest
import yaml
from _workflow_commands import LIVE_WORKFLOW, ROOT

pytestmark = pytest.mark.workflow

DOCKERFILE = ROOT / "images/bicep-sandbox/Dockerfile"
DIGEST = re.compile(r"\b[0-9a-f]{64}\b")


def _install_step() -> str:
    workflow = yaml.safe_load(LIVE_WORKFLOW.read_text("utf-8"))
    (block,) = [
        step["run"]
        for job in workflow["jobs"].values()
        for step in job.get("steps", [])
        if "bicep-linux-x64" in step.get("run", "")
    ]
    return block


def test_workflow_checks_the_download_before_installing_it():
    block = _install_step()
    assert block.index("sha256sum -c") < block.index("sudo install")
    assert block.index("set -euo pipefail") < block.index("sha256sum -c")


def test_dockerfile_checks_the_download_before_marking_it_executable():
    text = DOCKERFILE.read_text("utf-8")
    assert text.index("sha256sum -c") < text.index("chmod +x")


def test_workflow_and_image_pin_the_same_version_and_digest():
    block = _install_step()
    text = DOCKERFILE.read_text("utf-8")
    (version,) = re.findall(r"^ARG BICEP_VERSION=(\S+)$", text, re.MULTILINE)
    (digest,) = re.findall(r"^ARG BICEP_SHA256=(\S+)$", text, re.MULTILINE)
    assert f"/download/{version}/bicep-linux-x64" in block
    assert DIGEST.findall(block) == [digest]
