"""Keep analyzer identities, verified component pins and release routing consistent."""

import hashlib
import json
import re
import runpy
from pathlib import Path
from types import SimpleNamespace

import pytest
from maf_sandbox_shell._report import IDENTITY

ROOT = Path(__file__).resolve().parents[1]
CONTEXT = ROOT / "images/shell-sandbox"
affected = runpy.run_path(str(ROOT / "scripts/select_image_security.py"))["affected"]


def test_recipe_and_kind_agree_on_analyzer_identities():
    recipe = json.loads((CONTEXT / "image.json").read_text())
    launcher = runpy.run_path(str(CONTEXT / "runner.py"))
    assert launcher["IDENTITY"] == IDENTITY
    for name in ("bash", "dash", "shellcheck", "powershell", "psscriptanalyzer"):
        assert recipe[name]["version"] == IDENTITY[name]
        assert re.fullmatch(r"[a-f0-9]{64}", recipe[name]["sha256"])
        assert recipe[name]["url"].startswith("https://")
    assert recipe["baseImage"] in (CONTEXT / "Dockerfile").read_text()


def test_shell_sources_select_only_the_shell_image():
    assert affected("images/shell-sandbox/runner.py") == ("shell",)
    assert affected("images/shell-sandbox/README.md") == ()


@pytest.mark.parametrize("cached", [b"pinned archive", b"substituted archive", None])
def test_apk_install_refuses_unverified_cached_bytes(monkeypatch, cached):
    installer = runpy.run_path(str(CONTEXT / "install.py"))
    installed = []

    def apk(command, *, check):
        cache = Path(command[2])
        if "download" in command and cached is not None:
            (cache / "bash-5.3-r14.089044a4.apk").write_bytes(cached)
        if "add" in command:
            assert "--no-network" in command
            installed.append((cache / "bash-5.3-r14.089044a4.apk").read_bytes())

    monkeypatch.setattr(installer["subprocess"], "run", apk)
    digest = hashlib.sha256(b"pinned archive").hexdigest()
    if cached == b"pinned archive":
        installer["install_apk"]("bash", "5.3-r14", digest)
        assert installed == [b"pinned archive"]
    else:
        with pytest.raises(ValueError, match="cached component digest mismatch"):
            installer["install_apk"]("bash", "5.3-r14", digest)
        assert installed == []


@pytest.mark.parametrize("fault", [None, "version", "missing", "stderr"])
def test_interpreter_identity_is_verified_before_analysis(fault):
    launcher = runpy.run_path(str(CONTEXT / "runner.py"))
    calls = []

    def execute(command, data=b""):
        calls.append(command)
        if command[0] == "/sbin/apk":
            name = command[-1]
            return {
                "exit_code": 1 if fault == "missing" else 0,
                "stdout": f"{name}-{IDENTITY[name] if fault != 'version' else '0.0-r0'}\n",
                "stderr": "warning" if fault == "stderr" else "",
            }
        return {"exit_code": 0, "stdout": '{"protocol": 1, "files": []}', "stderr": ""}

    request = {"flavor": "bash", "files": []}
    supervisor = SimpleNamespace(execute=execute)
    if fault is not None:
        with pytest.raises(ValueError, match="interpreter identity mismatch"):
            launcher["analyze"](request, supervisor)
        assert len(calls) == 1
    else:
        assert launcher["analyze"](request, supervisor) == []
        assert [command[-1] for command in calls[:2]] == ["bash", "dash"]
        assert calls[-1] == ["/usr/local/bin/maf-shell-audit"]
