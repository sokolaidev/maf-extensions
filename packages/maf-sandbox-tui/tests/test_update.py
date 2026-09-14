"""MST version checks and installer-owned self-updates."""

from __future__ import annotations

import json
import shutil
import subprocess
from importlib.metadata import PackageNotFoundError
from io import BytesIO
from pathlib import Path
from urllib.error import URLError

import pytest
from packaging.version import Version

import maf_sandbox_tui._update as update_module
import maf_sandbox_tui.cli as cli_module

Installation = update_module.Installation
InstallationKind = update_module.InstallationKind
UpdateCheck = update_module.UpdateCheck
UpdateError = update_module.UpdateError
UpdateResult = update_module.UpdateResult


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
        lambda command, *, timeout: (
            str(tool_root) if command == ["uv", "tool", "dir"] and timeout == 0.25 else None
        ),
    )

    installation = update_module.inspect_installation(
        prefix=prefix,
        base_prefix=tmp_path / "python",
        timeout=0.25,
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
        lambda _pipx, *, global_install, timeout: (
            (tmp_path / "global" / "venvs" if global_install else tmp_path / "local" / "venvs")
            if timeout == 0.5
            else None
        ),
    )

    installation = update_module.inspect_installation(
        prefix=prefix,
        base_prefix=tmp_path / "python",
        timeout=0.5,
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

    with pytest.raises(UpdateError, match="environment's package manager") as captured:
        update_module.perform_update(installation=installation)
    assert "maf-sandbox-tui==0.2.0" in str(captured.value)
    assert "uv " not in str(captured.value)


def test_uv_update_delegates_an_exact_release_and_verifies_it(monkeypatch, tmp_path):
    installation = Installation(InstallationKind.UV_TOOL, tmp_path, "uv")
    commands: list[list[str]] = []
    timeouts: list[float] = []
    verification_timeouts: list[float] = []
    monkeypatch.setattr(update_module, "current_version", lambda: Version("0.1.0"))
    monkeypatch.setattr(
        update_module,
        "latest_version",
        lambda **_kwargs: Version("0.2.0"),
    )

    def fresh(*, timeout):
        verification_timeouts.append(timeout)
        return Version("0.2.0")

    monkeypatch.setattr(update_module, "_fresh_installed_version", fresh)

    def run(command, **_kwargs):
        commands.append(command)
        timeouts.append(_kwargs["timeout"])
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(subprocess, "run", run)

    result = update_module.perform_update(
        installation=installation,
        capture_output=True,
        timeout=0.25,
    )

    assert commands == [["uv", "tool", "install", "maf-sandbox-tui@0.2.0"]]
    assert timeouts == [0.25]
    assert verification_timeouts == [0.25]
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
    monkeypatch.setattr(
        update_module,
        "_fresh_installed_version",
        lambda *, timeout: Version("0.1.0") if timeout == 10.0 else Version("0.0.0"),
    )

    def run(command, **_kwargs):
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(subprocess, "run", run)

    result = update_module.perform_update(target="0.1.0", installation=installation)

    assert commands == [["pipx", "install", "--global", "--upgrade", "maf-sandbox-tui==0.1.0"]]
    assert result.status == "downgraded"


def test_update_reports_a_bounded_package_manager_timeout(monkeypatch, tmp_path):
    installation = Installation(InstallationKind.UV_TOOL, tmp_path, "uv")
    monkeypatch.setattr(update_module, "current_version", lambda: Version("0.1.0"))

    def run(command, **kwargs):
        assert kwargs["timeout"] == 0.25
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr(subprocess, "run", run)

    with pytest.raises(UpdateError, match="uv-tool timed out after 0.25 seconds"):
        update_module.perform_update(
            target="0.2.0",
            timeout=0.25,
            installation=installation,
        )


def test_update_propagates_timeout_to_the_installation_probe(monkeypatch, tmp_path):
    installation = Installation(InstallationKind.UV_TOOL, tmp_path, "uv")
    received: list[float] = []
    monkeypatch.setattr(update_module, "current_version", lambda: Version("0.2.0"))

    def inspect(*, timeout):
        received.append(timeout)
        return installation

    monkeypatch.setattr(update_module, "inspect_installation", inspect)

    result = update_module.perform_update(target="0.2.0", timeout=0.25)

    assert result.status == "current"
    assert received == [0.25]


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


@pytest.mark.parametrize(
    ("current", "latest", "expected"),
    [
        ("0.1.0", "0.2.0", "is available"),
        ("0.2.0", "0.2.0", "is current"),
        ("0.3.0", "0.2.0", "is newer than"),
    ],
)
def test_update_check_plain_output_covers_every_status(
    current, latest, expected, monkeypatch, capsys, tmp_path
):
    checked = UpdateCheck(
        Version(current),
        Version(latest),
        Installation(InstallationKind.UV_TOOL, tmp_path, "uv"),
        prereleases=False,
    )
    received: list[dict[str, object]] = []

    def check(**kwargs):
        received.append(kwargs)
        return checked

    monkeypatch.setattr(cli_module, "check_for_update", check)

    cli_module.main(["update", "--check", "--timeout", "0.25"])

    assert expected in capsys.readouterr().out
    assert received == [{"prereleases": False, "timeout": 0.25}]


@pytest.mark.parametrize(
    ("result_status", "expected"),
    [
        ("current", "already current"),
        ("updated", "updated from"),
        ("downgraded", "downgraded from"),
    ],
)
def test_update_plain_output_and_default_options(
    result_status, expected, monkeypatch, capsys, tmp_path
):
    installed = Version("0.2.0" if result_status != "downgraded" else "0.1.0")
    result = UpdateResult(
        result_status,
        Version("0.1.0" if result_status != "downgraded" else "0.2.0"),
        installed,
        Installation(InstallationKind.UV_TOOL, tmp_path, "uv"),
    )
    received: list[dict[str, object]] = []

    def perform(**kwargs):
        received.append(kwargs)
        return result

    monkeypatch.setattr(cli_module, "perform_update", perform)

    cli_module.main(["update", "--prerelease", "--timeout", "0.5"])

    assert expected in capsys.readouterr().out
    assert received == [
        {
            "target": None,
            "prereleases": True,
            "timeout": 0.5,
            "capture_output": False,
        }
    ]


def test_invalid_target_version_is_reported_by_the_cli(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(update_module, "current_version", lambda: Version("0.1.0"))
    monkeypatch.setattr(
        update_module,
        "inspect_installation",
        lambda *, timeout: Installation(InstallationKind.UV_TOOL, tmp_path, "uv"),
    )

    with pytest.raises(SystemExit) as raised:
        cli_module.main(["update", "--to", "not a version"])

    assert raised.value.code == 1
    assert "invalid target version" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("reported", "expected"),
    [
        (PackageNotFoundError("missing"), "no installed distribution metadata"),
        ("not a version", "installed MST version is invalid"),
    ],
)
def test_current_version_reports_missing_or_invalid_metadata(reported, expected, monkeypatch):
    def distribution_version(_name):
        if isinstance(reported, BaseException):
            raise reported
        return reported

    monkeypatch.setattr("maf_sandbox_tui._update.version", distribution_version)

    with pytest.raises(UpdateError, match=expected):
        update_module.current_version()


@pytest.mark.parametrize(
    "outcome",
    [
        subprocess.CompletedProcess(["manager"], 1, "ignored", "failed"),
        subprocess.CompletedProcess(["manager"], 0, "", ""),
        OSError("cannot start"),
        subprocess.TimeoutExpired(["manager"], 5),
    ],
)
def test_command_output_returns_none_for_every_unusable_result(outcome, monkeypatch):
    def run(*_args, **_kwargs):
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr(subprocess, "run", run)
    assert update_module._command_output(["manager"]) is None


@pytest.mark.parametrize(
    ("global_install", "variable", "prefix"),
    [(False, "PIPX_HOME", []), (True, "PIPX_GLOBAL_HOME", ["--global"])],
)
def test_pipx_root_reads_each_manager_environment(global_install, variable, prefix, monkeypatch):
    commands: list[list[str]] = []

    def output(command, *, timeout):
        commands.append(command)
        assert timeout == 0.25
        return "C:/pipx"

    monkeypatch.setattr(update_module, "_command_output", output)

    assert update_module._pipx_root("pipx", global_install=global_install, timeout=0.25) == Path(
        "C:/pipx/venvs"
    )
    assert commands == [["pipx", "environment", *prefix, "--value", variable]]


def test_inspection_falls_back_to_system_python_without_a_manager(monkeypatch, tmp_path):
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    installation = update_module.inspect_installation(prefix=tmp_path, base_prefix=tmp_path)
    assert installation.kind is InstallationKind.SYSTEM
    assert installation.self_updatable is False


@pytest.mark.parametrize("payload", [None, {}, {"releases": []}])
def test_release_selection_rejects_invalid_project_documents(payload):
    with pytest.raises(UpdateError, match="invalid project document"):
        update_module._release_versions(payload, prereleases=False)


def test_release_selection_reports_an_empty_channel():
    payload = {"releases": {"1.0.0rc1": [{"yanked": False}], 2: [{"yanked": False}]}}
    with pytest.raises(UpdateError, match="stable channel"):
        update_module._release_versions(payload, prereleases=False)


class _PyPIResponse(BytesIO):
    def __init__(self, payload: object, url: str = "https://pypi.org/pypi/maf-sandbox-tui/json"):
        super().__init__(json.dumps(payload).encode())
        self._url = url

    def geturl(self) -> str:
        return self._url

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


def test_latest_version_reads_only_the_fixed_pypi_origin(monkeypatch):
    seen: list[tuple[str, float, str | None]] = []
    payload = {"releases": {"0.1.0": [{"yanked": False}], "0.2.0": [{"yanked": False}]}}

    def open_request(request, *, timeout):
        seen.append((request.full_url, timeout, request.get_header("User-agent")))
        return _PyPIResponse(payload)

    monkeypatch.setattr("maf_sandbox_tui._update.urlopen", open_request)
    monkeypatch.setattr(update_module, "current_version", lambda: Version("0.1.0"))

    assert update_module.latest_version(timeout=0.25) == Version("0.2.0")
    assert seen == [("https://pypi.org/pypi/maf-sandbox-tui/json", 0.25, "mst/0.1.0")]


def test_latest_version_refuses_a_redirect_away_from_pypi(monkeypatch):
    monkeypatch.setattr(
        "maf_sandbox_tui._update.urlopen",
        lambda *_args, **_kwargs: _PyPIResponse(
            {"releases": {}},
            "https://example.com/project.json",
        ),
    )

    with pytest.raises(UpdateError, match="redirected"):
        update_module.latest_version()


def test_latest_version_wraps_transport_and_json_failures(monkeypatch):
    def fail(*_args, **_kwargs):
        raise URLError("offline")

    monkeypatch.setattr("maf_sandbox_tui._update.urlopen", fail)

    with pytest.raises(UpdateError, match="could not check PyPI"):
        update_module.latest_version()


def test_check_for_update_discovers_the_installation(monkeypatch, tmp_path):
    installation = Installation(InstallationKind.SYSTEM, tmp_path)
    monkeypatch.setattr(update_module, "current_version", lambda: Version("0.1.0"))
    monkeypatch.setattr(update_module, "latest_version", lambda **_kwargs: Version("0.2.0"))
    received: list[float] = []

    def inspect(*, timeout):
        received.append(timeout)
        return installation

    monkeypatch.setattr(update_module, "inspect_installation", inspect)

    checked = update_module.check_for_update(prereleases=True, timeout=0.1)

    assert checked.status == "update_available"
    assert checked.installation is installation
    assert checked.to_json()["channel"] == "prerelease"
    assert received == [0.1]


def test_upgrade_command_requires_a_supported_available_manager(tmp_path):
    with pytest.raises(UpdateError, match="executable is unavailable"):
        update_module._upgrade_command(
            Installation(InstallationKind.UV_TOOL, tmp_path),
            Version("1.0.0"),
        )
    with pytest.raises(UpdateError, match="cannot update itself"):
        update_module._upgrade_command(
            Installation(InstallationKind.SYSTEM, tmp_path, "python"),
            Version("1.0.0"),
        )


@pytest.mark.parametrize(
    ("outcome", "expected"),
    [
        (OSError("cannot start"), "could not verify"),
        (subprocess.CompletedProcess(["python"], 1, "", "broken"), "broken"),
        (subprocess.CompletedProcess(["python"], 1, "", ""), "Python failed"),
        (subprocess.CompletedProcess(["python"], 0, "bad version", ""), "invalid version"),
    ],
)
def test_fresh_version_reports_every_verification_failure(outcome, expected, monkeypatch):
    def run(*_args, **_kwargs):
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr(subprocess, "run", run)

    with pytest.raises(UpdateError, match=expected):
        update_module._fresh_installed_version()


def test_fresh_version_reads_the_updated_environment(monkeypatch):
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(["python"], 0, "1.2.3\n", ""),
    )
    assert update_module._fresh_installed_version() == Version("1.2.3")


def test_system_python_update_refusal_names_pip(monkeypatch, tmp_path):
    monkeypatch.setattr(update_module, "current_version", lambda: Version("0.1.0"))
    installation = Installation(InstallationKind.SYSTEM, tmp_path)
    with pytest.raises(UpdateError, match="-m pip install --upgrade"):
        update_module.perform_update(target="0.2.0", installation=installation)


def test_update_without_target_is_current_when_the_index_is_not_newer(monkeypatch, tmp_path):
    monkeypatch.setattr(update_module, "current_version", lambda: Version("0.2.0"))
    monkeypatch.setattr(update_module, "latest_version", lambda **_kwargs: Version("0.1.0"))
    installation = Installation(InstallationKind.UV_TOOL, tmp_path, "uv")

    result = update_module.perform_update(installation=installation)

    assert result.status == "current"
    assert result.installed == Version("0.2.0")


def test_explicit_current_target_does_not_run_the_manager(monkeypatch, tmp_path):
    monkeypatch.setattr(update_module, "current_version", lambda: Version("0.2.0"))
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("manager must not run"),
    )
    installation = Installation(InstallationKind.UV_TOOL, tmp_path, "uv")

    assert (
        update_module.perform_update(target="0.2.0", installation=installation).status == "current"
    )


@pytest.mark.parametrize(
    ("outcome", "expected"),
    [
        (OSError("cannot start"), "could not start"),
        (subprocess.CompletedProcess(["uv"], 1, "", "resolver failed"), "resolver failed"),
        (subprocess.CompletedProcess(["uv"], 1, "", ""), "could not update MST"),
    ],
)
def test_update_reports_manager_start_and_exit_failures(outcome, expected, monkeypatch, tmp_path):
    monkeypatch.setattr(update_module, "current_version", lambda: Version("0.1.0"))

    def run(*_args, **_kwargs):
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr(subprocess, "run", run)
    installation = Installation(InstallationKind.UV_TOOL, tmp_path, "uv")

    with pytest.raises(UpdateError, match=expected):
        update_module.perform_update(target="0.2.0", installation=installation)


def test_update_rejects_a_manager_version_mismatch(monkeypatch, tmp_path):
    monkeypatch.setattr(update_module, "current_version", lambda: Version("0.1.0"))
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda command, **_kwargs: subprocess.CompletedProcess(command, 0, "", ""),
    )
    monkeypatch.setattr(
        update_module,
        "_fresh_installed_version",
        lambda *, timeout: Version("0.3.0") if timeout == 10.0 else Version("0.0.0"),
    )
    installation = Installation(InstallationKind.UV_TOOL, tmp_path, "uv")

    with pytest.raises(UpdateError, match="0.3.0 is installed instead of 0.2.0"):
        update_module.perform_update(target="0.2.0", installation=installation)
