"""Deterministic storage faults and fencing for benchmark requests, with no external I/O."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any, cast


def snapshot(data: dict[str, Any]) -> dict[str, Any]:
    """Detach JSON state so observations cannot mutate stored sessions."""
    return cast(dict[str, Any], json.loads(json.dumps(data, allow_nan=False)))


class ScenarioFailure(ValueError):
    """A persistence-scenario failure; never a reason to retry model work."""


class ScenarioInterruption(ValueError):
    """A benchmark consumer deliberately stopped before stream completion."""


class ScenarioStore:
    """Simulate an atomic fenced store and retain independent request observations."""

    def __init__(self, scenario: str) -> None:
        self.scenario = scenario
        self.snapshots: dict[str, dict[str, Any]] = {}
        self.source_snapshot: dict[str, Any] = {}
        self.requests: list[dict[str, Any]] = []
        self.events: list[str] = []
        self.lock = asyncio.Lock()
        self.generation = 0
        self.active_token: int | None = None
        self.source = ""
        self.destination = ""
        self.committed = False
        self.notified = False

    def seed(self, data: dict[str, Any]) -> str | None:
        """Start a request from the runner's snapshot, including probe restoration."""
        self.source = data["session_id"]
        save_as = (
            f"{self.source}.branch-{len(self.requests) + 1}" if self.scenario == "branch" else None
        )
        self.destination = save_as or self.source
        stored = snapshot(data)
        if self.scenario in {"migrate", "invalid_migration"}:
            stored["type"] = "cachebench_legacy_session"
        if self.scenario != "branch" or self.source not in self.snapshots:
            self.snapshots[self.source] = stored
        self.source_snapshot = snapshot(self.snapshots[self.source])
        self.events = []
        self.committed = False
        self.notified = False
        return save_as

    @asynccontextmanager
    async def lease(self, session_id: str, *, save_as: str | None = None) -> AsyncGenerator[int]:
        """Hold the simulated source and destination lease through stream cleanup."""
        async with self.lock:
            if session_id != self.source or (save_as or session_id) != self.destination:
                raise ScenarioFailure("lease IDs do not match the request")
            self.generation += 1
            token = self.generation
            self.active_token = token
            self.events.append("lease_acquired")
            try:
                yield token
            finally:
                self.active_token = None
                self.events.append("lease_released")

    async def load(self, session_id: str) -> dict[str, Any]:
        self.events.append("load")
        return snapshot(self.snapshots[session_id])

    def migrate(self, data: dict[str, Any]) -> dict[str, Any]:
        self.events.append("migrate")
        if self.scenario != "invalid_migration":
            data["type"] = "session"
        return data

    async def save(self, session_id: str, data: dict[str, Any], *, lease: int) -> None:
        self.events.append("save_attempt")
        if self.scenario == "stale":
            self.generation += 1
        if lease != self.generation or self.active_token != lease:
            self.events.append("stale_rejected")
            raise ScenarioFailure("stale lease token")
        if self.scenario == "save_failure":
            raise ScenarioFailure("injected save failure before commit")
        self.snapshots[session_id] = snapshot(data)
        self.committed = True
        self.events.append("committed")

    async def after_save(self, session_id: str, data: dict[str, Any]) -> None:
        self.events.append("after_save")
        if self.active_token is None or self.snapshots.get(session_id) != data:
            raise ScenarioFailure("post-save hook ran outside the committed lease")
        if self.scenario == "after_save_failure":
            raise ScenarioFailure("injected post-save failure after commit")
        self.notified = True

    def callbacks(self) -> dict[str, Any]:
        """Return only the extension points selected for this experiment."""
        return {
            "load": self.load,
            "save": self.save,
            "lease": self.lease,
            **(
                {"migrate": self.migrate}
                if self.scenario in {"migrate", "invalid_migration"}
                else {}
            ),
            **(
                {"after_save": self.after_save}
                if self.scenario in {"after_save", "after_save_failure", "save_failure"}
                else {}
            ),
        }

    def finish(self, error: BaseException | None) -> None:
        """Record commit outcome separately from request success or post-save failure."""
        self.requests.append(
            {
                "source": self.source,
                "destination": self.destination,
                "committed": self.committed,
                "notified": self.notified,
                "error": type(error).__name__ if error else None,
                "events": list(self.events),
            }
        )
