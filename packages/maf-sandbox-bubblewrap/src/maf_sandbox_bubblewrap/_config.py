"""Host-owned runtime and delegation settings."""

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class BubblewrapSandboxConfig:
    """Use a trusted Linux root directory and a private delegated cgroup v2 subtree.

    Runtime and state ancestors must not be writable by untrusted principals. Provision the
    runtime separately; no archive extraction, package download or engine fallback occurs here.
    """

    runtime_root: Path
    state_root: Path
    cgroup_root: Path
    runtime_id: str = "native"
    bwrap: Path = Path("/usr/bin/bwrap")
    memory_bytes: int = 1024 * 1024 * 1024
    pids: int = 256
    cpu_quota: int = 200000
    workspace_bytes: int = 128 * 1024 * 1024
    output_bytes: int = 1024 * 1024
    max_timeout: float = 180

    def __post_init__(self) -> None:
        for name in ("runtime_root", "state_root", "cgroup_root", "bwrap"):
            path = getattr(self, name)
            if not isinstance(path, Path) or not (
                path.is_absolute() or (name == "bwrap" and path.as_posix().startswith("/"))
            ):
                raise ValueError(f"{name} must be an absolute Path")
        for name in ("memory_bytes", "pids", "cpu_quota", "workspace_bytes", "output_bytes"):
            value = getattr(self, name)
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if not 0 < self.max_timeout <= 3600:
            raise ValueError("max_timeout must be positive and at most 3600 seconds")
        if not self.runtime_id or "\x00" in self.runtime_id:
            raise ValueError("runtime_id must be nonempty")
