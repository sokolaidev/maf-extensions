"""Configuration for the Docker Sandboxes backend.

A plain frozen dataclass, like the other backends' configs.  The image, the storage base and
the egress mode travel in a :class:`~maf_sandbox.SandboxSpec`, not here.
"""

from __future__ import annotations

import math
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

__all__ = ["SbxSandboxConfig", "default_workspace_root"]

_PREFIX = re.compile(r"[a-z0-9]{1,16}")
_MEMORY = re.compile(r"[1-9][0-9]*[mMgG]")


def default_workspace_root() -> Path:
    """A per-user state directory, never the temporary directory a host may clean."""
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "maf-sandbox-docker-sbx" / "workspaces"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "maf-sandbox-docker-sbx"
    state = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(state) / "maf-sandbox-docker-sbx" / "workspaces"


@dataclass(frozen=True)
class SbxSandboxConfig:
    """Where ``sbx`` is, where workspaces live, and what each sandbox may use.

    ``workspace_root`` holds one private directory per sandbox, and each sandbox mounts only its
    own.  It is also this backend's record of what it created, so two hosts sharing it share
    ownership, and deleting it by hand orphans nothing that ``sbx rm`` cannot remove.

    ``name_prefix`` starts every sandbox name, so two applications on one host keep apart.

    ``cpus`` and ``memory`` are always passed, because an ``sbx`` sandbox otherwise takes every
    CPU and half the host's memory and nothing expires locally.

    ``command_timeout_seconds`` bounds every ``sbx`` command except ``create``, which
    ``create_timeout_seconds`` bounds because a first image pull takes tens of seconds, and
    ``exec``, which the caller's own timeout bounds.  ``exec_cleanup_timeout_seconds`` is the
    extra time an expired ``exec`` spends killing the guest's process group.
    """

    sbx_path: str = "sbx"
    workspace_root: Path | None = None
    name_prefix: str = "maf"
    cpus: int = 2
    memory: str = "2g"
    command_timeout_seconds: float = 60.0
    create_timeout_seconds: float = 600.0
    exec_cleanup_timeout_seconds: float = 20.0

    def __post_init__(self) -> None:
        if not _PREFIX.fullmatch(self.name_prefix):
            raise ValueError("name_prefix must be 1-16 lowercase letters or digits")
        if type(self.cpus) is not int or self.cpus < 1:
            raise ValueError("cpus must be a positive integer")
        if not _MEMORY.fullmatch(self.memory):
            raise ValueError("memory must be a size such as '512m' or '2g'")
        for name in (
            "command_timeout_seconds",
            "create_timeout_seconds",
            "exec_cleanup_timeout_seconds",
        ):
            value: object = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value <= 0
            ):
                raise ValueError(f"{name} must be a finite positive number of seconds")

    @property
    def resolved_workspace_root(self) -> Path:
        return self.workspace_root if self.workspace_root is not None else default_workspace_root()
