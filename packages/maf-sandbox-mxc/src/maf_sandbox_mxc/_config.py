"""Explicit native assets and storage budgets for the MXC adapter."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from .files.request import FileLimits, WorkspaceLimits
from .files.transport import DIRECTORY_FORMAT
from .host_call import digest
from .host_store import Refused
from .shared_store import Limits, ScratchLimits


@dataclass(frozen=True)
class MxcSandboxConfig:
    """Host-owned, qualified file helper assets; persistent state requires an explicit opt-in.

    Asset directories must remain immutable while the backend is open. Use a separate store
    root for a different runtime profile; checkpoints are never migrated implicitly.
    """

    helper: Path
    startup: Path
    root: Path
    profile: str
    helper_sha256: str
    startup_sha256: str
    limits: Limits
    scratch: ScratchLimits
    files: FileLimits = FileLimits()
    workspace: WorkspaceLimits = WorkspaceLimits()
    persistent: bool = False

    def __post_init__(self) -> None:
        if not self.profile or len(self.profile.encode()) > 256:
            raise ValueError("a bounded runtime profile name is required")
        for value in (self.helper_sha256, self.startup_sha256):
            if len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
                raise ValueError("runtime identities must be lowercase SHA-256 values")
        if type(self.persistent) is not bool:
            raise ValueError("persistent must be a boolean")

    def identity(self) -> dict[str, str]:
        """Bind durable sessions to assets, network posture and transfer policy."""
        return {
            "adapter": DIRECTORY_FORMAT,
            "runtime": self.profile,
            "helper": self.helper_sha256,
            "startup": self.startup_sha256,
            "egress": "closed",
            "persistent": str(self.persistent),
            "files": repr(self.files),
            "workspace": repr(self.workspace),
        }

    def verify(self) -> None:
        """Refuse changed or redirected native assets before admitting execution."""
        if self.helper.is_symlink() or not self.helper.is_file():
            raise Refused("helper must be a regular host-owned file")
        if digest(self.helper) != self.helper_sha256:
            raise Refused("helper identity differs from configuration")
        if startup_identity(self.startup) != self.startup_sha256:
            raise Refused("startup identity differs from configuration")


def startup_identity(root: Path) -> str:
    """Hash a bounded, ordinary startup tree for explicit configuration pinning."""
    if root.is_symlink() or root.is_junction() or not root.is_dir():
        raise Refused("startup must be an ordinary directory")
    records: list[tuple[str, str]] = []
    pending = [root]
    entries = 0
    size = 0
    while pending:
        for path in pending.pop().iterdir():
            entries += 1
            if entries > 1024 or path.is_symlink() or path.is_junction():
                raise Refused("startup inventory is redirected or exceeds its bound")
            if path.is_dir():
                pending.append(path)
            elif path.is_file():
                size += path.stat().st_size
                if size > 2 * 1024**3:
                    raise Refused("startup exceeds its byte bound")
                records.append((path.relative_to(root).as_posix(), digest(path)))
            else:
                raise Refused("startup contains a special file")
    if not records:
        raise Refused("startup inventory is empty")
    encoded = json.dumps(sorted(records), separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()
