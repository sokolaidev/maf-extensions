"""Select the host's worker containment without importing a native SDK."""

from __future__ import annotations

import subprocess
import sys
from typing import Protocol

from ._config import HyperlightSandboxConfig


class Lifetime(Protocol):
    """A worker must be contained before initialization and terminated as a tree."""

    def spawn(
        self, command: list[str], *, environment: dict[str, str], cwd: str, cleanup_timeout: float
    ) -> subprocess.Popen[bytes]: ...

    def ready(self, *, deadline: float) -> None: ...

    def close(self, *, deadline: float | None = None) -> None: ...


def create_job(config: HyperlightSandboxConfig) -> Lifetime:
    """Require kernel memory enforcement and owner-death cleanup on each host."""
    if config.pod is not None:
        if sys.platform != "linux":
            raise ValueError("pod containment requires Linux")
        from ._pod import PodJob

        return PodJob(config.pod, config.cleanup_timeout)
    assert config.max_worker_memory_bytes is not None
    if sys.platform == "linux":
        from ._linux import DEFAULT_CGROUP_ROOT, Job

        return Job(
            config.max_worker_memory_bytes,
            config.linux_cgroup_root or DEFAULT_CGROUP_ROOT,
            config.cleanup_timeout,
        )
    from ._windows import Job as WindowsJob

    return WindowsJob(config.max_worker_memory_bytes)
