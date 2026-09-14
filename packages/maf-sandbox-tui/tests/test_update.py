"""MST version checks and installer-owned self-updates."""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest
from packaging.version import Version

import maf_sandbox_tui._update as update_module
import maf_sandbox_tui.cli as cli_module
from maf_sandbox_tui._update import (
    Installation,
    InstallationKind,
    UpdateCheck,
    UpdateError,
    UpdateResult,
)


def test_release_selection_excludes_prereleases_and_fully_yanked_versions():
    payload = {
        "releases": {
            "0.1.0": [{"yanked": False}],
            "0.2.0rc1": [{"yanked": False}],
            "0.3.0": [{"yanked": True}],
            "not-a-version": [{"yanked": False}],
            "0.4.0": [],
        }
    }

    assert update_module._release_versions(payload, prereleases=False) == (Version("0.1.0"),)
    assert update_module._release_versions(payload, prereleases=True) == (
        Version("0.1.0"),
        Version("0.2.0rc1"),
    )


def test_inspection_recognizes_only_the_matching_uv_tool_environment(monkeypatch, tmp_path):
    tool_root = tmp_path / "tools"
    prefix = tool_root / "maf-sandbox-tui"
    monkeypatch.setattr(shutil, "which", lambda name: "uv" if name == "uv" else None)
    monkeypatch.setattr(
        update_module,
        "_command_output",
        lambda command: str(tool_root) if command == ["uv", "tool", "dir"] else None,
    )

    installation = update_module.inspect_installation(
        prefix=prefix,
        base_prefix=tmp_path / "python",
    )

    assert installation == Installation(InstallationKind.UV_TOOL, prefix, "uv")


def test_inspection_recognizes_a_global_pipx_environment(monkeypatch, tmp_path):
    prefix = tmp_path / "global" / "venvs" / "maf-sandbox-tui"
    monkeypatch.setattr(
        shutil,
        "which",
        lambda name: "pipx" if name == "pipx" else None,
    )
    monkeypatch.setattr(
        update_module,
        "_pipx_root",
        lambda _pipx, *, global_install: (
            tmp_path / "global" / "venvs" if global_install else tmp_path / "local" / "venvs"
        ),
    )

    installation = update_module.inspect_installation(
        prefix=prefix,
        base_prefix=tmp_path / "python",
    )

    assert installation.kind is InstallationKind.PIPX
    assert installation.executable == "pipx"
    assert installation.global_pipx is True


def test_update_refuses_to_mutate_a_project_environment(monkeypatch, tmp_path):
    installation = Installation(InstallationKind.VIRTUAL_ENVIRONMENT, tmp_path / ".venv")
    monkeypatch.setattr(update_module, "current_version", lambda: Version("0.1.0"))
    monkeypatch.setattr(
        update_module,
        "latest_version",
        lambda **_kwargs: Version("0.2.0"),
    )

    with pytest.raises(UpdateError, match="uv lock --upgrade-package"):
        update_module.perform_update(installation=installation)


def test_uv_update_delegates_an_exact_release_and_verifies_it(monkeypatch, tmp_path):
    installation = Installation(InstallationKind.UV_TOOL, tmp_path, "uv")
    commands: list[list[str]] = []
    monkeypatch.setattr(update_module, "current_version", lambda: Version("0.1.0"))
    monkeypatch.setattr(
        update_module,
        "latest_version",
        lambda **_kwargs: Version("0.2.0"),
    )
    monkeypatch.setattr(update_module, "_fresh_installed_version", lambda: Version("0.2.0"))

    def run(command, **_kwargs):
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(subprocess, "run", run)

    result = update_module.perform_update(installation=installation, capture_output=True)

    assert commands == [["uv", "tool", "install", "maf-sandbox-tui@0.2.0"]]
    assert result.status == "updated"
    assert result.installed == Version("0.2.0")


def test_pipx_can_roll_back_to_an_explicit_version(monkeypatch, tmp_path):
    installation = Installation(
        InstallationKind.PIPX,
        tmp_path,
        "pipx",
        global_pipx=True,
    )
    commands: list[list[str]] = []
    monkeypatch.setattr(update_module, "current_version", lambda: Version("0.2.0"))
    monkeypatch.setattr(update_module, "_fresh_installed_version", lambda: Version("0.1.0"))

    def run(command, **_kwargs):
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(subprocess, "run", run)

    result = update_module.perform_update(target="0.1.0", installation=installation)

    assert commands == [["pipx", "install", "--global", "--upgrade", "maf-sandbox-tui==0.1.0"]]
    assert result.status == "downgraded"


def test_version_command_reports_why_workspace_installation_cannot_self_update(
    monkeypatch, capsys, tmp_path
):
    installation = Installation(InstallationKind.VIRTUAL_ENVIRONMENT, tmp_path / ".venv")
    monkeypatch.setattr(cli_module, "current_version", lambda: Version("0.1.0"))
    monkeypatch.setattr(cli_module, "inspect_installation", lambda: installation)

    cli_module.main(["version", "--json"])

    payload = json.loads(capsys.readouterr().out)
    assert payload["version"] == "0.1.0"
    assert payload["installation"]["manager"] == "virtual-environment"
    assert payload["installation"]["self_updatable"] is False


def test_update_check_is_read_only_and_prints_the_selected_channel(monkeypatch, capsys, tmp_path):
    installation = Installation(InstallationKind.UV_TOOL, tmp_path, "uv")
    checked = UpdateCheck(
        Version("0.1.0"),
        Version("0.2.0rc1"),
        installation,
        prereleases=True,
    )
    monkeypatch.setattr(cli_module, "check_for_update", lambda **_kwargs: checked)

    cli_module.main(["update", "--check", "--prerelease", "--json"])

    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "update_available"
    assert payload["latest_version"] == "0.2.0rc1"
    assert payload["channel"] == "prerelease"


def test_update_command_does_not_probe_sandbox_hosts(monkeypatch, capsys, tmp_path):
    installation = Installation(InstallationKind.UV_TOOL, tmp_path, "uv")
    result = UpdateResult(
        "current",
        Version("0.2.0"),
        Version("0.2.0"),
        installation,
    )
    monkeypatch.setattr(cli_module, "perform_update", lambda **_kwargs: result)
    monkeypatch.setattr(
        cli_module,
        "read_manifests",
        lambda: pytest.fail("version management must not inspect sandbox discovery"),
    )

    cli_module.main(["update", "--to", "0.2.0", "--json"])

    assert json.loads(capsys.readouterr().out)["status"] == "current"
