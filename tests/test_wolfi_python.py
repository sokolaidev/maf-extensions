"""The Python security replacement must remain part of every consumer's evidence."""

from __future__ import annotations

import runpy
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import build_wolfi_python as builder  # noqa: E402
from container_image_inputs import build_inputs  # noqa: E402
from select_image_security import PYTHON_PROFILES, affected  # noqa: E402

pytestmark = pytest.mark.workflow


@pytest.mark.parametrize("name", ["Dockerfile", "configure.py", "build.sh"])
def test_shared_python_inputs_are_retained_and_select_every_consumer(name):
    path = "images/wolfi-python/" + name
    assert ROOT / path in build_inputs(ROOT)
    assert set(affected(path)) == set(PYTHON_PROFILES)
    assert len(PYTHON_PROFILES) == 9
    assert "bicep-docker-sbx" not in affected(path)
    assert "graphviz" not in affected(path)


def test_wrapper_changes_require_new_consumer_evidence():
    path = "scripts/build_wolfi_python.py"
    assert ROOT / path in build_inputs(ROOT)
    assert set(affected(path)) == set(PYTHON_PROFILES)


@pytest.mark.parametrize("failure", [None, "create", "start", "cp"])
def test_package_build_failure_prevents_consumer_build_and_cleans_container(
    tmp_path, monkeypatch, failure
):
    commands = []

    def execute(command, *, check):
        commands.append(command)
        if command[1] == "build" and "--iidfile" in command:
            Path(command[command.index("--iidfile") + 1]).write_text("sha256:" + "a" * 64)
        if command[1] == failure:
            raise subprocess.CalledProcessError(1, command)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(builder.subprocess, "run", execute)
    command = ["docker", "build", "--tag", "consumer:test", "images/drawio-sandbox"]
    if failure:
        with pytest.raises(subprocess.CalledProcessError):
            builder.build_image(command)
        assert not any("consumer:test" in call for call in commands)
    else:
        builder.build_image(command)
        consumer = commands[-1]
        assert consumer[:3] == ["docker", "build", "--build-context"]
        assert consumer[3].startswith("python-packages=")
        assert consumer[4:] == command[2:]
    created = next(call for call in commands if call[1] == "create")
    name = created[created.index("--name") + 1]
    assert ["docker", "rm", "--force", name] in commands
    assert "--privileged" in created
    assert "--mount" not in created
    assert "--volume" not in created


def test_existing_output_is_never_overwritten(tmp_path, monkeypatch):
    (tmp_path / "keep").write_text("unrelated")
    monkeypatch.setattr(builder.subprocess, "run", lambda *a, **kw: pytest.fail("executed Docker"))
    with pytest.raises(ValueError, match="empty"):
        builder.build_packages(tmp_path)


@pytest.mark.parametrize("old_version", ["3.13.17_git20261023", "3.13.19"])
def test_recipe_transform_preserves_abi_patches_and_tests(tmp_path, old_version):
    suffix = """  - name: Support OpenSSL 4 ABI
    uses: patch
    with:
      patches: compatibility.patch
  - uses: upstream-tests/python-regrtest
"""
    recipe = tmp_path / "python-3.13.yaml"
    recipe.write_text(
        f'package:\n  version: "{old_version}"\n'
        "  epoch: 1 # run upstream regrtest subset via shared pipeline\n"
        "pipeline:\n  - uses: git-checkout\n    with:\n      branch: 3.13\n" + suffix
    )
    configure = runpy.run_path(str(ROOT / "images/wolfi-python/configure.py"))["configure"]
    if old_version != "3.13.17_git20261023":
        with pytest.raises(ValueError, match="Unexpected Wolfi recipe"):
            configure(tmp_path)
        return
    configure(tmp_path)
    result = recipe.read_text()
    assert result.endswith(suffix)
    assert 'version: "3.13.18"' in result
    assert "epoch: 0" in result
    assert "tag: v3.13.18" in result
    assert "expected-commit: 0d4bd512fc429e3d4eacdd113c975e161f21644d" in result
    assert "branch: 3.13" not in result
