"""The bicep CLI download is checked against one pinned digest before it is installed."""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys

import pytest
import yaml
from _workflow_commands import LIVE_WORKFLOW, ROOT

pytestmark = pytest.mark.workflow

DOCKERFILE = ROOT / "images/bicep-sandbox/Dockerfile"
sys.path.insert(0, str(ROOT / "scripts"))
from bicep_image_pin import read_pin  # noqa: E402


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
    assert '"$HARNESS"/scripts/bicep_image_pin.py images/bicep-sandbox/Dockerfile' in block
    assert 'read -r BICEP_VERSION BICEP_SHA256 <<< "$BICEP_PIN"' in block
    assert "/download/$BICEP_VERSION/bicep-linux-x64" in block
    assert 'echo "$BICEP_SHA256  $BICEP_BINARY" | sha256sum -c -' in block
    version, digest = read_pin(DOCKERFILE)
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/bicep_image_pin.py"), str(DOCKERFILE)],
        check=True,
        capture_output=True,
        text=True,
    )
    assert result.stdout.split() == [version, digest]


@pytest.mark.parametrize("version", ["v0.46.1", "v0.48.1", "v1.2.3"])
def test_pin_reader_uses_the_selected_recipe(tmp_path, version):
    path = tmp_path / "Dockerfile"
    digest = "a" * 64
    path.write_text(f"ARG BICEP_VERSION={version}\nARG BICEP_SHA256={digest}\n")
    assert read_pin(path) == (version, digest)


@pytest.mark.parametrize(
    "text",
    [
        "ARG BICEP_VERSION=v1.2.3\n",
        "ARG BICEP_VERSION=v1.2.3\nARG BICEP_SHA256=bad\n",
        "ARG BICEP_VERSION=$(touch /tmp/untrusted)\nARG BICEP_SHA256=" + "a" * 64,
        "ARG BICEP_VERSION=v1.2.3\nARG BICEP_VERSION=v1.2.4\nARG BICEP_SHA256=" + "a" * 64,
        "ARG BICEP_VERSION=v1.2.3\nARG BICEP_SHA256=" + "a" * 64 + "\nARG BICEP_SHA256=" + "b" * 64,
        "ARG BICEP_VERSION=v1.2.3-rc1\nARG BICEP_SHA256=" + "a" * 64,
    ],
)
def test_pin_reader_refuses_missing_ambiguous_or_executable_values(tmp_path, text):
    path = tmp_path / "Dockerfile"
    path.write_text(text)
    with pytest.raises(ValueError, match="pin"):
        read_pin(path)


@pytest.mark.skipif(sys.platform != "linux", reason="production installer uses Linux Bash")
@pytest.mark.parametrize("mode", ["valid", "bad-digest", "bad-pin"])
def test_production_installer_checks_recipe_pin_before_installing(tmp_path, mode):
    binaries = tmp_path / "bin"
    binaries.mkdir()
    payload = b"compiler bytes"
    digest = hashlib.sha256(payload).hexdigest() if mode == "valid" else "0" * 64
    version = "invalid" if mode == "bad-pin" else "v1.2.3"
    recipe = tmp_path / "images/bicep-sandbox/Dockerfile"
    recipe.parent.mkdir(parents=True)
    recipe.write_text(f"ARG BICEP_VERSION={version}\nARG BICEP_SHA256={digest}\n")
    for name, source in {
        "curl": (
            "from pathlib import Path\nimport os, sys\n"
            'Path(os.environ["DOWNLOAD_LOG"]).write_text(sys.argv[-1])\n'
            f'Path(sys.argv[sys.argv.index("-o") + 1]).write_bytes({payload!r})\n'
        ),
        "sudo": 'from pathlib import Path\nimport os\nPath(os.environ["INSTALL_LOG"]).touch()\n',
        "bicep": 'print("Bicep CLI version 1.2.3")\n',
    }.items():
        path = binaries / name
        path.write_text("#!/usr/bin/env python3\n" + source)
        path.chmod(0o755)
    download = tmp_path / "download.log"
    installed = tmp_path / "installed"
    result = subprocess.run(
        ["bash", "-c", _install_step()],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env=os.environ
        | {
            "PATH": str(binaries) + os.pathsep + os.environ["PATH"],
            "HARNESS": str(ROOT),
            "DOWNLOAD_LOG": str(download),
            "INSTALL_LOG": str(installed),
        },
    )
    assert (result.returncode == 0) == (mode == "valid"), result.stdout + result.stderr
    assert installed.exists() == (mode == "valid")
    if mode == "bad-pin":
        assert not download.exists()
    else:
        assert (
            download.read_text()
            == "https://github.com/Azure/bicep/releases/download/v1.2.3/bicep-linux-x64"
        )
