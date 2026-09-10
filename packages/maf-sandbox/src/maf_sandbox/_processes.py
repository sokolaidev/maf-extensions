"""Observe bounded process inventories around a supervised guest run."""

from __future__ import annotations

import asyncio
import json
import logging
import shlex
import time
from dataclasses import fields, replace
from functools import cache
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

from ._host_tools import HostToolRun
from ._observer import ProcessCleanup, ProcessesObserved, record, recorded_call
from ._process_info import ProcessAttribution, ProcessInfo, ProcessPhase
from ._protocol import Sandbox

logger = logging.getLogger(__name__)
_TIMEOUT = 3.0
_BYTES = 1024 * 1024


@cache
def _probe() -> str:
    return Path(__file__).with_name("_process_probe.py").read_text(encoding="utf-8")


def _decode(stdout: str) -> tuple[tuple[ProcessInfo, ...], bool]:
    if len(stdout.encode("utf-8")) > _BYTES:
        raise ValueError("process snapshot exceeds byte limit")
    decoded: object = json.loads(stdout)
    if not isinstance(decoded, dict):
        raise ValueError("invalid process snapshot")
    payload = cast(dict[str, Any], decoded)
    if not isinstance(payload.get("processes"), list):
        raise ValueError("invalid process snapshot")
    raw: list[Any] = payload["processes"]
    if len(raw) > 256:
        raise ValueError("process snapshot exceeds count limit")
    allowed = {f.name for f in fields(ProcessInfo)} - {"attribution"}
    result: list[ProcessInfo] = []
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("invalid process fields")
        data = cast(dict[str, Any], item).copy()
        if set(data) - allowed:
            raise ValueError("invalid process fields")
        for key in ("pid", "ppid", "pgid", "sid", "start_ticks"):
            if type(data.get(key)) is not int or data[key] < 0:
                raise ValueError("invalid process identity")
        if data["pid"] <= 1 or not isinstance(data.get("state"), str):
            # PID 1 remains observable, but can never be a signal target.
            if data["pid"] != 1 or not isinstance(data.get("state"), str):
                raise ValueError("invalid process identity")
        for key in ("groups", "argv", "unavailable"):
            incoming: object = data.get(key, [])
            if not isinstance(incoming, list):
                raise ValueError("invalid process list")
            values = cast(list[Any], incoming)
            expected = int if key == "groups" else str
            if len(values) > 256 or any(type(v) is not expected for v in values):
                raise ValueError("invalid process list")
            data[key] = tuple(values)
        for key in ("name", "username", "command", "executable", "cwd"):
            if data.get(key) is not None and not isinstance(data[key], str):
                raise ValueError("invalid process text")
        for key in (
            "uid",
            "effective_uid",
            "gid",
            "effective_gid",
            "threads",
            "user_ticks",
            "system_ticks",
            "rss_bytes",
            "virtual_bytes",
        ):
            if data.get(key) is not None and type(data[key]) is not int:
                raise ValueError("invalid process number")
        if type(data.get("truncated", False)) is not bool:
            raise ValueError("invalid truncation flag")
        result.append(ProcessInfo(**data))
    if len({p.pid for p in result}) != len(result):
        raise ValueError("duplicate process identity")
    if type(payload.get("incomplete")) is not bool:
        raise ValueError("invalid completeness flag")
    return tuple(result), payload["incomplete"]


class ProcessTracker:
    """Retain observed lineage within one physical sandbox and one run."""

    def __init__(
        self, sandbox: Sandbox, run: HostToolRun, interpreter: str, directory: str
    ) -> None:
        self.sandbox, self.run = sandbox, run
        self.interpreter, self.directory = interpreter, directory
        self.instance_id = sandbox.instance_id
        self.pid: int | None = None
        self.pgid: int | None = None
        self.baseline: set[tuple[int, int]] = set()
        self.known: dict[tuple[int, int], ProcessAttribution] = {}
        self.latest: tuple[ProcessInfo, ...] | None = None
        self.incomplete = True
        self.phase: ProcessPhase | None = None

    def attribute(self, processes: tuple[ProcessInfo, ...]) -> tuple[ProcessInfo, ...]:
        current = {p.pid: p for p in processes}
        for process in processes:
            if process.identity in self.baseline:
                continue
            if process.pid == self.pid and not any(pid == process.pid for pid, _ in self.known):
                self.known[process.identity] = "program"
            elif process.pgid == self.pgid and not any(pid == process.pid for pid, _ in self.known):
                self.known[process.identity] = "group"
        changed = True
        while changed:
            changed = False
            for process in processes:
                parent = current.get(process.ppid)
                if (
                    process.identity not in self.known
                    and process.identity not in self.baseline
                    and parent is not None
                    and parent.identity in self.known
                    and parent.start_ticks <= process.start_ticks
                ):
                    self.known[process.identity] = "descendant"
                    changed = True
        return tuple(
            replace(
                p,
                attribution=self.known.get(
                    p.identity, "preexisting" if p.identity in self.baseline else "unattributed"
                ),
            )
            for p in processes
        )

    async def snapshot(self, phase: ProcessPhase) -> None:
        self.phase = phase
        started = time.monotonic()
        timestamp = time.time()
        unavailable: str | None = None
        processes: tuple[ProcessInfo, ...] = ()
        incomplete = True
        try:
            if self.sandbox.instance_id != self.instance_id:
                raise ValueError("sandbox instance changed")
            async with asyncio.timeout(_TIMEOUT):
                result = await self.sandbox.exec(
                    f"{shlex.quote(self.interpreter)} -I -S -c {shlex.quote(_probe())}",
                    working_directory=self.directory,
                    timeout=_TIMEOUT,
                )
            if result.exit_code != 0:
                raise ValueError("process collector failed")
            processes, incomplete = _decode(result.stdout)
            if phase == "before_launch":
                self.baseline = {p.identity for p in processes}
            processes = self.attribute(processes)
            self.latest = processes
        except Exception as error:  # noqa: BLE001 - diagnostics must not prevent cleanup
            unavailable = type(error).__name__
            self.latest = None
        self.incomplete = incomplete
        event = ProcessesObserved(
            key=self.run.key,
            instance_id=self.instance_id,
            run_id=self.run.run_id,
            snapshot_id=uuid4().hex,
            phase=phase,
            timestamp=timestamp,
            seconds=time.monotonic() - started,
            processes=processes,
            incomplete=incomplete,
            unavailable=unavailable,
            call=recorded_call(),
        )
        record(self.run.registry.observer, event, logger)
        logger.info(
            "host tools: process snapshot run=%s instance=%s call=%s phase=%s pids=%s "
            "count=%d incomplete=%s unavailable=%s",
            self.run.run_id,
            self.instance_id,
            recorded_call(),
            phase,
            [p.pid for p in processes],
            len(processes),
            incomplete,
            unavailable,
        )

    def report_stop(self, outcome: str, reach: str, seconds: float) -> None:
        record(
            self.run.registry.observer,
            ProcessCleanup(
                key=self.run.key,
                instance_id=self.instance_id,
                run_id=self.run.run_id,
                pid=self.pid,
                pgid=self.pgid,
                outcome=outcome,
                reach=reach,
                seconds=seconds,
                call=recorded_call(),
            ),
            logger,
        )
        logger.info(
            "host tools: process cleanup run=%s instance=%s pid=%s pgid=%s outcome=%s reach=%s",
            self.run.run_id,
            self.instance_id,
            self.pid,
            self.pgid,
            outcome,
            reach,
        )

    def replaced(self) -> bool:
        """Refuse a recorded PID or group that now belongs to an observed replacement."""
        if self.sandbox.instance_id != self.instance_id:
            return True
        if self.latest is None:
            return False
        for p in self.latest:
            if p.pid == self.pid or p.pid == self.pgid:
                if p.identity in self.baseline or any(
                    pid == p.pid and start != p.start_ticks for pid, start in self.known
                ):
                    return True
        return False

    def survivors(self) -> tuple[ProcessInfo, ...]:
        return tuple(p for p in self.latest or () if p.running and p.identity in self.known)

    async def stop_descendants(self) -> bool:
        """Signal observed descendants outside the launcher's group; never signal mere additions."""
        targets = [p for p in self.survivors() if p.pid != self.pid and p.pgid != self.pgid]
        if not targets:
            return True
        started = time.monotonic()
        outcomes: dict[int, str] = {}
        try:
            identities = json.dumps([p.identity for p in targets])
            async with asyncio.timeout(_TIMEOUT):
                result = await self.sandbox.exec(
                    f"{shlex.quote(self.interpreter)} -I -S -c {shlex.quote(_probe())} "
                    f"--signal {shlex.quote(identities)}",
                    working_directory=self.directory,
                    timeout=_TIMEOUT,
                )
            if result.exit_code == 0 and len(result.stdout) <= _BYTES:
                raw: Any = json.loads(result.stdout)
                if isinstance(raw, list):
                    for row in cast(list[Any], raw):
                        if isinstance(row, dict):
                            entry = cast(dict[str, Any], row)
                            if type(entry.get("pid")) is int and entry.get("outcome") in {
                                "sent",
                                "absent",
                                "replaced",
                                "refused",
                            }:
                                outcomes[entry["pid"]] = entry["outcome"]
        except Exception:  # noqa: BLE001 - continue to verification and directory reclamation
            pass
        for process in targets:
            outcome = outcomes.get(process.pid, "unknown")
            record(
                self.run.registry.observer,
                ProcessCleanup(
                    key=self.run.key,
                    instance_id=self.instance_id,
                    run_id=self.run.run_id,
                    pid=process.pid,
                    pgid=None,
                    outcome=outcome,
                    reach="program" if outcome == "sent" else "nothing",
                    seconds=time.monotonic() - started,
                    call=recorded_call(),
                    start_ticks=process.start_ticks,
                ),
                logger,
            )
            logger.info(
                "host tools: descendant cleanup run=%s pid=%s outcome=%s",
                self.run.run_id,
                process.pid,
                outcome,
            )
        return all(outcomes.get(p.pid) in {"sent", "absent"} for p in targets)
