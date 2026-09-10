"""Bounded process observations; evidence rather than proof of confinement."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

ProcessAttribution = Literal["program", "descendant", "group", "preexisting", "unattributed"]
ProcessPhase = Literal["before_launch", "after_launch", "before_cleanup", "after_cleanup"]


@dataclass(frozen=True)
class ProcessInfo:
    """One observed Linux process. Start ticks distinguish PID replacements within an instance."""

    pid: int
    ppid: int
    pgid: int
    sid: int
    start_ticks: int
    state: str
    name: str | None = None
    uid: int | None = None
    effective_uid: int | None = None
    gid: int | None = None
    effective_gid: int | None = None
    groups: tuple[int, ...] = ()
    username: str | None = None
    argv: tuple[str, ...] = ()
    command: str | None = None
    executable: str | None = None
    cwd: str | None = None
    threads: int | None = None
    user_ticks: int | None = None
    system_ticks: int | None = None
    rss_bytes: int | None = None
    virtual_bytes: int | None = None
    attribution: ProcessAttribution = "unattributed"
    unavailable: tuple[str, ...] = ()
    truncated: bool = False

    @property
    def identity(self) -> tuple[int, int]:
        return self.pid, self.start_ticks

    @property
    def running(self) -> bool:
        """Zombies and dead processes cannot continue guest work."""
        return self.state not in {"Z", "X", "x"}
