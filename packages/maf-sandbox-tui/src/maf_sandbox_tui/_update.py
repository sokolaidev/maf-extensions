"""Installer-owned version checks and updates for MST."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from dataclasses import dataclass
from enum import StrEnum
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any, cast
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

DISTRIBUTION_NAME = "maf-sandbox-tui"
_PYPI_JSON_URL = f"https://pypi.org/pypi/{DISTRIBUTION_NAME}/json"
_MANAGER_TIMEOUT = 5.0


class UpdateError(RuntimeError):
    """MST could not check or safely update its installed distribution."""


class InstallationKind(StrEnum):
    """The owner of the environment containing the running MST executable."""

    UV_TOOL = "uv-tool"
    PIPX = "pipx"
    VIRTUAL_ENVIRONMENT = "virtual-environment"
    SYSTEM = "system-python"


@dataclass(frozen=True)
class Installation:
    """An identified installation owner and the command that controls it."""

    kind: InstallationKind
    prefix: Path
    executable: str | None = None
    global_pipx: bool = False

    @property
    def self_updatable(self) -> bool:
        """Return whether MST can delegate an update to this owner."""
        return self.kind in {InstallationKind.UV_TOOL, InstallationKind.PIPX}

    def to_json(self) -> dict[str, object]:
        """Return a stable machine-readable installation description."""
        return {
            "manager": self.kind.value,
            "self_updatable": self.self_updatable,
            "prefix": str(self.prefix),
        }


@dataclass(frozen=True)
class UpdateCheck:
    """The installed and newest versions on one release channel."""

    current: Version
    latest: Version
    installation: Installation
    prereleases: bool

    @property
    def status(self) -> str:
        """Describe the installed version relative to the selected channel."""
        if self.latest > self.current:
            return "update_available"
        if self.latest == self.current:
            return "current"
        return "newer_than_index"

    def to_json(self) -> dict[str, object]:
        """Return the stable JSON shape printed by ``mst update --check``."""
        return {
            "status": self.status,
            "current_version": str(self.current),
            "latest_version": str(self.latest),
            "channel": "prerelease" if self.prereleases else "stable",
            "installation": self.installation.to_json(),
        }


@dataclass(frozen=True)
class UpdateResult:
    """A completed or unnecessary installer-owned update."""

    status: str
    previous: Version
    installed: Version
    installation: Installation

    def to_json(self) -> dict[str, object]:
        """Return the stable JSON shape printed by ``mst update``."""
        return {
            "status": self.status,
            "previous_version": str(self.previous),
            "installed_version": str(self.installed),
            "installation": self.installation.to_json(),
        }


def current_version() -> Version:
    """Read MST's installed distribution version without importing it again."""
    try:
        return Version(version(DISTRIBUTION_NAME))
    except PackageNotFoundError as error:
        raise UpdateError(f"{DISTRIBUTION_NAME} has no installed distribution metadata") from error
    except InvalidVersion as error:
        raise UpdateError(f"the installed MST version is invalid: {error}") from error


def _command_output(command: list[str]) -> str | None:
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            check=False,
            text=True,
            timeout=_MANAGER_TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    output = completed.stdout.strip()
    return output or None


def _owned_environment(prefix: Path, root: Path) -> bool:
    try:
        relative = prefix.resolve().relative_to(root.resolve())
    except (OSError, ValueError):
        return False
    return len(relative.parts) == 1 and canonicalize_name(relative.parts[0]) == canonicalize_name(
        DISTRIBUTION_NAME
    )


def _pipx_root(pipx: str, *, global_install: bool) -> Path | None:
    variable = "PIPX_GLOBAL_HOME" if global_install else "PIPX_HOME"
    command = [pipx, "environment"]
    if global_install:
        command.append("--global")
    command.extend(("--value", variable))
    output = _command_output(command)
    return None if output is None else Path(output) / "venvs"


def inspect_installation(
    *,
    prefix: Path | None = None,
    base_prefix: Path | None = None,
) -> Installation:
    """Identify only managers that can prove they own the running environment."""
    active = Path(sys.prefix) if prefix is None else prefix
    base = Path(sys.base_prefix) if base_prefix is None else base_prefix

    if uv := shutil.which("uv"):
        if output := _command_output([uv, "tool", "dir"]):
            if _owned_environment(active, Path(output)):
                return Installation(InstallationKind.UV_TOOL, active, uv)

    if pipx := shutil.which("pipx"):
        for global_install in (False, True):
            root = _pipx_root(pipx, global_install=global_install)
            if root is not None and _owned_environment(active, root):
                return Installation(
                    InstallationKind.PIPX,
                    active,
                    pipx,
                    global_pipx=global_install,
                )

    kind = (
        InstallationKind.VIRTUAL_ENVIRONMENT
        if active.resolve() != base.resolve()
        else InstallationKind.SYSTEM
    )
    return Installation(kind, active)


def _release_versions(payload: object, *, prereleases: bool) -> tuple[Version, ...]:
    if not isinstance(payload, dict):
        raise UpdateError("PyPI returned an invalid project document")
    document = cast("dict[object, object]", payload)
    raw_releases = document.get("releases")
    if not isinstance(raw_releases, dict):
        raise UpdateError("PyPI returned an invalid project document")
    release_document = cast("dict[object, object]", raw_releases)
    releases: list[Version] = []
    for raw_version, raw_files in release_document.items():
        if not isinstance(raw_version, str) or not isinstance(raw_files, list) or not raw_files:
            continue
        try:
            parsed = Version(raw_version)
        except InvalidVersion:
            continue
        if parsed.is_prerelease and not prereleases:
            continue
        raw_file_items = cast("list[object]", raw_files)
        files = [
            cast("dict[object, object]", item) for item in raw_file_items if isinstance(item, dict)
        ]
        if not files or all(item.get("yanked") is True for item in files):
            continue
        releases.append(parsed)
    if not releases:
        channel = "including prereleases" if prereleases else "on the stable channel"
        raise UpdateError(f"PyPI reports no installable MST releases {channel}")
    return tuple(releases)


def latest_version(*, prereleases: bool = False, timeout: float = 10.0) -> Version:
    """Return the newest non-yanked MST release advertised by PyPI."""
    request = Request(
        _PYPI_JSON_URL,
        headers={"Accept": "application/json", "User-Agent": f"mst/{current_version()}"},
    )
    try:
        with urlopen(request, timeout=timeout) as response:  # noqa: S310 - fixed HTTPS origin
            final_url = response.geturl()
            if not final_url.startswith("https://pypi.org/"):
                raise UpdateError(f"PyPI redirected the version check to {final_url!r}")
            payload: Any = json.load(response)
    except UpdateError:
        raise
    except (HTTPError, URLError, OSError, TimeoutError, json.JSONDecodeError) as error:
        raise UpdateError(f"could not check PyPI for MST updates: {error}") from error
    return max(_release_versions(payload, prereleases=prereleases))


def check_for_update(
    *,
    prereleases: bool = False,
    timeout: float = 10.0,
    installation: Installation | None = None,
) -> UpdateCheck:
    """Compare the installed MST version with the selected PyPI channel."""
    return UpdateCheck(
        current=current_version(),
        latest=latest_version(prereleases=prereleases, timeout=timeout),
        installation=inspect_installation() if installation is None else installation,
        prereleases=prereleases,
    )


def _upgrade_command(installation: Installation, target: Version) -> list[str]:
    executable = installation.executable
    if executable is None:
        raise UpdateError("the installation manager executable is unavailable")
    if installation.kind is InstallationKind.UV_TOOL:
        return [executable, "tool", "install", f"{DISTRIBUTION_NAME}@{target}"]
    if installation.kind is InstallationKind.PIPX:
        command = [executable, "install"]
        if installation.global_pipx:
            command.append("--global")
        command.extend(("--upgrade", f"{DISTRIBUTION_NAME}=={target}"))
        return command
    raise UpdateError("this MST installation cannot update itself")


def _fresh_installed_version() -> Version:
    command = [
        sys.executable,
        "-c",
        (f"from importlib.metadata import version; print(version({DISTRIBUTION_NAME!r}))"),
    ]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            check=False,
            text=True,
            timeout=_MANAGER_TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise UpdateError(f"could not verify the updated MST environment: {error}") from error
    if completed.returncode != 0:
        detail = completed.stderr.strip() or "the environment's Python failed"
        raise UpdateError(f"could not verify the updated MST environment: {detail}")
    try:
        return Version(completed.stdout.strip())
    except InvalidVersion as error:
        raise UpdateError("the updated MST environment reported an invalid version") from error


def _manual_update_message(installation: Installation, target: Version) -> str:
    requirement = f"{DISTRIBUTION_NAME}=={target}"
    if installation.kind is InstallationKind.VIRTUAL_ENVIRONMENT:
        return (
            "MST is running from a project or manually managed virtual environment. "
            f"Update its lock with 'uv lock --upgrade-package {requirement}', then run 'uv sync'."
        )
    return (
        "MST is running from a system Python installation. "
        f"Use '{sys.executable} -m pip install --upgrade {requirement}'."
    )


def perform_update(
    *,
    target: str | None = None,
    prereleases: bool = False,
    timeout: float = 10.0,
    installation: Installation | None = None,
    capture_output: bool = False,
) -> UpdateResult:
    """Delegate an explicit update to the manager that owns this executable."""
    owner = inspect_installation() if installation is None else installation
    previous = current_version()
    if target is None:
        selected = latest_version(prereleases=prereleases, timeout=timeout)
        if selected <= previous:
            return UpdateResult("current", previous, previous, owner)
    else:
        try:
            selected = Version(target)
        except InvalidVersion as error:
            raise UpdateError(f"invalid target version {target!r}: {error}") from error
        if selected == previous:
            return UpdateResult("current", previous, previous, owner)

    if not owner.self_updatable:
        raise UpdateError(_manual_update_message(owner, selected))

    command = _upgrade_command(owner, selected)
    try:
        completed = subprocess.run(
            command,
            capture_output=capture_output,
            check=False,
            text=True,
        )
    except OSError as error:
        raise UpdateError(f"could not start {owner.kind.value}: {error}") from error
    if completed.returncode != 0:
        detail = (completed.stderr or "").strip()
        suffix = f": {detail}" if detail else ""
        raise UpdateError(f"{owner.kind.value} could not update MST{suffix}")

    installed = _fresh_installed_version()
    if installed != selected:
        raise UpdateError(
            f"{owner.kind.value} completed, but MST {installed} is installed instead of {selected}"
        )
    status = "updated" if installed > previous else "downgraded"
    return UpdateResult(status, previous, installed, owner)


__all__ = [
    "DISTRIBUTION_NAME",
    "Installation",
    "InstallationKind",
    "UpdateCheck",
    "UpdateError",
    "UpdateResult",
    "check_for_update",
    "current_version",
    "inspect_installation",
    "latest_version",
    "perform_update",
]
