"""Resource budgets for the packaged Python guest on Windows WHP."""

from __future__ import annotations

import math
from dataclasses import dataclass

MAX_CODE_BYTES = 10 * 1024 * 1024
MAX_OUTPUT_BYTES = 16 * 1024 * 1024


@dataclass(frozen=True)
class HyperlightSandboxConfig:
    """Bound cold preparation, worker cleanup, program text and returned diagnostics.

    The Windows job also bounds worker memory, including native result buffering that occurs
    before the output limit can be checked. Heap, guest and hypervisor choices are fixed.
    """

    startup_timeout: float = 30.0
    cleanup_timeout: float = 3.0
    max_code_bytes: int = 1024 * 1024
    max_output_bytes: int = 1024 * 1024
    max_worker_memory_bytes: int = 1536 * 1024 * 1024

    def __post_init__(self) -> None:
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
            if isinstance(value, bool) or not isinstance(value, int) or not 0 < value <= ceiling:
                raise ValueError(f"{name} must be an integer between 1 and {ceiling}")
