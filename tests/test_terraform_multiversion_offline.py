"""Opt-in offline validation and lock verification with two AzureRM provider lines."""

from __future__ import annotations

import json
import os
import subprocess

import pytest

IMAGE = os.environ.get("MAF_OPENTOFU_MULTIVERSION_IMAGE", "")
pytestmark = pytest.mark.skipif(not IMAGE, reason="needs MAF_OPENTOFU_MULTIVERSION_IMAGE")

PROBE = r"""
import importlib.util
import json
import os
import pathlib
import sys
import tempfile

line, lock_mode = sys.argv[1:]
install = pathlib.Path("/opt/maf-terraform")
receipt = json.loads((install / "dependencies.json").read_text())
source = "registry.opentofu.org/hashicorp/azurerm"
providers = [item for item in receipt["providers"] if item["source"] == source]
assert {item["version"].split(".")[0] for item in providers} == {"4", "5"}
selected = max(
    (item for item in providers if item["version"].startswith(line + ".")),
    key=lambda item: tuple(map(int, item["version"].split("."))),
)
constraint = f"~> {line}.0" if lock_mode == "none" else ">= 4.0.0, < 6.0.0"
with tempfile.TemporaryDirectory() as temporary:
    os.chdir(temporary)
    project = pathlib.Path("project")
    project.mkdir()
    main = project / "main.tf"
    main.write_text(
        "terraform {\n  required_providers {\n"
        f'    azurerm = {{ source = "{source}", version = "{constraint}" }}\n'
        "  }\n}\n"
        'provider "azurerm" {\n  features {}\n}\n'
        'resource "azurerm_resource_group" "example" {\n'
        '  name = "example"\n  location = "westeurope"\n}\n'
    )
    original = main.read_bytes()
    lock = project / ".terraform.lock.hcl"
    if lock_mode != "none":
        other = next(item for item in providers if item["version"] != selected["version"])
        digest = selected["h1"] if lock_mode == "correct" else other["h1"]
        lock.write_text(
            f'provider "{source}" {{\n'
            f'  version = "{selected["version"]}"\n'
            f'  constraints = "{constraint}"\n'
            f'  hashes = ["{digest}"]\n}}\n'
        )
    original_lock = lock.read_bytes() if lock.exists() else None
    spec = importlib.util.spec_from_file_location("runner", install / "runner.py")
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    result = runner.execute(receipt["engine"], ".", 120)
    assert result["error"] is None, result
    assert main.read_bytes() == original
    if original_lock is not None:
        assert lock.read_bytes() == original_lock
    if lock_mode == "wrong":
        assert result["phases"]["init"]["exit_code"] != 0, result
        assert "checksum" in result["phases"]["init"]["stderr"].lower(), result
        assert "validate" not in result["phases"], result
    else:
        assert result["phases"]["init"]["exit_code"] == 0, result
        validation = result["phases"]["validate"]
        assert validation["exit_code"] == 0, result
        assert json.loads(validation["stdout"])["valid"] is True, result
        assert f'"{selected["version"]}"' in lock.read_text(), lock.read_text()
        installed = list(pathlib.Path(".runner/data/providers").rglob("linux_amd64"))
        assert len(installed) == 1 and installed[0].parent.name == selected["version"], installed
        assert installed[0].is_symlink(), installed
    print(json.dumps({"version": selected["version"], "lock": lock_mode, "result": result}))
"""


@pytest.mark.parametrize("line", ["4", "5"])
@pytest.mark.parametrize("lock_mode", ["none", "correct", "wrong"])
def test_provider_lines_and_lock_hashes_offline(line, lock_mode):
    completed = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "-i",
            "--network=none",
            IMAGE,
            "python3",
            "-I",
            "-",
            line,
            lock_mode,
        ],
        input=PROBE,
        text=True,
        capture_output=True,
        timeout=150,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    evidence = json.loads(completed.stdout)
    assert evidence["version"].startswith(line + ".")
    assert evidence["lock"] == lock_mode
