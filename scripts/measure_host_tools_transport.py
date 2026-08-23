"""Measure host-tool dispatch for sequential and concurrent guest request publication.

The guest double models only the request/response files. It keeps backend latency out of the
comparison while recording host arrivals, transport probes, and elapsed time. The result is a
measurement, not a performance gate: timings are printed for inspection and no threshold is
asserted.
"""

from __future__ import annotations

import asyncio
import json
import posixpath
import time
from dataclasses import dataclass
from typing import Any

from maf_sandbox import (
    EntryKind,
    ExecResult,
    HostToolRegistry,
    HostToolRun,
    Identity,
    SandboxEntry,
    SourceIntegrity,
    dispatch_over_exec,
    guest_run_layout,
    sandbox_tool,
)

_RUN = "/maf-sandbox/work/measurement"
_LAYOUT = guest_run_layout(_RUN)
_CALLS = 8


@dataclass(frozen=True)
class Measurement:
    """One transport run's counts and timing observations."""

    mode: str
    elapsed_seconds: float
    dispatches: int
    host_arrivals: int
    host_arrival_gaps: tuple[float, ...]
    stat_probes: int
    read_probes: int
    write_probes: int
    answers: tuple[int, ...]


class _Guest:
    """A guest that publishes requests one at a time or all at once."""

    def __init__(self, concurrent: bool) -> None:
        self.concurrent = concurrent
        self.files: dict[str, bytes] = {}
        self.answers: list[int] = []
        self._issued = 0
        self._collected = 0
        self._calls = [("add", {"left": index, "right": 1}) for index in range(_CALLS)]
        self._output = False
        self.stat_probes = 0
        self.read_probes = 0
        self.write_probes = 0

    async def exec(self, command: Any, *, working_directory: str, timeout: float) -> ExecResult:
        del command, working_directory, timeout
        self._issue_next()
        if self.concurrent:
            while self._issued < len(self._calls):
                self._issue_next()
        return ExecResult(stdout="", exit_code=0)

    async def run_code(self, code: str, *, timeout: float) -> ExecResult:
        """Unused: this measurement drives the exec transport. Present so it is a `Sandbox`."""
        del code, timeout
        raise NotImplementedError("this guest measures the exec transport only")

    async def stat_file(self, path: str, *, working_directory: str) -> SandboxEntry | None:
        del working_directory
        self.stat_probes += 1
        await asyncio.sleep(0)
        self._collect_answers()
        content = self.files.get(path)
        if content is None:
            return None
        return SandboxEntry(path=path, kind=EntryKind.FILE, size_bytes=len(content))

    async def read_file(self, path: str, *, working_directory: str, max_bytes: int) -> bytes:
        del working_directory, max_bytes
        self.read_probes += 1
        await asyncio.sleep(0)
        return self.files[path]

    async def write_file(self, path: str, content: str | bytes, *, working_directory: str) -> None:
        del working_directory
        self.write_probes += 1
        await asyncio.sleep(0)
        self.files[path] = content.encode() if isinstance(content, str) else content

    async def remove(self, path: str, *, working_directory: str, recursive: bool = False) -> None:
        del path, working_directory, recursive

    async def reclaim(self, directory: str, *, working_directory: str, timeout: float) -> None:
        """Really drop ``directory`` and everything under it from :attr:`files`.

        No confinement check: the caller made ``directory``. A directory with nothing stored
        under it is already the success this call promises.
        """
        del working_directory, timeout
        prefix = directory.rstrip("/") + "/"
        for stored in [path for path in self.files if path == directory or path.startswith(prefix)]:
            del self.files[stored]

    async def list_dir(self, path: str, *, working_directory: str) -> tuple[SandboxEntry, ...]:
        del path, working_directory
        return ()

    def _issue_next(self) -> None:
        if self._issued == len(self._calls):
            if self._collected == len(self._calls):
                self.files[_LAYOUT.output] = b"done"
                self.files[_LAYOUT.exit_code] = b"0"
                self._output = True
            return
        self._issued += 1
        name, arguments = self._calls[self._issued - 1]
        payload = {"id": f"{self._issued:04d}", "name": name, "arguments": arguments}
        path = posixpath.join(_LAYOUT.calls, f"{self._issued:04d}.request.json")
        self.files[path] = json.dumps(payload).encode()

    def _collect_answers(self) -> None:
        while self._collected < self._issued:
            index = self._collected + 1
            path = posixpath.join(_LAYOUT.calls, f"{index:04d}.response.json")
            content = self.files.get(path)
            if content is None:
                return
            self._collected = index
            self.answers.append(json.loads(content)["value"])
            if not self.concurrent:
                self._issue_next()
        if self._issued == len(self._calls) and self._collected == len(self._calls):
            self.files[_LAYOUT.output] = b"done"
            self.files[_LAYOUT.exit_code] = b"0"
            self._output = True


async def _measure(concurrent: bool) -> Measurement:
    arrivals: list[float] = []

    @sandbox_tool(source=SourceIntegrity.TRUSTED, sink=None, identity=Identity.APP)
    def add(left: int, right: int) -> int:
        arrivals.append(time.perf_counter())
        return left + right

    registry = HostToolRegistry()
    registry.register(add)
    guest = _Guest(concurrent)
    started = time.perf_counter()
    result = await dispatch_over_exec(
        guest,
        HostToolRun(registry),
        _LAYOUT,
        timeout=10,
        poll_interval=0.001,
    )
    elapsed = time.perf_counter() - started
    assert result.exit_code == 0
    gaps = tuple(later - earlier for earlier, later in zip(arrivals, arrivals[1:]))
    return Measurement(
        mode="concurrent" if concurrent else "sequential",
        elapsed_seconds=elapsed,
        dispatches=len(arrivals),
        host_arrivals=len(arrivals),
        host_arrival_gaps=gaps,
        stat_probes=guest.stat_probes,
        read_probes=guest.read_probes,
        write_probes=guest.write_probes,
        answers=tuple(guest.answers),
    )


def measure() -> tuple[Measurement, Measurement]:
    """Run both publication modes and return their transport measurements."""
    return asyncio.run(_measure(False)), asyncio.run(_measure(True))


def main() -> None:
    """Print sequential and concurrent measurements as JSON records."""
    for measurement in measure():
        print(json.dumps(measurement.__dict__, sort_keys=True))


if __name__ == "__main__":
    main()
