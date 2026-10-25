"""Keep analyzer identities, verified component pins and release routing consistent."""

import hashlib
import io
import json
import re
import runpy
from pathlib import Path

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


@pytest.mark.parametrize("component", ["bash", "dash"])
@pytest.mark.parametrize("valid_digest", [True, False])
def test_interpreter_install_requires_pinned_bytes_before_offline_installation(
    monkeypatch, tmp_path, component, valid_digest
):
    installer = runpy.run_path(str(CONTEXT / "install.py"))
    payload = b"pinned interpreter archive"
    recipe = {
        component: {
            "version": IDENTITY[component],
            "url": f"https://example.invalid/{component}.apk",
            "sha256": hashlib.sha256(payload if valid_digest else b"other").hexdigest(),
        }
    }
    original_read = Path.read_text
    monkeypatch.setattr(
        Path,
        "read_text",
        lambda path, *args, **kwargs: (
            json.dumps(recipe)
            if path == Path("/recipe/image.json")
            else original_read(path, *args, **kwargs)
        ),
    )
    monkeypatch.setattr(installer["tempfile"], "tempdir", str(tmp_path))
    monkeypatch.setattr(
        installer["urllib"].request, "urlopen", lambda *args, **kwargs: io.BytesIO(payload)
    )
    commands = []

    def execute(command, *, check):
        assert check is True
        cache = Path(command[2])
        cached = cache / f"{component}-{IDENTITY[component]}.01234567.apk"
        if "download" in command:
            cached.write_bytes(payload)
        if "add" in command:
            assert "--no-network" in command
            assert cached.read_bytes() == payload
        assert "--allow-untrusted" not in command
        commands.append(command)

    monkeypatch.setattr(installer["subprocess"], "run", execute)
    if valid_digest:
        installer["install"](component, tmp_path / "destination")
        assert len(commands) == 3
        assert commands[-1][-3:] == ["--no-network", "add", f"{component}={IDENTITY[component]}"]
    else:
        with pytest.raises(ValueError, match="component digest mismatch"):
            installer["install"](component, tmp_path / "destination")
        assert commands == []


@pytest.mark.parametrize("component", ["bash", "dash"])
@pytest.mark.parametrize("cached", [b"pinned archive", b"substituted archive", None])
def test_apk_install_refuses_unverified_cached_bytes(monkeypatch, component, cached):
    installer = runpy.run_path(str(CONTEXT / "install.py"))
    installed = []

    def apk(command, *, check):
        cache = Path(command[2])
        archive = cache / f"{component}-{IDENTITY[component]}.01234567.apk"
        if "download" in command and cached is not None:
            archive.write_bytes(cached)
        if "add" in command:
            assert "--no-network" in command
            installed.append(archive.read_bytes())

    monkeypatch.setattr(installer["subprocess"], "run", apk)
    digest = hashlib.sha256(b"pinned archive").hexdigest()
    if cached == b"pinned archive":
        installer["install_apk"](component, IDENTITY[component], digest)
        assert installed == [b"pinned archive"]
    else:
        with pytest.raises(ValueError, match="cached component digest mismatch"):
            installer["install_apk"](component, IDENTITY[component], digest)
        assert installed == []


@pytest.mark.parametrize("interpreter", ["bash", "dash"])
@pytest.mark.parametrize("failure", [None, "version", "missing", "stderr"])
def test_launcher_requires_exact_installed_interpreter_identity(interpreter, failure):
    launcher = runpy.run_path(str(CONTEXT / "runner.py"))

    class Supervisor:
        def execute(self, command, payload=None):
            if command[0] == "/usr/bin/apk":
                name = command[-1]
                assert command == ["/usr/bin/apk", "info", "--installed", "-v", name]
                result = {"exit_code": 0, "stdout": f"{name}-{IDENTITY[name]}\n", "stderr": ""}
                if name == interpreter:
                    if failure == "version":
                        result["stdout"] = f"{name}-0.0.0\n"
                    elif failure == "missing":
                        result["exit_code"] = 1
                    elif failure == "stderr":
                        result["stderr"] = "package database unavailable"
                return result
            assert command == ["/usr/local/bin/maf-shell-audit"]
            return {
                "exit_code": 0,
                "stdout": json.dumps({"protocol": 1, "files": []}),
                "stderr": "",
            }

    request = {"flavor": "bash", "files": []}
    if failure is None:
        assert launcher["analyze"](request, Supervisor()) == []
    else:
        with pytest.raises(ValueError, match="interpreter identity mismatch"):
            launcher["analyze"](request, Supervisor())
