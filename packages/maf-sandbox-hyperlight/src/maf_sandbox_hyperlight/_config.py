"""Resource budgets for the packaged Python guest."""

from __future__ import annotations

import math
import posixpath
import sys
from dataclasses import dataclass
from typing import cast

from ._pod_config import HyperlightPodConfig

MAX_CODE_BYTES = 10 * 1024 * 1024
MAX_OUTPUT_BYTES = 16 * 1024 * 1024


@dataclass(frozen=True)
class HyperlightSandboxConfig:
    """Bound cold preparation, worker cleanup, program text and returned diagnostics.

    Windows jobs or Linux cgroups bound native memory before output limits can be checked.
    Local Linux workers require delegated cgroups. Explicit pod containment uses its aggregate
    container budget instead; heap and guest choices are fixed.
    """

    startup_timeout: float = 30.0
    cleanup_timeout: float = 3.0
    max_code_bytes: int = 1024 * 1024
    max_output_bytes: int = 1024 * 1024
    max_worker_memory_bytes: int | None = 3 * 1024**3 if sys.platform == "linux" else 1536 * 1024**2
    linux_cgroup_root: str | None = None
    file_outputs: bool = False
    pod: HyperlightPodConfig | None = None

    def __post_init__(self) -> None:
        if self.pod is not None:
            if not isinstance(cast("object", self.pod), HyperlightPodConfig):
                raise TypeError("pod must be a HyperlightPodConfig")
            if self.max_worker_memory_bytes is not None or self.linux_cgroup_root is not None:
                raise ValueError(
                    "pod containment requires no per-worker memory limit or cgroup root"
                )
        elif self.max_worker_memory_bytes is None:
            raise ValueError("worker containment requires max_worker_memory_bytes")
        if type(self.file_outputs) is not bool:
            raise ValueError("file_outputs must be a boolean")
        if self.linux_cgroup_root is not None and (
            not isinstance(cast("object", self.linux_cgroup_root), str)
            or not posixpath.isabs(self.linux_cgroup_root)
            or "\x00" in self.linux_cgroup_root
        ):
            raise ValueError("linux_cgroup_root must be an absolute Linux path")
        for name in ("startup_timeout", "cleanup_timeout"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"{name} must be a positive finite number")
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be a positive finite number")
        for name, ceiling in (
            ("max_code_bytes", MAX_CODE_BYTES),
            ("max_output_bytes", MAX_OUTPUT_BYTES),
            ("max_worker_memory_bytes", 16 * 1024**3),
        ):
            value = getattr(self, name)
            if name == "max_worker_memory_bytes" and self.pod is not None:
                continue
            if isinstance(value, bool) or not isinstance(value, int) or not 0 < value <= ceiling:
                raise ValueError(f"{name} must be an integer between 1 and {ceiling}")
