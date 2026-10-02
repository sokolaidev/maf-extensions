"""Optional sandbox lifetime support for a supervised guest program."""

from contextlib import AbstractAsyncContextManager
from typing import Protocol, runtime_checkable


class SandboxRunActivityLost(RuntimeError):
    """The activity keeping a guest alive ended; an in-flight host effect may have completed."""


class RunActivity(Protocol):
    """A run's lifetime guard, held through its process and file cleanup."""

    def check(self) -> None:
        """Raise ``SandboxRunActivityLost`` if the guard has ended unexpectedly."""
        ...


@runtime_checkable
class SandboxRunActivity(Protocol):
    """Optional surface used by the file-based host-tools transport before guest launch."""

    def run_activity(self, *, timeout: float) -> AbstractAsyncContextManager[RunActivity]:
        """Acquire one independent guard within ``timeout`` seconds.

        The timeout bounds acquisition only. Hold the guard until context exit, including
        host tools that outlast the run deadline, and release it on every exit. A failed
        guard must not cancel an in-flight host tool or replay its effect. Release must be
        cancellation-safe and must retire the sandbox if lifetime or cleanup is uncertain.
        """
        ...
