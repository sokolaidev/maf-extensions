"""Host-owned runtime and delegation settings."""

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class BubblewrapSandboxConfig:
    """Use a trusted Linux root directory and a private delegated cgroup v2 subtree.

    Runtime and control-directory ancestors must not be writable by untrusted principals.
    State and cgroup roots must stay outside the runtime mounted into the guest. Provision the
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
    egress_proxy: Path | None = None
    egress_proxy_sha256: str | None = None

    def __post_init__(self) -> None:
        if (self.egress_proxy is None) != (self.egress_proxy_sha256 is None):
            raise ValueError("egress_proxy and egress_proxy_sha256 must be configured together")
        if self.egress_proxy is not None:
            if not self.egress_proxy.is_absolute():
                raise ValueError("egress_proxy must be an absolute Path")
            digest = self.egress_proxy_sha256 or ""
            if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
                raise ValueError("egress_proxy_sha256 must be a lowercase SHA-256 digest")
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
        if type(self.max_timeout) not in (int, float) or not 0 < self.max_timeout <= 3600:
            raise ValueError("max_timeout must be positive and at most 3600 seconds")
        if not self.runtime_id or "\x00" in self.runtime_id:
            raise ValueError("runtime_id must be nonempty")
