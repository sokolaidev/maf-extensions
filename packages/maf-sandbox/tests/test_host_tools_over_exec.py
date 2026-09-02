"""The file transport: a guest that calls out, a supervisor that answers, and the caps between.

The specimen is a scripted guest rather than `InProcessSandbox`, because what is under test is
an *interleaving* — a detached program that writes a request and blocks, a supervisor that
notices, answers, and comes back. A fake whose `exec` runs to completion cannot express that.

`_ScriptedGuest` never imports the generated shim; it writes request files itself. That is the
transport's own claim under test rather than a shortcut: the shim is guest-side convenience,
and a program bypassing it is served identically because every gate is host-side.
`TestTheGeneratedShim` runs the real thing against a real filesystem, which is what keeps the
two halves agreeing about names.
"""

from __future__ import annotations

import ast
import asyncio
import importlib.util
import json
import logging
import os
import pathlib
import posixpath
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
import unicodedata
from pathlib import Path
from typing import Any

import pytest

from maf_sandbox import (
    CALLS_DIRECTORY,
    SHIM_MODULE,
    EntryKind,
    ExecResult,
    GuestRunLayout,
    HostToolRegistry,
    HostToolRun,
    Identity,
    SandboxEntry,
    SandboxProgramTimeout,
    SandboxTransferCapExceeded,
    SourceIntegrity,
    TransferLimits,
    guest_run_layout,
    host_tool_calls_over_exec,
    host_tool_shim,
    launcher_script,
    reclaim_run,
    sandbox_tool,
)
from maf_sandbox import _host_tools_over_exec as host_tools_over_exec
from maf_sandbox._host_tools_over_exec import SESSION_MADE
from maf_sandbox._reclaim import close_unclean_notes, open_unclean_notes
from maf_sandbox._shim_wire_contract import (
    assert_calls_conform,
    assert_request_conforms,
)
from maf_sandbox.paths import confine_resolve_guest_path, guest_path_relative_to

#: Fast enough for a suite, and still an interval — the API refuses zero, because
#: `sleep(0)` is not a throttle.
_FAST = 0.001

_RUN = "/maf-sandbox/work/run-1"
_LAYOUT = guest_run_layout(_RUN)

#: Read through `getattr` for the reason `_SIGKILL` is: this file is type-checked on Windows,
#: where `os` has no `geteuid`.
_RUNNING_AS_ROOT = getattr(os, "geteuid", lambda: 1)() == 0

#: The signal the transport sends, for the tests that send it themselves. Read through
#: `getattr` because this file is type-checked on Windows, where `signal` has no `SIGKILL` —
#: the tests that use it are skipped there, but pyright reads them anyway.
_SIGKILL = getattr(signal, "SIGKILL", 9)

#: Scripted in place of a name: the caller took this number and could not publish under it.
_ABANDONED = "<abandoned>"

#: Where `site` looks for user packages under `PYTHONUSERBASE`, relative to that base, on the
#: POSIX guest these launcher tests target. Built from the running interpreter because the
#: test plants a hook there and then runs `sys.executable` against it, so a hard-coded version
#: would make the case vacuous the next time Python bumps a minor — it would plant the file
#: somewhere nothing reads and pass whether the launcher filtered anything or not.
_USER_SITE_UNDER_BASE = f"lib/python{sys.version_info.major}.{sys.version_info.minor}/site-packages"


@sandbox_tool(source=SourceIntegrity.TRUSTED, sink=None, identity=Identity.APP)
def add(left: int, right: int) -> int:
    """Add two numbers, in the host process."""
    return left + right


class _ScriptedGuest:
    """A sandbox whose detached "program" makes host-tool calls, one at a time.

    The program advances only when the supervisor answers, which is the interleaving a real
    guest has: write request `n`, block, and write request `n+1` once the response lands.
    """

    def __init__(
        self,
        calls: list[tuple[str, dict[str, Any]]],
        *,
        exit_code: int = 0,
        output: str = "done",
        finish: bool = True,
        launcher_exit_code: int = 0,
        request_bytes: int | None = None,
        raw_request: bytes | None = None,
        replay_after_answer: bool = False,
        reclaim_error: Exception | None = None,
    ) -> None:
        self.files: dict[str, bytes] = {}
        self.calls = calls
        self.answers: list[Any] = []
        #: Every directory `reclaim` was asked for, in order.
        self.reclaimed: list[str] = []
        self._reclaim_error = reclaim_error
        self.started = False
        self._exit_code = exit_code
        self._output = output
        self._finish = finish
        self._launcher_exit_code = launcher_exit_code
        self._request_bytes = request_bytes
        self._raw_request = raw_request
        self._replay_after_answer = replay_after_answer
        self.replayed = False
        self._polls_after_replay = 0
        self._issued = 0
        self._collected = 0

    # -- Sandbox ------------------------------------------------------------------------

    def _resolved(self, path: str, working_directory: str) -> str:
        """What a backend would make of this path — `confine_resolve_guest_path`, as they all use.

        A double keying on the string it was handed cannot tell a path a real backend
        accepts from one it refuses, which is most of what these tests are for.
        """
        return confine_resolve_guest_path(path, working_directory)

    async def write_file(self, path: str, content: str | bytes, *, working_directory: str) -> None:
        await asyncio.sleep(0)  # as in `stat_file`: a bound is only a bound against a yield
        del working_directory
        self.files[path] = content.encode("utf-8") if isinstance(content, str) else content

    async def exec(
        self, command: str | Any, *, working_directory: str, timeout: float
    ) -> ExecResult:
        del command, working_directory, timeout
        self.started = True
        if self._launcher_exit_code == 0:
            self._issue_next()
        return ExecResult(stdout="", exit_code=self._launcher_exit_code)

    async def stat_file(self, path: str, *, working_directory: str) -> SandboxEntry | None:
        # Every real backend suspends here, and a bound only bites a call that does: under
        # `asyncio.timeout` a coroutine that never yields finishes even on a zero budget, so
        # a double that never yields cannot tell a grace from no grace at all.
        await asyncio.sleep(0)
        path = self._resolved(path, working_directory)
        self._collect_answers()
        if self.replayed:
            # Stay alive a few polls, so a supervisor that rescans has every chance to.
            self._polls_after_replay += 1
            if self._polls_after_replay > 4 and self._finish:
                self.files[_LAYOUT.output] = self._output.encode("utf-8")
                self.files[_LAYOUT.exit_code] = str(self._exit_code).encode("utf-8")
        content = self.files.get(path)
        if content is None:
            return None
        return SandboxEntry(path=path, kind=EntryKind.FILE, size_bytes=len(content))

    async def read_file(self, path: str, *, working_directory: str, max_bytes: int) -> bytes:
        await asyncio.sleep(0)  # as in `stat_file`: a bound is only a bound against a yield
        content = self.files[self._resolved(path, working_directory)]
        if len(content) > max_bytes:
            raise AssertionError("the supervisor read past its own cap")
        return content

    async def remove(self, path: str, *, working_directory: str, recursive: bool = False) -> None:
        """Not what this double is for; the protocol needs it present, not useful."""
        raise NotImplementedError

    async def reclaim(self, directory: str, *, working_directory: str, timeout: float) -> None:
        """Record the directory, and refuse when the test asked for a refusal.

        Records rather than deletes, for the reason `TestRemovalAgainstARealFilesystem` gives:
        what these tests answer is which directory the transport named, and a double that
        emptied its own store would take the evidence of everything else with it. The
        refusal is configurable because a double without one makes every test about a
        *reported* cleanup failure pass on whatever the double happened to raise instead.
        """
        del working_directory
        # After the budget check, for the reason the kill is: a removal the transport had no
        # time to run is not a removal, and recording it on entry hid exactly that.
        await _spend(timeout, f"the reclaim of {directory}")
        self.reclaimed.append(directory)
        if self._reclaim_error is not None:
            raise self._reclaim_error

    async def list_dir(self, path: str, *, working_directory: str) -> tuple[SandboxEntry, ...]:
        raise NotImplementedError("this transport must not need FILES_LIST")

    # -- the "program" ------------------------------------------------------------------

    def _issue_next(self) -> None:
        if self._issued >= len(self.calls):
            if self._finish:
                self.files[_LAYOUT.output] = self._output.encode("utf-8")
                self.files[_LAYOUT.exit_code] = str(self._exit_code).encode("utf-8")
            return
        name, arguments = self.calls[self._issued]
        self._issued += 1
        if name == _ABANDONED:
            # One caller's number, claimed and abandoned. Nothing waits on it, so the double
            # does not either — and the caller behind it has already published, which is the
            # whole reason a hole here would strand somebody.
            self.files[self._request_path(self._issued)] = json.dumps(
                {"id": f"{self._issued:04d}", "abandoned": True}
            ).encode()
            self._collected = self._issued
            self._issue_next()
            return
        payload: dict[str, Any] = {
            "id": f"{self._issued:04d}",
            "name": name,
            "arguments": arguments,
        }
        if self._request_bytes is not None:
            payload["padding"] = "x" * self._request_bytes
        body = self._raw_request if self._raw_request is not None else json.dumps(payload).encode()
        self.files[self._request_path(self._issued)] = body

    def _collect_answers(self) -> None:
        """Read the answer to the outstanding call, if it has landed, and issue the next.

        Once each: a real program returns from `call()` one time, and a double that re-read
        its own answer on every poll would report a dispatch that never happened twice.
        """
        if self._issued == 0 or self._collected >= self._issued:
            return
        answered = self.files.get(self._response_path(self._issued))
        if answered is None:
            return
        self._collected = self._issued
        self.answers.append(json.loads(answered))
        if self._replay_after_answer and not self.replayed:
            # A guest spending a second dispatch on an answer it already has.
            del self.files[self._response_path(self._issued)]
            self.replayed = True
            return
        self._issue_next()

    def _request_path(self, index: int) -> str:
        return posixpath.join(_LAYOUT.calls, f"{index:04d}.request.json")

    def _response_path(self, index: int) -> str:
        return posixpath.join(_LAYOUT.calls, f"{index:04d}.response.json")


class _ConcurrentGuest(_ScriptedGuest):
    """A guest that publishes every request before waiting for their ordered answers."""

    async def exec(
        self, command: str | Any, *, working_directory: str, timeout: float
    ) -> ExecResult:
        result = await super().exec(command, working_directory=working_directory, timeout=timeout)
        while self._issued < len(self.calls):
            self._issue_next()
        return result

    def _collect_answers(self) -> None:
        while self._collected < self._issued:
            index = self._collected + 1
            answered = self.files.get(self._response_path(index))
            if answered is None:
                return
            self._collected = index
            self.answers.append(json.loads(answered))
        if self._finish and self._collected == len(self.calls):
            self.files[_LAYOUT.output] = self._output.encode("utf-8")
            self.files[_LAYOUT.exit_code] = str(self._exit_code).encode("utf-8")


def _registry(**kwargs: Any) -> HostToolRegistry:
    registry = HostToolRegistry(**kwargs)
    registry.register(add)
    return registry


def _run(
    guest: _ScriptedGuest,
    run: HostToolRun,
    *,
    timeout: float = 5.0,
    poll: float = _FAST,
    output_limit: int | None = None,
) -> ExecResult:
    return asyncio.run(
        host_tool_calls_over_exec(
            guest, run, _LAYOUT, timeout=timeout, poll_interval=poll, output_limit=output_limit
        )
    )


class TestTheHappyPath:
    def test_two_calls_are_answered_and_the_program_finishes(self):
        guest = _ScriptedGuest(
            [("add", {"left": 2, "right": 3}), ("add", {"left": 10, "right": 1})]
        )
        result = _run(guest, HostToolRun(_registry()))
        assert result.exit_code == 0
        assert result.stdout == "done"
        assert [answer["value"] for answer in guest.answers] == [5, 11]

    def test_the_launcher_is_written_and_started(self):
        guest = _ScriptedGuest([])
        _run(guest, HostToolRun(_registry()))
        assert guest.started
        launcher = guest.files[_LAYOUT.launcher].decode("utf-8")
        assert "nohup" in launcher
        assert _LAYOUT.program in launcher
        assert _LAYOUT.exit_code in launcher

    def test_a_launcher_that_fails_is_reported_without_supervising(self):
        guest = _ScriptedGuest([("add", {"left": 1, "right": 1})], launcher_exit_code=127)
        result = _run(guest, HostToolRun(_registry()))
        assert result.exit_code == 127
        assert guest.answers == []

    def test_a_launcher_that_fails_without_a_word_still_gets_a_reason(self):
        """An empty ``stderr`` beside a non-zero exit tells a caller nothing it can act on,
        so the transport supplies the sentence itself."""
        guest = _ScriptedGuest([], launcher_exit_code=127)
        result = _run(guest, HostToolRun(_registry()))
        assert result.stderr == "the launcher did not start the program"


class TestTheOverlappedProbes:
    def test_an_exit_marker_cancels_and_drains_the_request_probe(self):
        class _ExitWins(_ScriptedGuest):
            request_started = False
            request_drained = False

            async def stat_file(self, path: str, *, working_directory: str):
                if path == _LAYOUT.exit_code:
                    while not self.request_started:
                        await asyncio.sleep(0)
                return await super().stat_file(path, working_directory=working_directory)

            async def read_file(self, path: str, *, working_directory: str, max_bytes: int):
                if path.endswith(".request.json"):
                    self.request_started = True
                    try:
                        await asyncio.sleep(3600)
                    finally:
                        self.request_drained = True
                return await super().read_file(
                    path, working_directory=working_directory, max_bytes=max_bytes
                )

        async def probe() -> tuple[object, tuple[object, ...], _ExitWins]:
            guest = _ExitWins([], finish=False)
            guest.files[_LAYOUT.exit_code] = b"0"
            guest.files[guest._request_path(1)] = b'{"name": "add", "arguments": {}}'
            finished, requests = await host_tools_over_exec._probe_exit_and_requests(
                guest,
                HostToolRun(_registry()),
                _LAYOUT,
                served=0,
                count=1,
                deadline=time.monotonic() + 1,
            )
            return finished, requests, guest

        finished, requests, guest = asyncio.run(probe())
        assert finished == "0"
        assert requests == ()
        assert guest.request_drained, "the losing request probe survived the completed poll"

    def test_a_marker_error_drains_the_request_probe_before_it_propagates(self):
        class _MarkerFails(_ScriptedGuest):
            request_started = False
            request_drained = False

            async def stat_file(self, path: str, *, working_directory: str):
                if path == _LAYOUT.exit_code:
                    while not self.request_started:
                        await asyncio.sleep(0)
                    raise RuntimeError("marker failed")
                if path.endswith(".request.json"):
                    self.request_started = True
                    try:
                        await asyncio.sleep(3600)
                    finally:
                        self.request_drained = True
                return await super().stat_file(path, working_directory=working_directory)

        guest = _MarkerFails([], finish=False)
        with pytest.raises(RuntimeError, match="marker failed"):
            asyncio.run(
                host_tools_over_exec._probe_exit_and_requests(
                    guest,
                    HostToolRun(_registry()),
                    _LAYOUT,
                    served=0,
                    count=1,
                    deadline=time.monotonic() + 1,
                )
            )
        assert guest.request_drained, "the sibling probe was not awaited after the marker failed"

    def test_cancelling_the_poll_drains_both_probes(self):
        class _Blocked(_ScriptedGuest):
            entered: set[str]
            drained: set[str]

            def __init__(self) -> None:
                super().__init__([], finish=False)
                self.entered = set()
                self.drained = set()

            async def stat_file(self, path: str, *, working_directory: str):
                kind = "marker" if path == _LAYOUT.exit_code else "request"
                self.entered.add(kind)
                try:
                    await asyncio.sleep(3600)
                finally:
                    self.drained.add(kind)

        async def cancel() -> _Blocked:
            guest = _Blocked()
            poll = asyncio.create_task(
                host_tools_over_exec._probe_exit_and_requests(
                    guest,
                    HostToolRun(_registry()),
                    _LAYOUT,
                    served=0,
                    count=1,
                    deadline=time.monotonic() + 10,
                )
            )
            while guest.entered != {"marker", "request"}:
                await asyncio.sleep(0)
            poll.cancel()
            with pytest.raises(asyncio.CancelledError):
                await poll
            return guest

        guest = asyncio.run(cancel())
        assert guest.drained == {"marker", "request"}


class TestSpeculativeRequestDiscovery:
    def test_a_contiguous_prefix_widens_the_bounded_window(self):
        looks: list[str] = []
        reads: list[str] = []
        dispatched: list[int] = []

        @sandbox_tool(source=SourceIntegrity.TRUSTED, sink=None, identity=Identity.APP)
        def record(value: int) -> int:
            dispatched.append(value)
            return value

        class _RecordsRequests(_ConcurrentGuest):
            async def stat_file(self, path: str, *, working_directory: str):
                if path.endswith(".request.json"):
                    looks.append(posixpath.basename(path))
                return await super().stat_file(path, working_directory=working_directory)

            async def read_file(self, path: str, *, working_directory: str, max_bytes: int):
                if path.endswith(".request.json"):
                    reads.append(posixpath.basename(path))
                return await super().read_file(
                    path, working_directory=working_directory, max_bytes=max_bytes
                )

        registry = HostToolRegistry()
        registry.register(record)
        guest = _RecordsRequests([("record", {"value": value}) for value in range(6)])
        result = _run(guest, HostToolRun(registry))

        assert result.exit_code == 0
        assert dispatched == [0, 1, 2, 3, 4, 5]
        assert [answer["value"] for answer in guest.answers] == dispatched
        assert looks[:7] == [
            "0001.request.json",
            "0002.request.json",
            "0003.request.json",
            # 0003 twice: once as the speculative tail of the 2-window, once as the
            # frontier read's own stat when it becomes the frontier — discovery is by
            # stat, never by read.
            "0003.request.json",
            "0004.request.json",
            "0005.request.json",
            "0006.request.json",
        ]
        assert not any(name >= "0008.request.json" for name in looks)
        # The fold budgets one read per served request, so a speculative stat that says
        # present must not become a read until the identifier is the one being served (#659).
        assert sorted(reads) == [f"{n:04d}.request.json" for n in range(1, 7)]

    def test_a_speculative_miss_collapses_the_next_window(self):
        looks: list[str] = []

        class _RecordsRequests(_ScriptedGuest):
            async def stat_file(self, path: str, *, working_directory: str):
                if path.endswith(".request.json"):
                    looks.append(posixpath.basename(path))
                return await super().stat_file(path, working_directory=working_directory)

        guest = _RecordsRequests([("add", {"left": value, "right": 1}) for value in range(4)])
        _run(guest, HostToolRun(_registry()))

        assert looks[:6] == [
            "0001.request.json",
            "0002.request.json",
            "0003.request.json",
            "0003.request.json",
            "0004.request.json",
            "0005.request.json",
        ]

    def test_a_request_waiting_behind_a_gap_is_never_read_twice(self):
        """The #659 budget: a request is read once — when it is served.

        The fixture holds a claimed-but-unpublished frontier (0002) with 0003 published
        behind it, and the dead-claim grace steps the frontier over the hole. Whatever
        the interleaving, each served request is read exactly once; 0002 — a claim with
        no request behind it — is read never.
        """
        reads: list[str] = []

        class _GapGuest(_ConcurrentGuest):
            def __init__(self) -> None:
                super().__init__([], finish=False)
                # The multi-worker end state: 0001 and 0003 published, a worker's claim
                # on 0002, and 0002 itself never arriving.
                self._issued = 2
                self.files[self._request_path(1)] = json.dumps(
                    {"id": "0001", "name": "add", "arguments": {"left": 1, "right": 1}}
                ).encode()
                self.files[self._request_path(3)] = json.dumps(
                    {"id": "0003", "name": "add", "arguments": {"left": 3, "right": 0}}
                ).encode()
                self.files[posixpath.join(_LAYOUT.calls, "0002.claim")] = b""

            async def exec(
                self, command: str | Any, *, working_directory: str, timeout: float
            ) -> ExecResult:
                del command, working_directory, timeout
                self.started = True
                return ExecResult(stdout="", exit_code=0)

            async def stat_file(self, path: str, *, working_directory: str) -> SandboxEntry | None:
                entry = await super().stat_file(path, working_directory=working_directory)
                # A real program finishes once its own calls are answered; it does not
                # wait on the number whose caller died. The marker lands when 0003 —
                # the last call the surviving worker made — has its response.
                if (
                    self.files.get(self._response_path(1)) is not None
                    and self.files.get(self._response_path(3)) is not None
                ):
                    self.files[_LAYOUT.output] = b"done"
                    self.files[_LAYOUT.exit_code] = b"0"
                self._collect_answers()
                return entry

            async def read_file(self, path: str, *, working_directory: str, max_bytes: int):
                if path.endswith(".request.json"):
                    reads.append(posixpath.basename(path))
                return await super().read_file(
                    path, working_directory=working_directory, max_bytes=max_bytes
                )

        guest = _GapGuest()
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(host_tools_over_exec, "_CLAIM_HOLE_GRACE", 0.0)
            result = _run(guest, HostToolRun(_registry()), timeout=3.0)

        # Reaching exit 0 proves the hole at 0002 was stepped over: nothing else moves
        # the frontier past a number that never publishes.
        assert result.exit_code == 0
        # One read each across the whole run: 0003 sat published inside a wide window
        # for the miss poll and was read only when it reached the frontier. 0002 is
        # read never — a claim with no request behind it is stat-only evidence.
        assert reads == [
            "0001.request.json",
            "0003.request.json",
        ]

    def test_a_spent_allowance_polls_the_marker_through_the_bounded_read(self):
        """Once the allowance is spent, the marker poll must not swallow errors.

        The spent branch polls every interval, so it cannot use the one-shot final
        look whose broad except exists to keep a diagnostic from replacing the run's
        own reason. A backend that fails the marker stat/read after the allowance is
        spent must reach the caller as the transport failure it is — not surface as
        a guest timeout after retries the last look was never meant to make.
        """

        @sandbox_tool(source=SourceIntegrity.TRUSTED, sink=None, identity=Identity.APP)
        def add(value: int) -> int:
            return value

        registry = HostToolRegistry(max_host_tool_calls_per_run=16)
        registry.register(add)
        failing = {"armed": False}

        class _ExhaustedGuest(_ScriptedGuest):
            def __init__(self) -> None:
                super().__init__([("add", {"value": n}) for n in range(20)], finish=False)
                # A claim on 0002 forces the claim-hole path once the frontier sits
                # on it — the ordinary way a run reaches a spent allowance while the
                # guest keeps calling.
                self.files[posixpath.join(_LAYOUT.calls, "0002.claim")] = b""

            async def stat_file(self, path: str, *, working_directory: str) -> SandboxEntry | None:
                if failing["armed"] and path.endswith("program_exit_code"):
                    raise RuntimeError("the marker read keeps failing at the transport")
                entry = await super().stat_file(path, working_directory=working_directory)
                # Once the refusal past the cap has landed, the allowance is spent and
                # the supervisor is polling the marker — arm the transport failure now.
                if self.files.get(self._response_path(17)) is not None:
                    failing["armed"] = True
                return entry

            def _issue_next(self) -> None:
                super()._issue_next()
                if self._issued >= 3 and self._request_path(3) not in self.files:
                    # The rest of the calls publish behind the claim on 0002: 0003+
                    # are visible to the window while the frontier is stuck.
                    for index in range(3, len(self.calls) + 1):
                        payload: dict[str, Any] = {
                            "id": f"{index:04d}",
                            "name": "add",
                            "arguments": {"value": index},
                        }
                        self.files[self._request_path(index)] = json.dumps(payload).encode()

        guest = _ExhaustedGuest()
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(host_tools_over_exec, "_CLAIM_HOLE_GRACE", 0.0)
            with pytest.raises(RuntimeError, match="marker read keeps failing"):
                _run(guest, HostToolRun(registry), timeout=10.0)

        class _SecondProbeFails(_ScriptedGuest):
            async def stat_file(self, path: str, *, working_directory: str):
                if path.endswith("0002.request.json"):
                    raise RuntimeError("second probe failed")
                return await super().stat_file(path, working_directory=working_directory)

        async def serve(guest: _SecondProbeFails) -> tuple[int, bool]:
            _, probes = await host_tools_over_exec._probe_exit_and_requests(
                guest,
                HostToolRun(_registry()),
                _LAYOUT,
                served=0,
                count=2,
                deadline=time.monotonic() + 1,
            )
            return await host_tools_over_exec._serve_request_probes(
                guest, HostToolRun(_registry()), _LAYOUT, 0, probes, time.monotonic() + 1
            )

        guest = _SecondProbeFails([], finish=False)
        assert asyncio.run(serve(guest)) == (0, False)

        guest.files[guest._request_path(1)] = b'{"id": "0001", "abandoned": true}'
        with pytest.raises(RuntimeError, match="second probe failed"):
            asyncio.run(serve(guest))

    def test_speculation_never_reads_past_the_transport_allowance(self):
        looks: list[str] = []

        class _RecordsRequests(_ConcurrentGuest):
            async def stat_file(self, path: str, *, working_directory: str):
                if path.endswith(".request.json"):
                    looks.append(posixpath.basename(path))
                return await super().stat_file(path, working_directory=working_directory)

        guest = _RecordsRequests([("add", {"left": 1, "right": 1})] * 4)
        with pytest.raises(SandboxProgramTimeout):
            _run(
                guest,
                HostToolRun(_registry(max_host_tool_calls_per_run=1)),
                timeout=0.1,
            )

        assert len(guest.answers) == 2
        assert guest.answers[0]["value"] == 2
        assert "host-tool-call cap" in guest.answers[1]["refusal"]
        assert "0003.request.json" not in looks


class _DeadWorkerGuest(_ScriptedGuest):
    """A guest state no single-process program produces: a worker claimed ``0001`` and died
    before publishing it, while a surviving worker published ``0002``. The exit marker lands only
    once ``0002`` has been answered, so the supervisor cannot finish without first stepping over
    the ``0001`` hole — which is exactly the reading under test (#352). No dead process required.
    """

    def __init__(self) -> None:
        super().__init__([], finish=False)

    async def exec(self, command: str | Any, *, working_directory: str, timeout: float):
        del command, working_directory, timeout
        self.started = True
        self.files[posixpath.join(_LAYOUT.calls, "0001.claim")] = b""
        self.files[posixpath.join(_LAYOUT.calls, "0002.request.json")] = json.dumps(
            {"id": "0002", "name": "add", "arguments": {"left": 2, "right": 3}}
        ).encode()
        return ExecResult(stdout="", exit_code=0)

    async def stat_file(self, path: str, *, working_directory: str):
        await asyncio.sleep(0)  # as everywhere: a bound only bites a call that yields
        resolved = self._resolved(path, working_directory)
        if (
            posixpath.join(_LAYOUT.calls, "0002.response.json") in self.files
            and _LAYOUT.exit_code not in self.files
        ):
            self.files[_LAYOUT.output] = b"done"
            self.files[_LAYOUT.exit_code] = b"0"
        content = self.files.get(resolved)
        if content is None:
            return None
        return SandboxEntry(path=resolved, kind=EntryKind.FILE, size_bytes=len(content))


class _StatTable:
    """A sandbox that answers ``stat_file`` from a fixed ``basename -> size`` table, for driving
    :func:`_skip_dead_claim_hole` over each filesystem state directly. ``None`` models a backend
    that cannot state a size; an absent key is a missing file."""

    def __init__(self, sizes: dict[str, int | None]) -> None:
        self._sizes = sizes

    async def stat_file(self, path: str, *, working_directory: str):
        await asyncio.sleep(0)
        resolved = confine_resolve_guest_path(path, working_directory)
        name = posixpath.basename(resolved)
        if name not in self._sizes:
            return None
        return SandboxEntry(path=resolved, kind=EntryKind.FILE, size_bytes=self._sizes[name])


class TestADeadWorkerClaimHole:
    """A number claimed by a worker that died before publishing must not strand the rest of the
    run: the supervisor steps over it after the grace and serves the later calls (#352)."""

    @pytest.mark.parametrize(
        ("sizes", "expected"),
        [
            # claim present, a later request present, the frontier's own request absent → skip.
            pytest.param({"0001.claim": 0, "0002.request.json": 20}, (1, True), id="hole"),
            # nothing claimed the frontier → not a dead worker, do not skip (the claim guard).
            pytest.param({"0002.request.json": 20}, (0, False), id="no-claim"),
            # nothing has moved past the frontier → nothing stranded yet, wait (the later guard).
            pytest.param({"0001.claim": 0}, (0, False), id="nothing-past-it"),
            # the frontier's request arrived while the check ran → serve it, never skip it.
            pytest.param(
                {"0001.claim": 0, "0001.request.json": 20, "0002.request.json": 20},
                (0, False),
                id="frontier-arrived",
            ),
            # the frontier is there but its size is unknown → fail closed, do not skip.
            pytest.param(
                {"0001.claim": 0, "0001.request.json": None, "0002.request.json": 20},
                (0, False),
                id="frontier-size-unknown",
            ),
            # a zero-length frontier has not arrived, so it is still a hole.
            pytest.param(
                {"0001.claim": 0, "0001.request.json": 0, "0002.request.json": 20},
                (1, True),
                id="frontier-size-zero",
            ),
        ],
    )
    def test_the_hole_check_reads_each_state_correctly(
        self, sizes: dict[str, int | None], expected: tuple[int, bool]
    ):
        """Every guard in `_skip_dead_claim_hole`, pinned directly: the claim, the later request,
        the frontier re-read (including a size the backend cannot state, which must fail closed)."""
        result = asyncio.run(
            host_tools_over_exec._skip_dead_claim_hole(
                _StatTable(sizes), _LAYOUT, served=0, deadline=time.monotonic() + 1
            )
        )
        assert result == expected

    def test_a_claimed_but_unpublished_number_is_skipped_after_the_grace(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ):
        monkeypatch.setattr(host_tools_over_exec, "_CLAIM_HOLE_GRACE", 0.0)
        guest = _DeadWorkerGuest()
        with caplog.at_level(logging.WARNING):
            result = _run(guest, HostToolRun(_registry()))
        assert result.exit_code == 0
        answered = guest.files.get(posixpath.join(_LAYOUT.calls, "0002.response.json"))
        assert answered is not None, "the later call was stranded behind the dead-worker hole"
        assert json.loads(answered)["value"] == 5
        assert any(
            "claimed but never published" in record.getMessage() for record in caplog.records
        )

    def test_a_slow_first_call_is_served_in_order_not_skipped(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """A single guest slow to issue its one call: the frontier is absent for longer than the
        grace, but nothing claimed it and nothing is past it, so the hole check declines to skip
        (the per-guard reasoning is pinned in `test_the_hole_check_reads_each_state_correctly`;
        this holds the whole loop to serving the call in order rather than stepping over it)."""
        monkeypatch.setattr(host_tools_over_exec, "_CLAIM_HOLE_GRACE", 0.0)

        class _SlowFirstCall(_ScriptedGuest):
            _polls = 0

            async def stat_file(self, path: str, *, working_directory: str):
                # Withhold 0001 for a few polls past the (zeroed) grace, then let it arrive; the
                # double writes neither a claim nor a later request, so nothing looks like a hole.
                if path.endswith("0001.request.json"):
                    self._polls += 1
                    if self._polls <= 3:
                        return None
                return await super().stat_file(path, working_directory=working_directory)

        guest = _SlowFirstCall([("add", {"left": 1, "right": 1})])
        result = _run(guest, HostToolRun(_registry()))
        assert result.exit_code == 0
        assert [answer["value"] for answer in guest.answers] == [2], "the slow call was served"


class TestWhatTheGuestIsAllowedToSee:
    def test_an_unregistered_name_comes_back_as_a_sentence(self):
        guest = _ScriptedGuest([("delete_everything", {})])
        _run(guest, HostToolRun(_registry()))
        assert "is not a registered host tool" in guest.answers[0]["refusal"]
        assert "value" not in guest.answers[0]

    def test_the_dispatch_cap_refuses_rather_than_ending_the_program(self):
        guest = _ScriptedGuest(
            [("add", {"left": 1, "right": 1}), ("add", {"left": 2, "right": 2})],
            output="finished after a refusal",
        )
        result = _run(guest, HostToolRun(_registry(max_host_tool_calls_per_run=1)))
        assert guest.answers[0]["value"] == 2
        assert "host-tool-call cap (1) is exhausted" in guest.answers[1]["refusal"]
        assert result.stdout == "finished after a refusal"
        assert result.exit_code == 0

    def test_a_request_over_the_ceiling_is_refused_unread(self):
        limits = TransferLimits(max_bytes_per_file=256, max_total_bytes=4096, max_files=4)
        guest = _ScriptedGuest([("add", {"left": 1, "right": 1})], request_bytes=1024)
        _run(guest, HostToolRun(_registry(response_limits=limits)))
        assert "larger than the host will read" in guest.answers[0]["refusal"]

    def test_a_request_whose_size_the_backend_cannot_state_is_refused_unread(self):
        """A size the backend will not state fails closed, as the pull surface's rule says:
        the cap cannot be checked against a number that is not there."""
        reads: list[str] = []

        class _SizeUnknown(_ScriptedGuest):
            async def stat_file(self, path: str, *, working_directory: str):
                entry = await super().stat_file(path, working_directory=working_directory)
                if entry is not None and entry.path.endswith(".request.json"):
                    return SandboxEntry(path=entry.path, kind=entry.kind, size_bytes=None)
                return entry

            async def read_file(self, path: str, *, working_directory: str, max_bytes: int):
                reads.append(path)
                return await super().read_file(
                    path, working_directory=working_directory, max_bytes=max_bytes
                )

        guest = _SizeUnknown([("add", {"left": 1, "right": 1})])
        _run(guest, HostToolRun(_registry()))
        assert "larger than the host will read" in guest.answers[0]["refusal"]
        assert "value" not in guest.answers[0]
        assert not any(path.endswith(".request.json") for path in reads), (
            "a request of unknown size was read"
        )

    def test_a_request_that_is_not_json_is_refused(self):
        guest = _ScriptedGuest([("add", {"left": 1, "right": 1})], raw_request=b"{not json")
        _run(guest, HostToolRun(_registry()))
        assert "not valid JSON" in guest.answers[0]["refusal"]


class TestWhatAGuestCannotSpend:
    def test_a_replayed_request_is_never_answered_twice(self):
        """Sequential ids are the mechanism: the supervisor never looks back at an answered id.

        Inside one supervised run, which is the only place it means anything: the guest takes
        its answer, deletes it, rewrites the same request file, and keeps the program alive
        long enough for the supervisor to poll again. A supervisor scanning for *any* unanswered
        request would serve it a second time and spend a second dispatch on it.
        """
        dispatched: list[str] = []

        def counting(left: int, right: int) -> int:
            dispatched.append("add")
            return left + right

        stamped = sandbox_tool(source=SourceIntegrity.TRUSTED, sink=None, identity=Identity.APP)
        registry = HostToolRegistry()
        registry.register(stamped(counting), name="add")

        guest = _ScriptedGuest([("add", {"left": 1, "right": 1})], replay_after_answer=True)
        _run(guest, HostToolRun(registry))

        assert dispatched == ["add"], "a replayed id bought a second dispatch"
        assert guest.replayed, "the guest never got to replay — the test proved nothing"


class TestTheSupervisorsOwnBounds:
    def test_a_program_that_never_finishes_raises_with_what_it_printed(self):
        guest = _ScriptedGuest([], finish=False)
        guest.files[_LAYOUT.output] = b"halfway through"
        with pytest.raises(TimeoutError, match="halfway through"):
            _run(guest, HostToolRun(_registry()), timeout=0.05)

    def test_the_transport_never_enumerates(self):
        """`list_dir` raises in the specimen: docker serves FILES_OUT and not FILES_LIST."""
        guest = _ScriptedGuest([("add", {"left": 1, "right": 1})])
        _run(guest, HostToolRun(_registry()))
        assert guest.answers[0]["value"] == 2

    def test_starting_the_program_spends_the_same_deadline_the_run_does(self):
        """A slow launcher must not hand supervision a second full timeout."""

        class _SlowToStart(_ScriptedGuest):
            async def exec(self, command, *, working_directory: str, timeout: float):
                # The launcher only. The kill and the cleanup are `exec`s too, and they run
                # after the deadline by design — slowing those would measure the price of
                # reclaiming the run rather than the thing this test is named for.
                if not str(command).startswith(("kill", "rm ")):
                    await asyncio.sleep(0.15)
                return await super().exec(
                    command, working_directory=working_directory, timeout=timeout
                )

        guest = _SlowToStart([], finish=False)
        began = time.monotonic()
        with pytest.raises(TimeoutError):
            _run(guest, HostToolRun(_registry()), timeout=0.2)
        assert time.monotonic() - began < 0.35, "the launcher's time was not the run's time"

    def test_the_poll_never_sleeps_past_the_deadline(self):
        """A long interval against a short bound: the deadline is what has to win."""
        guest = _ScriptedGuest([], finish=False)
        began = time.monotonic()
        with pytest.raises(TimeoutError):
            asyncio.run(
                host_tool_calls_over_exec(
                    guest, HostToolRun(_registry()), _LAYOUT, timeout=0.05, poll_interval=5.0
                )
            )
        assert time.monotonic() - began < 1.0, "one poll interval outlasted the whole timeout"

    def test_the_launcher_upload_is_spent_from_the_same_budget(self):
        """`exec` gets what is left after writing the launcher, not another full timeout."""
        seen: list[float] = []

        class _SlowUpload(_ScriptedGuest):
            async def write_file(self, path: str, content, *, working_directory: str):
                await asyncio.sleep(0.1)
                await super().write_file(path, content, working_directory=working_directory)

            async def exec(self, command, *, working_directory: str, timeout: float):
                seen.append(timeout)
                return await super().exec(
                    command, working_directory=working_directory, timeout=timeout
                )

        guest = _SlowUpload([])
        _run(guest, HostToolRun(_registry()), timeout=1.0)
        assert seen and seen[0] < 1.0, "the upload's time was handed back to `exec`"

    def test_a_request_read_inside_the_bound_is_not_dispatched_after_it(self, monkeypatch):
        """The window a loop-top check cannot see: the read succeeds, then the bound passes.

        Every transport call is bounded, so a request can only be read while budget remains —
        and the clock can still cross the deadline between that read and the dispatch. The
        check that matters therefore sits beside the dispatch, and the guest below moves the
        clock at exactly that moment.
        """
        dispatched: list[str] = []

        def counting(left: int, right: int) -> int:
            dispatched.append("add")
            return left + right

        stamped = sandbox_tool(source=SourceIntegrity.TRUSTED, sink=None, identity=Identity.APP)
        registry = HostToolRegistry()
        registry.register(stamped(counting), name="add")

        clock = {"now": 0.0}

        class _TimePassesOnRead(_ScriptedGuest):
            async def read_file(self, path: str, *, working_directory: str, max_bytes: int):
                data = await super().read_file(
                    path, working_directory=working_directory, max_bytes=max_bytes
                )
                if path.endswith(".request.json"):
                    clock["now"] = 99.0
                return data

        module = sys.modules["maf_sandbox._host_tools_over_exec"]
        monkeypatch.setattr(module.time, "monotonic", lambda: clock["now"])

        guest = _TimePassesOnRead([("add", {"left": 1, "right": 1})], finish=False)
        with pytest.raises(TimeoutError):
            asyncio.run(
                host_tool_calls_over_exec(
                    guest, HostToolRun(registry), _LAYOUT, timeout=1.0, poll_interval=_FAST
                )
            )
        assert dispatched == [], "a request read inside the bound was dispatched after it"

    def test_the_poll_interval_actually_throttles_the_polling(self):
        """The interval is the only thing between a remote backend and a stat per loop tick.

        Refusing zero is not enough if the sleep itself goes missing: a run bounded at a
        quarter second polls a handful of times at 0.05s, and thousands of times unthrottled.
        """
        looks: list[str] = []

        class _CountsTheLooks(_ScriptedGuest):
            async def stat_file(self, path: str, *, working_directory: str):
                if path.endswith("program_exit_code"):
                    looks.append(path)
                return await super().stat_file(path, working_directory=working_directory)

        guest = _CountsTheLooks([], finish=False)
        with pytest.raises(SandboxProgramTimeout):
            _run(guest, HostToolRun(_registry()), timeout=0.25, poll=0.05)

        assert 0 < len(looks) <= 25, f"{len(looks)} marker looks in 0.25s at a 0.05s interval"

    def test_what_a_timeout_quotes_is_capped(self):
        """`output` and the message both quote the program, and both are bounded.

        Uncapped, a program that printed megabytes puts them whole into an exception a kind
        renders for a model — "already capped" is the attribute's own contract, and it has
        to hold on both give-up paths: the plain expiry, and the transport call that ran out.
        """

        class _StallsOnTheFirstMarkerLook(_ScriptedGuest):
            stalled = False

            async def stat_file(self, path: str, *, working_directory: str):
                if path.endswith("program_exit_code") and not self.stalled:
                    self.stalled = True
                    await asyncio.sleep(3600)
                return await super().stat_file(path, working_directory=working_directory)

        quiet = _ScriptedGuest([], finish=False)
        held_up = _StallsOnTheFirstMarkerLook([], finish=False)
        for guest in (quiet, held_up):
            guest.files[_LAYOUT.output] = b"x" * 3000
            with pytest.raises(SandboxProgramTimeout) as expired:
                _run(guest, HostToolRun(_registry()), timeout=0.05)

            assert expired.value.output == "x" * 2000
            assert "x" * 2000 in str(expired.value), "the quote lost its label or its text"
            assert "x" * 2001 not in str(expired.value), "the message quotes more than the cap"


def _branch(script: str, *, setsid: bool) -> str:
    """The launcher starts the program twice, once per guest.

    The `setsid` branch is what a guest that has it runs; the other is the fallback. Both
    are `nohup sh -c …`, so a test about the command reads whichever it means rather than
    the last line of the file.
    """
    lines = [line.strip() for line in script.splitlines()]
    started = [line for line in lines if line.endswith(" &") and "sh -c" in line]
    assert len(started) == 2, f"expected two branches, got {len(started)}"
    with_setsid, without = started
    assert with_setsid.startswith("setsid nohup ")
    assert without.startswith("nohup ")
    return with_setsid.removeprefix("setsid ") if setsid else without


class TestTheLauncher:
    """What the guest is actually asked to run, and whether a shell can read it."""

    def test_it_passes_one_argument_to_sh_however_the_path_reads(self):
        """Quoting fragments inside an already quoted `sh -c '…'` ends the outer argument."""
        layout = guest_run_layout("/maf-sandbox/work/run 1")
        command = _branch(launcher_script(layout), setsid=True)
        tokens = shlex.split(command.removesuffix(" &"))
        assert tokens[:3] == ["nohup", "sh", "-c"]
        assert layout.program in tokens[3], "the inner command did not survive as one argument"
        assert layout.output in tokens[3]

    def test_the_shims_directory_is_put_on_the_import_path(self):
        """Placement alone is a default a guest image can switch off.

        Under `PYTHONSAFEPATH` the interpreter prepends no directory of its own, and then only
        `PYTHONPATH` decides which `maf_host_tools` an import reaches — the guest's own file,
        on an image carrying the working directory there. Prepended rather than assigned, so an
        image keeps whatever entries it needs and none of them can outrank the shim.
        """
        script = launcher_script(guest_run_layout("/maf-sandbox/work/run-1"))
        assignment = next(line for line in script.splitlines() if line.startswith("PYTHONPATH="))

        assert assignment.startswith("PYTHONPATH='/maf-sandbox/work/run-1/host_tools'"), (
            "the shim's directory is not first, so an inherited entry can outrank it"
        )
        assert "$maf_kept" in assignment, "an image's own entries were dropped, not kept"
        assert '    /*) maf_kept="${maf_kept:+$maf_kept:}$maf_entry" ;;\n' in script, (
            "a relative inherited entry is not filtered"
        )
        run = "/maf-sandbox/work/run-1"
        assert f"    '{run}'|'{run}'/*) ;;\n" in script, (
            "an absolute inherited entry naming the run tree is not filtered — quoted so a "
            "run directory containing a glob character matches only itself"
        )
        assert "    */./*|*/../*|*//*|*/.|*/..) ;;\n" in script, (
            "a non-canonical entry is not filtered, so the run-tree test above compares "
            "spellings rather than directories and `/runs/./current/work` walks through it"
        )
        assert "\nkept=" not in script and "for entry in" not in script, (
            "an unprefixed name collides with one an image may already export"
        )
        # `set -f`, the `unset`, and the order of the two `case` branches are deliberately
        # *not* asserted here. All three are properties of where they sit relative to
        # something else, and a substring check cannot see order: move any of them and this
        # stays green while the behaviour is gone — the run-tree branch demoted below `/*)`
        # would never be reached. They are covered by running the launcher, in
        # `TestTheLauncherAgainstARealShell`.

    def test_the_interpreter_is_a_shell_word_like_every_path(self):
        """An interpreter path with a space is split unless it is quoted like the rest.

        `sh` takes `NAME=value cmd` as one command with variables set for it, so the
        interpreter is whatever follows the assignments. Found by skipping them rather than by
        counting them: how many the launcher sets is not this test's subject, and pinning the
        number here means adding one breaks a test that has nothing to say about it.
        """
        layout = guest_run_layout("/maf-sandbox/work/run-1")
        command = _branch(launcher_script(layout, "/opt/py 3.12/bin/python3"), setsid=False)
        inner = shlex.split(command.removesuffix(" &"))[3]

        words = shlex.split(inner)
        interpreter_at = next(index for index, word in enumerate(words) if "=" not in word)

        assert words[interpreter_at] == "/opt/py 3.12/bin/python3", (
            "the interpreter path did not survive as one word — a space split it, or an "
            f"assignment in front of it is not one: {words[: interpreter_at + 1]}"
        )

    def test_the_program_is_given_both_startup_variables(self):
        """Set through the environment because `interpreter` need not be CPython.

        Named here because the test above deliberately skips the assignments without reading
        them, so without this nothing would notice either going missing. Both are load-bearing
        and neither is visible in the run's result when it is absent: unbuffered output is
        what makes the output file a witness the timeout can quote, and no-user-site is what
        keeps `$PYTHONUSERBASE/lib/pythonX.Y/site-packages` — which `site` adds, and which an
        image can point into the run tree — from carrying a `sitecustomize` the guest wrote.
        """
        layout = guest_run_layout("/maf-sandbox/work/run-1")
        command = _branch(launcher_script(layout), setsid=False)
        inner = shlex.split(command.removesuffix(" &"))[3]

        words = shlex.split(inner)
        assignments = words[: next(index for index, word in enumerate(words) if "=" not in word)]

        assert sorted(assignments) == ["PYTHONNOUSERSITE=1", "PYTHONUNBUFFERED=1"], (
            f"the program's startup environment is not the two it needs: {assignments}"
        )


def _reap(pid_file: Path) -> None:
    """Stop the detached program a real-launcher test started, if it is still there.

    `nohup` means the process this test can see is not the process it made, so cleanup has to
    go by the pid the program recorded. Best effort by nature: it may have stopped on its own
    between the check and the signal, and that race is the ordinary outcome rather than a
    failure.
    """
    if not pid_file.exists():
        return
    for _ in range(40):
        try:
            os.kill(int(pid_file.read_text(encoding="utf-8")), signal.SIGTERM)
        except (OSError, ValueError):
            return
        time.sleep(0.05)


class TestTheLauncherAgainstARealShell:
    """The launcher is a shell script, and some of what it promises only a shell can show."""

    @pytest.mark.skipif(shutil.which("sh") is None, reason="needs a POSIX shell")
    @pytest.mark.skipif(os.pathsep != ":", reason="the launcher's path handling is POSIX")
    def test_the_guest_sees_the_inherited_path_filtered_and_otherwise_untouched(
        self, tmp_path: Path
    ):
        """What the launcher builds, read back from the environment the program actually gets.

        Ordering is the whole property and no substring check can see it: `set -f` moved after
        the loop globs, an `unset` moved before the assignment empties the result, and either
        rearrangement leaves every recognisable line of the script in place. So this runs it.

        Verbatim matters as much as filtered — Python does not glob `PYTHONPATH`, so an
        inherited `/opt/plugins/*` has to arrive as those characters.

        The glob entry sits outside the run tree because entries inside it are now dropped
        whether or not they are absolute, so a glob written under `work` would be filtered
        before the no-globbing claim could be tested. `{directory}-sibling` guards the
        boundary that move introduces: a prefix of the run directory is not inside it.
        """
        directory = (tmp_path / "run").as_posix()
        outside = (tmp_path / "outside").as_posix()
        served, work = f"{directory}/host_tools", f"{directory}/work"
        pathlib.Path(served).mkdir(parents=True, exist_ok=True)
        pathlib.Path(work).mkdir(parents=True, exist_ok=True)
        pathlib.Path(outside).mkdir(parents=True, exist_ok=True)
        # Something for a glob to catch, in the directory the pattern names.
        pathlib.Path(outside, "caught.py").write_text("", encoding="utf-8")
        pathlib.Path(served, "program.py").write_text(
            "import os\nprint(os.environ.get('PYTHONPATH', '<unset>'))\n"
            "print('leaked:', sorted(k for k in os.environ if k.startswith('maf_')))\n",
            encoding="utf-8",
        )
        layout = GuestRunLayout(
            directory=directory,
            work=work,
            program=f"{served}/program.py",
            shim=f"{served}/{SHIM_MODULE}",
            launcher=f"{served}/run_program.sh",
            calls=f"{served}/{CALLS_DIRECTORY}",
            output=f"{served}/program_output.txt",
            exit_code=f"{served}/program_exit_code",
            pid=f"{served}/program_pid",
        )
        pathlib.Path(layout.launcher).write_text(
            launcher_script(layout, sys.executable), encoding="utf-8"
        )

        subprocess.run(
            ["sh", layout.launcher],
            cwd=directory,
            capture_output=True,
            text=True,
            timeout=60,
            check=True,
            # `maf_kept` exported by the "image": without it the leak assertion below is
            # vacuous, because a plain shell assignment never reaches a child's environment
            # and the launcher's leftovers could only ever be visible through a name that was
            # already exported. This is the one arrangement where the `unset` does work.
            env={
                **os.environ,
                "PYTHONPATH": (
                    f"/image/libs:{outside}/*:{work}:{directory}:{directory}-sibling:rel:"
                ),
                "maf_kept": "the image's own",
            },
        )
        marker = pathlib.Path(layout.exit_code)
        for _ in range(200):
            if marker.exists():
                break
            time.sleep(0.05)

        seen, leaked = pathlib.Path(layout.output).read_text(encoding="utf-8").splitlines()[:2]

        assert seen == f"{served}:/image/libs:{outside}/*:{directory}-sibling", (
            "the guest's path is not the shim's directory followed by the absolute entries "
            "from outside the run tree, unchanged — a glob was expanded, an entry was dropped "
            "or kept wrongly, or the order moved"
        )
        assert leaked == "leaked: []", (
            "the launcher's own value for maf_kept reached the guest, where the image's "
            "exported variable of that name used to be"
        )

    @pytest.mark.skipif(shutil.which("sh") is None, reason="needs a POSIX shell")
    @pytest.mark.skipif(
        os.pathsep != ":",
        reason="the launcher splits PYTHONPATH on ':' and tests entries for a leading '/', "
        "which is the guest it targets; a Windows interpreter parses neither that way",
    )
    @pytest.mark.parametrize(
        ("inherited", "hook_in"),
        [
            ({"PYTHONPATH": "."}, ""),
            ({"PYTHONPATH": "<work>"}, ""),
            # The `/./` has to fall *inside* the run directory's own spelling. Put it after,
            # as `<work>` spelled `<run>/./work`, and the prefix branch matches it anyway —
            # `'<run>'/*` accepts `./work` for its `*` — so the case would pass with the
            # canonical branch deleted and prove nothing.
            ({"PYTHONPATH": "<tmp>/./run/work"}, ""),
            ({"PYTHONUSERBASE": "<work>"}, _USER_SITE_UNDER_BASE),
        ],
        ids=["relative", "absolute-in-tree", "non-canonical-alias", "user-base"],
    )
    def test_an_inherited_variable_cannot_reach_into_the_run(
        self, tmp_path: Path, inherited: dict[str, str], hook_in: str
    ):
        """The launcher's own filtering, against a real `sh` and a real interpreter.

        `site` imports `sitecustomize` before the program runs, from anywhere on the startup
        path, and such a hook can seed `sys.modules` outright — no amount of ordering between
        the shim and the guest's files prevents it, because the import never reaches a file.
        So every inherited way of naming the run tree has to be closed, and the four here are
        the ways that exist: a relative entry, resolved against the working directory the
        launcher just changed to the guest's own; an absolute one, for a host that places runs
        at a path an image can predict; a non-canonical spelling of either, which is a
        different string to a textual filter and the same directory to the kernel; and
        `PYTHONUSERBASE`, which reaches startup through `site` rather than through the path.

        Parametrised rather than duplicated so the four cannot drift: they are one guarantee,
        and a fix for any one of them must not reopen another. Each case also asserts that an
        entry from *outside* the tree survives, so a filter that passes by dropping everything
        fails here.

        The `user-base` case asserts the interpreter's own no-user-site flag rather than only
        which shim won, because `site` disables user packages inside a virtual environment and
        this suite runs in one: the planted hook cannot win here however the launcher behaves,
        so a test that only watched for it would be green with `PYTHONNOUSERSITE` deleted. The
        flag is observable either way, and it is what the launcher is actually responsible for.
        """
        directory = (tmp_path / "run").as_posix()
        served, work = f"{directory}/host_tools", f"{directory}/work"
        # Outside the run tree, so its survival is a real claim rather than one the launcher
        # would satisfy anyway by prepending the shim's own directory.
        outside = (tmp_path / "outside").as_posix()
        pathlib.Path(served).mkdir(parents=True, exist_ok=True)
        pathlib.Path(work).mkdir(parents=True, exist_ok=True)
        pathlib.Path(outside).mkdir(parents=True, exist_ok=True)
        pathlib.Path(served, SHIM_MODULE).write_text("SPEAKING = 'the shim'\n", encoding="utf-8")
        hook = pathlib.Path(work, hook_in)
        hook.mkdir(parents=True, exist_ok=True)
        pathlib.Path(hook, "sitecustomize.py").write_text(
            "import sys, types\n"
            "m = types.ModuleType('maf_host_tools')\n"
            "m.SPEAKING = 'the guest'\n"
            "sys.modules['maf_host_tools'] = m\n",
            encoding="utf-8",
        )
        pathlib.Path(served, "program.py").write_text(
            "import maf_host_tools, os, sys\n"
            "print('speaking:', maf_host_tools.SPEAKING)\n"
            "print('user-site-off:', bool(sys.flags.no_user_site))\n"
            # Everything the filter kept beyond the shim's own directory, one per line.
            "print(*('kept: ' + e for e in os.environ['PYTHONPATH'].split(':')[1:]), sep='\\n')\n",
            encoding="utf-8",
        )
        layout = GuestRunLayout(
            directory=directory,
            work=work,
            program=f"{served}/program.py",
            shim=f"{served}/{SHIM_MODULE}",
            launcher=f"{served}/run_program.sh",
            calls=f"{served}/{CALLS_DIRECTORY}",
            output=f"{served}/program_output.txt",
            exit_code=f"{served}/program_exit_code",
            pid=f"{served}/program_pid",
        )
        pathlib.Path(layout.launcher).write_text(
            launcher_script(layout, sys.executable), encoding="utf-8"
        )
        hostile = {
            name: value.replace("<work>", work)
            .replace("<run>", directory)
            .replace("<tmp>", tmp_path.as_posix())
            for name, value in inherited.items()
        }
        # The survivor rides on `PYTHONPATH` in every case, behind the hostile entry when that
        # is where the hostile entry lives, so the second assertion means one thing throughout.
        hostile["PYTHONPATH"] = ":".join(filter(None, [hostile.get("PYTHONPATH"), outside]))

        subprocess.run(
            ["sh", layout.launcher],
            cwd=directory,
            capture_output=True,
            text=True,
            timeout=60,
            check=True,
            # What an image does, not what this run does: the hostile variable under test,
            # and an entry from outside the tree that must survive alongside the shim's.
            env={**os.environ, **hostile, "PYTHONSAFEPATH": "1"},
        )
        marker = pathlib.Path(layout.exit_code)
        for _ in range(200):
            if marker.exists():
                break
            time.sleep(0.05)

        printed = pathlib.Path(layout.output).read_text(encoding="utf-8").splitlines()
        said = dict(line.split(": ", 1) for line in printed if not line.startswith("kept: "))
        kept = [line.removeprefix("kept: ") for line in printed if line.startswith("kept: ")]

        assert said.get("speaking") == "the shim", (
            f"a startup hook in the work directory won: {printed}"
        )
        assert said.get("user-site-off") == "True", (
            "the interpreter was started with user site-packages enabled, so a hook under "
            f"$PYTHONUSERBASE would be imported before the program: {printed}"
        )
        assert kept == [outside], (
            "the entry from outside the run tree did not survive the filter, so the launcher "
            f"is dropping more than the run's own directories: {kept!r}"
        )

    @pytest.mark.skipif(shutil.which("sh") is None, reason="needs a POSIX shell")
    def test_a_work_directory_that_cannot_be_entered_stops_the_run(self, tmp_path: Path):
        """`sh` does not stop on a failed command, so the `cd` has to say so itself.

        Unguarded, a failed `mkdir`/`cd` pair leaves the program running wherever the launcher
        was exec'd: artifacts land where no kind collects them, the exit marker still appears,
        and the run reports success. A non-zero launcher is already handled — `host_tool_calls_over_exec`
        turns it into "the launcher did not start the program" — so failing loudly is free.
        """
        directory = tmp_path.as_posix()
        served = f"{directory}/host_tools"
        pathlib.Path(served).mkdir(parents=True, exist_ok=True)
        # A file where the work directory belongs: `mkdir -p` cannot make one over it.
        pathlib.Path(directory, "work").write_text("in the way", encoding="utf-8")

        layout = GuestRunLayout(
            directory=directory,
            work=f"{directory}/work",
            program=f"{served}/program.py",
            shim=f"{served}/{SHIM_MODULE}",
            launcher=f"{served}/run_program.sh",
            calls=f"{served}/{CALLS_DIRECTORY}",
            output=f"{served}/program_output.txt",
            exit_code=f"{served}/program_exit_code",
            pid=f"{served}/program_pid",
        )
        pathlib.Path(layout.program).write_text("open('escaped', 'w').close()\n", encoding="utf-8")
        pathlib.Path(layout.launcher).write_text(
            launcher_script(layout, sys.executable), encoding="utf-8"
        )

        started = subprocess.run(
            ["sh", layout.launcher], cwd=directory, capture_output=True, text=True, timeout=60
        )

        assert started.returncode != 0, "a launcher that could not enter the work directory ran on"
        assert not pathlib.Path(directory, "escaped").exists(), "the program ran somewhere else"
        assert not pathlib.Path(layout.exit_code).exists(), "a relocated run recorded an exit code"

    @pytest.mark.skipif(shutil.which("sh") is None, reason="needs a POSIX shell")
    def test_a_guest_file_named_like_the_shim_is_not_the_module_the_program_imports(
        self, tmp_path: Path
    ):
        """A file a model names cannot become the module the program imports.

        Asked of a real interpreter, because the guarantee is the runtime's: `sys.path[0]` is
        the *script's* directory and a script's working directory is never added at all, so a
        program run from beside the shim cannot reach a same-named file among the model's own.
        Placement is not the whole defence — `PYTHONSAFEPATH` switches that default off, which
        the launcher answers by putting the shim's directory on `PYTHONPATH` as well.
        """
        directory = tmp_path.as_posix()
        served, work = f"{directory}/host_tools", f"{directory}/work"
        pathlib.Path(served).mkdir(parents=True, exist_ok=True)
        pathlib.Path(work).mkdir(parents=True, exist_ok=True)

        pathlib.Path(served, SHIM_MODULE).write_text("SPEAKING = 'the shim'\n", encoding="utf-8")
        # What a model can put in its own working directory, under the one name that matters.
        pathlib.Path(work, SHIM_MODULE).write_text("SPEAKING = 'the guest'\n", encoding="utf-8")
        pathlib.Path(served, "program.py").write_text(
            "import maf_host_tools\nprint(maf_host_tools.SPEAKING)\n", encoding="utf-8"
        )

        # A startup hook is stronger than a same-named module: `site` imports `sitecustomize`
        # before the program runs, so one on the path can seed `sys.modules` and the import
        # never reaches a file at all. Only reachable through a *relative* inherited entry,
        # which is why the launcher drops those — planted here so that stays true.
        pathlib.Path(work, "sitecustomize.py").write_text(
            "import sys, types\n"
            "m = types.ModuleType('maf_host_tools')\n"
            "m.SPEAKING = 'the guest'\n"
            "sys.modules['maf_host_tools'] = m\n",
            encoding="utf-8",
        )

        for label, environment in (
            # What the launcher sets on an ordinary image: the shim's directory prepended.
            ("the default path", {"PYTHONPATH": served}),
            # `PYTHONSAFEPATH` stops the interpreter prepending the *script's* directory, so
            # placement alone stops defending and the path is all that is left.
            ("a safe-path guest", {"PYTHONSAFEPATH": "1", "PYTHONPATH": served}),
        ):
            spoke = subprocess.run(
                [sys.executable, f"{served}/program.py"],
                cwd=work,
                capture_output=True,
                text=True,
                timeout=60,
                env={**os.environ, **environment},
            )

            assert spoke.returncode == 0, f"{label}: {spoke.stderr}"
            assert spoke.stdout.strip() == "the shim", (
                f"{label}: a guest-named file became the host's module"
            )

    @pytest.mark.skipif(shutil.which("sh") is None, reason="needs a POSIX shell")
    def test_a_program_that_prints_and_then_hangs_leaves_its_output_readable(self, tmp_path: Path):
        """The timeout quotes this file, so what has not reached it does not exist.

        CPython block-buffers stdout when it is not a terminal, and the launcher redirects to a
        file. A few hundred bytes printed by a program that then wedges sit in its own memory
        until it exits — which, being wedged, it does not. The supervisor's "Output so far"
        would quote an empty file at exactly the moment a reader most needs it.

        A real `sh` and a real interpreter, because buffering is the runtime's behaviour and a
        double cannot have it.

        The program stops on a file rather than a signal and bounds itself besides; see
        `_reap` for why cleanup cannot simply kill what it started.
        """
        directory = tmp_path.as_posix()
        # Hand-built rather than through `guest_run_layout`: a Windows `tmp_path` is not a
        # POSIX absolute path, and the constructor is right to refuse one. The shape is the
        # constructor's, checked against it by `TestTheLayoutsOwnPromise`.
        served = f"{directory}/host_tools"
        layout = GuestRunLayout(
            directory=directory,
            work=f"{directory}/work",
            program=f"{served}/program.py",
            shim=f"{served}/{SHIM_MODULE}",
            launcher=f"{served}/run_program.sh",
            calls=f"{served}/{CALLS_DIRECTORY}",
            output=f"{served}/program_output.txt",
            exit_code=f"{served}/program_exit_code",
            pid=f"{served}/program_pid",
        )
        pathlib.Path(served).mkdir(parents=True, exist_ok=True)
        stop = tmp_path / "stop"
        pathlib.Path(layout.program).write_text(
            "\n".join(
                (
                    "import os, time",
                    f"open({str(tmp_path / 'pid')!r}, 'w').write(str(os.getpid()))",
                    "print('the part before the wedge')",
                    # Wedged for the observation, and mortal anyway: cleanup can be skipped
                    # by a killed test run, and nothing else would ever stop this.
                    "for _ in range(240):",
                    f"    if os.path.exists({str(stop)!r}):",
                    "        break",
                    "    time.sleep(0.25)",
                    "",
                )
            ),
            encoding="utf-8",
        )
        pathlib.Path(layout.launcher).write_text(
            launcher_script(layout, sys.executable.replace("\\", "/")), encoding="utf-8"
        )

        subprocess.run(  # noqa: S603 - a shell script this test just wrote
            [shutil.which("sh") or "sh", layout.launcher],
            stdout=subprocess.DEVNULL,
            timeout=30,
            check=False,
        )
        try:
            output = pathlib.Path(layout.output)
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                if output.exists() and output.read_text(encoding="utf-8").strip():
                    break
                time.sleep(0.05)
            assert output.exists(), "the launcher never created the output file"
            assert "the part before the wedge" in output.read_text(encoding="utf-8"), (
                "the program's output was still in its own buffer when the deadline would have "
                "quoted this file"
            )
        finally:
            stop.write_text("", encoding="utf-8")
            _reap(tmp_path / "pid")

    @pytest.mark.skipif(shutil.which("sh") is None, reason="needs a POSIX shell")
    def test_the_launcher_leaves_the_programs_own_ending_behind(self, tmp_path: Path):
        """The two facts the supervisor reads are the program's own, made where it says.

        The exit code must be what the program exited with, not what the launcher wishes it
        had. Stderr belongs in the output file — `ExecResult.stderr` is the host's channel
        on this transport, so a guest's complaints have nowhere else to land. And a relative
        artifact must land in the work directory, which is the only place a kind collects.
        """
        directory = tmp_path.as_posix()
        served, work = f"{directory}/host_tools", f"{directory}/work"
        pathlib.Path(served).mkdir(parents=True, exist_ok=True)
        layout = GuestRunLayout(
            directory=directory,
            work=work,
            program=f"{served}/program.py",
            shim=f"{served}/{SHIM_MODULE}",
            launcher=f"{served}/run_program.sh",
            calls=f"{served}/{CALLS_DIRECTORY}",
            output=f"{served}/program_output.txt",
            exit_code=f"{served}/program_exit_code",
            pid=f"{served}/program_pid",
        )
        pathlib.Path(layout.program).write_text(
            "import sys\n"
            "print('to stdout')\n"
            "print('to stderr', file=sys.stderr)\n"
            "open('artifact.txt', 'w').close()\n"
            "sys.exit(3)\n",
            encoding="utf-8",
        )
        pathlib.Path(layout.launcher).write_text(
            launcher_script(layout, sys.executable.replace("\\", "/")), encoding="utf-8"
        )

        subprocess.run(  # noqa: S603 - a shell script this test just wrote
            ["sh", layout.launcher], cwd=directory, capture_output=True, timeout=60, check=True
        )
        marker = pathlib.Path(layout.exit_code)
        deadline = time.monotonic() + 20
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.05)

        assert marker.read_text(encoding="utf-8") == "3", "the exit code is not the program's"
        printed = pathlib.Path(layout.output).read_text(encoding="utf-8")
        assert "to stdout" in printed
        assert "to stderr" in printed, "the guest's stderr went where the host never reads"
        assert pathlib.Path(work, "artifact.txt").exists(), (
            "the program did not run in the work directory"
        )
        assert not pathlib.Path(directory, "artifact.txt").exists(), (
            "a relative artifact escaped the work directory"
        )


class TestWhatAWrapperCanTakeAway:
    @staticmethod
    def _wrappers(names: set[str]) -> set[str]:
        """The tool wrappers in the generated module, told apart from the shim's own defs.

        Parsed rather than searched: the shim defines `call` itself, so a substring test
        cannot tell a wrapper named `call` from the machinery it would have replaced.
        """
        tree = ast.parse(host_tool_shim(names))
        defined = {node.name for node in tree.body if isinstance(node, ast.FunctionDef)}
        return defined - {"call", "_claim", "_publish"}

    def test_a_tool_named_after_one_of_the_shims_locals_still_gets_a_wrapper(self):
        """A wrapper is a module-level `def`, so it cannot reach a parameter or a temporary.

        `name`, `request` and `payload` are the shim's own locals and perfectly good tool
        names. Reserving them costs a tool its convenience function and buys nothing.
        """
        locals_of_the_shim = {"name", "request", "payload", "identifier", "response"}
        assert self._wrappers(locals_of_the_shim) == locals_of_the_shim

    def test_a_tool_named_after_something_a_wrapper_would_replace_gets_none(self):
        """Module bindings, and the builtins the shim reaches for from inside them.

        `open` is the one that would hurt: `call` uses it to stage a request, so a wrapper
        by that name breaks every dispatch rather than only its own.

        The set is derived, so it tracks the shim: a name the shim stops reading is a name a
        tool may have, and the second assertion below is that behaviour rather than an
        oversight.
        """
        assert self._wrappers({"call", "json", "os", "open", "_claim", "_CALLS", "time"}) == set()
        assert self._wrappers({"int"}) == {"int"}, "a name the shim stopped using stays reserved"


class TestTheGuestsPatience:
    def test_the_call_timeout_is_the_callers_to_set(self):
        """It must not be shorter than the run's bound, so a long run has to be able to say so.

        Give up first and the guest is wrong twice: a dispatch the supervisor is still running
        goes on to act, while the program has been told the host never answered.
        """
        assert "_TIMEOUT = 900.0" in host_tool_shim(call_timeout=900.0)
        assert "_TIMEOUT = 300.0" in host_tool_shim(), "the default moved without the docs"

    @pytest.mark.parametrize("patience", [0.0, -1.0, float("nan"), float("inf")])
    def test_a_patience_that_is_not_finite_and_positive_is_refused(self, patience: float):
        """Zero gives up before the supervisor's first poll; `nan` and `inf` never start.

        Neither is caught by a range check — both are `<= 0`-false — and formatting either
        into the source emits a bare `nan` or `inf`, which is not a name the guest's module
        can resolve. The shim would fail at import, before any call is made.
        """
        with pytest.raises(ValueError, match="call_timeout"):
            host_tool_shim(call_timeout=patience)

    def test_the_generated_shim_imports_at_every_allowed_patience(self, tmp_path: Path):
        """The check above is only worth having if what it admits actually loads.

        The int (``120``) is the real caller's type — the codeact kind passes
        ``exec_timeout_seconds``, an ``int`` — and it must render as a plain literal the guest
        can import, which is why the override is rendered with ``str`` rather than ``repr``.
        """
        for index, patience in enumerate((0.001, 1.0, 86_400.0, 120)):
            module_path = tmp_path / f"patience_{index}.py"
            module_path.write_text(host_tool_shim(call_timeout=patience), encoding="utf-8")
            spec = importlib.util.spec_from_file_location(f"patience_{index}", module_path)
            assert spec and spec.loader
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            assert module._TIMEOUT == patience


class TestTheGuestSourceMatchesTheHost:
    """The guest file carries `_CALLS`/`_TIMEOUT`/`_POLL` as literals so it is valid Python on its
    own and can be imported and checked here. These pin those literals to the host's own
    constants, so a change to one side that forgets the other fails now rather than at a run where
    the guest writes requests where the supervisor is not looking.
    """

    def test_the_guest_literals_match_the_host_constants(self):
        import maf_sandbox._guest.maf_host_tools as guest_shim
        from maf_sandbox._shim import _GUEST_CALL_TIMEOUT, _GUEST_POLL_SECONDS

        # The guest joins the name onto its own directory; it is the trailing name that has to
        # agree with the subdirectory the supervisor builds and polls.
        assert os.path.basename(guest_shim._CALLS) == CALLS_DIRECTORY
        assert guest_shim._POLL == _GUEST_POLL_SECONDS
        assert guest_shim._TIMEOUT == _GUEST_CALL_TIMEOUT


class TestTheGeneratedShim:
    """The guest half, run for real — the seam where a name mismatch would hide."""

    @staticmethod
    def _load(tmp_path: Path):
        source = host_tool_shim({"add"})
        module_path = tmp_path / SHIM_MODULE
        module_path.write_text(source, encoding="utf-8")
        spec = importlib.util.spec_from_file_location("maf_host_tools_under_test", module_path)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_a_call_writes_the_request_the_supervisor_polls_for(self, tmp_path: Path):
        module = self._load(tmp_path)
        answered: list[Any] = []

        def call_it() -> None:
            answered.append(module.add(left=2, right=3))

        caller = threading.Thread(target=call_it)
        caller.start()
        request = tmp_path / CALLS_DIRECTORY / "0001.request.json"
        deadline = time.monotonic() + 5
        while not request.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert request.exists(), "the shim wrote no request where the supervisor looks"
        assert json.loads(request.read_text(encoding="utf-8")) == {
            "id": "0001",
            "name": "add",
            "arguments": {"left": 2, "right": 3},
        }
        (tmp_path / CALLS_DIRECTORY / "0001.response.json").write_text(
            json.dumps({"value": 5}), encoding="utf-8"
        )
        caller.join(timeout=5)
        assert answered == [5]

    def test_a_refusal_raises_in_the_guest_with_the_sentence(self, tmp_path: Path):
        module = self._load(tmp_path)
        raised: list[str] = []

        def call_it() -> None:
            try:
                module.call("add", left=1, right=1)
            except module.HostToolError as refused:
                raised.append(str(refused))

        caller = threading.Thread(target=call_it)
        caller.start()
        response = tmp_path / CALLS_DIRECTORY / "0001.response.json"
        deadline = time.monotonic() + 5
        while not (tmp_path / CALLS_DIRECTORY / "0001.request.json").exists():
            assert time.monotonic() < deadline, "no request appeared"
            time.sleep(0.01)
        response.parent.mkdir(parents=True, exist_ok=True)
        response.write_text(json.dumps({"refusal": "Error: no."}), encoding="utf-8")
        caller.join(timeout=5)
        assert raised == ["Error: no."]

    def test_the_wrappers_are_convenience_and_not_authority(self, tmp_path: Path):
        source = host_tool_shim({"add", "not an identifier"})
        assert "def add(**arguments)" in source
        assert "not an identifier" not in source
        # `call` reaches anything; resolution is the registry's, host-side.
        assert "def call(name, **arguments)" in source

    def test_a_tool_named_like_a_keyword_does_not_break_the_shim(self):
        """`def class(...)` is a SyntaxError, and one bad wrapper takes every call with it."""
        source = host_tool_shim({"class", "lookup", "import"})
        compile(source, SHIM_MODULE, "exec")  # the whole point: it still parses
        assert "def lookup(" in source
        assert "def class(" not in source and "def import(" not in source

    def test_a_tool_cannot_shadow_the_shim_s_own_machinery(self):
        source = host_tool_shim({"call", "json", "_CALLS", "lookup"})
        assert source.count("def call(name, **arguments)") == 1
        assert "def json(" not in source
        assert "def _CALLS(" not in source
        assert "def lookup(" in source

    def test_the_shim_publishes_a_request_only_once_it_is_whole(self, tmp_path: Path):
        """`open` creates the file empty; a poll in that window refuses a call that was fine.

        Observed at the rename rather than by racing a watcher against it: what `os.replace`
        was handed proves the same property without a timing-sensitive test to go with it.
        """
        source = host_tool_shim()
        module_path = tmp_path / SHIM_MODULE
        module_path.write_text(source, encoding="utf-8")
        spec = importlib.util.spec_from_file_location("maf_host_tools_atomic", module_path)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        renames: list[tuple[str, str, str]] = []
        real_replace = module.os.replace

        def recording_replace(source_path, destination):
            # What the destination would have looked like to a poll, and what was staged.
            renames.append(
                (
                    str(source_path),
                    str(destination),
                    pathlib.Path(source_path).read_text(encoding="utf-8"),
                )
            )
            real_replace(source_path, destination)

        module.os.replace = recording_replace
        try:
            # Answer immediately so `call` returns rather than waiting on a supervisor.
            calls = tmp_path / CALLS_DIRECTORY
            calls.mkdir(parents=True, exist_ok=True)
            (calls / "0001.response.json").write_text(json.dumps({"value": None}), encoding="utf-8")
            module.call("anything", padding="x" * 200_000)
        finally:
            module.os.replace = real_replace

        assert renames, "the request was published without an atomic rename"
        staged, published, contents = renames[0]
        assert staged.endswith(".part"), "the request was written straight to its final name"
        assert published.endswith("0001.request.json")
        assert json.loads(contents)["name"] == "anything", (
            "the staged file was incomplete at the moment it became visible"
        )

    def test_a_response_caught_mid_write_is_retried_rather_than_raised(self, tmp_path: Path):
        """A backend may make the supervisor's one write visible in pieces.

        Half a JSON document is a `ValueError`, and treating it as an answer kills a call
        that was about to succeed; the shim polls again instead, exactly as for a file that
        is not there yet.
        """
        module = self._load(tmp_path)
        calls = tmp_path / CALLS_DIRECTORY
        calls.mkdir(parents=True, exist_ok=True)
        response = calls / "0001.response.json"
        response.write_text('{"value": "who', encoding="utf-8")
        answered: list[Any] = []

        def call_it() -> None:
            answered.append(module.call("anything"))

        caller = threading.Thread(target=call_it)
        caller.start()
        request = calls / "0001.request.json"
        deadline = time.monotonic() + 5
        while not request.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert request.exists(), "the shim wrote no request"
        time.sleep(0.25)  # several poll intervals spent on the torn response
        response.write_text('{"value": "whole"}', encoding="utf-8")
        caller.join(timeout=5)

        assert answered == ["whole"], "a torn read was treated as the answer"


class TestTheWireFormatIsOneContract:
    """One wire format, two producers.

    The generated shim and the `_ScriptedGuest` double the supervisor suite runs against both
    write requests, independently, and nothing but `_shim_wire_contract` compares them. Driving
    both through its probes is what stops the double drifting from the shim it stands in for: a
    key renamed in one and not the other stops satisfying the shared contract, and the check goes
    red instead of green against a shape nothing in production emits.
    """

    def test_the_generated_shims_request_conforms(self, tmp_path: Path):
        """The real shim, run for real: what it writes is what the contract describes."""
        module = TestTheGeneratedShim._load(tmp_path)
        answered: list[Any] = []

        def call_it() -> None:
            answered.append(module.add(left=2, right=3))

        caller = threading.Thread(target=call_it)
        caller.start()
        request = tmp_path / CALLS_DIRECTORY / "0001.request.json"
        deadline = time.monotonic() + 5
        while not request.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert request.exists(), "the shim wrote no request"
        assert_request_conforms(request.name, request.read_bytes())
        (tmp_path / CALLS_DIRECTORY / "0001.response.json").write_text(
            json.dumps({"value": 5}), encoding="utf-8"
        )
        caller.join(timeout=5)
        assert answered == [5]

    def test_the_doubles_requests_and_responses_conform(self):
        """The double emits the same request shapes, and the supervisor's real responses match
        the response half of the same contract."""
        guest = _ScriptedGuest([("add", {"left": 1, "right": 2}), ("add", {"left": 3, "right": 4})])
        _run(guest, HostToolRun(_registry()))
        _, responses = assert_calls_conform(guest.files, expect_requests=2)
        assert responses >= 1, "the supervisor wrote no response for the double to read"

    def test_the_doubles_abandonment_conforms(self):
        """The `{id, abandoned}` shape — which the shim writes only on a publish failure — is
        exercised by the double here, and it is the same shape the contract admits."""
        guest = _ScriptedGuest([(_ABANDONED, {}), ("add", {"left": 1, "right": 2})])
        _run(guest, HostToolRun(_registry()))
        assert_calls_conform(guest.files, expect_requests=2)

    def test_the_probes_reject_a_renamed_key(self):
        """The teeth: a request with a key renamed — the exact drift this exists to catch —
        does not pass, so a producer that emitted it would fail rather than go green."""
        with pytest.raises(AssertionError):
            assert_request_conforms(
                "0001.request.json", b'{"id": "0001", "tool": "add", "arguments": {}}'
            )

    def test_the_probes_reject_an_id_that_does_not_match_the_name(self):
        """The id inside the payload and the number in the file name are one identifier."""
        with pytest.raises(AssertionError):
            assert_request_conforms(
                "0002.request.json", b'{"id": "0001", "name": "add", "arguments": {}}'
            )

    def test_the_count_tripwire_refuses_a_vacuous_pass(self):
        """A producer that wrote nothing must not satisfy the contract by having nothing to
        check — the expected count is what turns an empty run into a failure."""
        with pytest.raises(AssertionError):
            assert_calls_conform({}, expect_requests=1)


class TestNamesThatAreNotWhatTheyLookLike:
    """Identifier normalisation, where a wrapper name and a shim global can be the same name."""

    def test_a_fullwidth_spelling_cannot_shadow_the_machinery(self):
        """Python NFKC-normalises identifiers at compile time; `ｃａｌｌ` becomes `call`."""
        source = host_tool_shim({"\uff43\uff41\uff4c\uff4c", "lookup"})
        assert source.count("def call(name, **arguments)") == 1
        assert "\uff43\uff41\uff4c\uff4c" not in source
        assert "def lookup(" in source
        compile(source, SHIM_MODULE, "exec")

    def test_two_names_that_normalise_together_produce_one_wrapper(self):
        """Counted over every wrapper the module defines, normalised \u2014 not over one spelling.

        Counting one spelling proves nothing: `def \uff4c\uff4f\uff4f\uff4b\uff55\uff50(` and `def lookup(` are different
        substrings and the same global.
        """
        source = host_tool_shim({"lookup", "\uff4co\uff4fkup"})
        defined = [
            unicodedata.normalize("NFKC", node.name)
            for node in ast.parse(source).body
            if isinstance(node, ast.FunctionDef)
        ]
        assert sorted(defined) == sorted(set(defined)), f"two wrappers bind one name: {defined}"
        assert defined.count("lookup") == 1


class TestWhatTheSupervisorRefusesToParse:
    def test_a_deeply_nested_request_is_refused_rather_than_raised(self):
        """`json.loads` raises `RecursionError`, not `ValueError`, and it is under the size cap."""
        nested = ("[" * 60_000 + "]" * 60_000).encode("utf-8")
        limits = TransferLimits(max_bytes_per_file=1 << 20, max_total_bytes=1 << 20, max_files=8)
        guest = _ScriptedGuest([("add", {"left": 1, "right": 1})], raw_request=nested)
        _run(guest, HostToolRun(_registry(response_limits=limits)))
        assert "not valid JSON" in guest.answers[0]["refusal"]

    def test_a_request_that_is_not_utf8_is_refused_rather_than_repaired(self):
        """Decoded strictly, because a repaired request is one the host acts on wrongly.

        The byte is inside a JSON string, so replacement decoding leaves a document that
        parses: `add` would be called with `left` one character different from what the guest
        sent, and no one would ever know. There is nothing to salvage here — the guest wrote
        bytes it cannot have meant — so the answer is the refusal an unparseable request gets.
        """
        seen: list[Any] = []

        @sandbox_tool(source=SourceIntegrity.TRUSTED, sink=None, identity=Identity.APP)
        def echo(text: str) -> str:
            seen.append(text)
            return text

        registry = HostToolRegistry()
        registry.register(echo, name="echo")
        mangled = b'{"id": "0001", "name": "echo", "arguments": {"text": "caf\xe9"}}'

        guest = _ScriptedGuest([("echo", {"text": "cafe"})], raw_request=mangled)
        _run(guest, HostToolRun(registry))
        assert seen == [], f"the tool ran on repaired bytes: {seen}"
        assert "not valid JSON" in guest.answers[0]["refusal"]

    def test_a_request_that_is_json_but_not_an_object_is_refused(self):
        """`json.loads` happily returns a list, and `.get` on one crashes the supervisor.

        Like every other malformed request, it has to come back as a sentence: the guest
        cannot retry it either way, and the run must outlive it.
        """
        guest = _ScriptedGuest([("add", {"left": 1, "right": 1})], raw_request=b"[1, 2, 3]")
        _run(guest, HostToolRun(_registry()))
        assert "must be a JSON object" in guest.answers[0]["refusal"]
        assert "value" not in guest.answers[0]


class TestWhatTheDeadlineCovers:
    """The bound is on the run, and sandbox I/O is part of the run."""

    def test_a_stalled_backend_call_does_not_hold_the_supervisor(self):
        """A hung `stat_file` is what this bound is for: no guest, no exit marker, no end.

        The ceiling is the run's own bound plus the two graces a guest this slow can spend in
        full — the last look for the marker, and the one stopping the program gets to itself —
        with room for a loaded machine. What it rules out is the 3600s sleep below, and it is
        derived rather than tuned so that a third grace appearing has to be argued for here.
        """

        class _Stalled(_ScriptedGuest):
            async def stat_file(self, path: str, *, working_directory: str):
                await asyncio.sleep(3600)
                raise AssertionError("unreachable")

        run_bound = 0.2
        ceiling = run_bound + 2 * host_tools_over_exec._FINAL_READ_GRACE + 2.0
        began = time.monotonic()
        with pytest.raises(TimeoutError):
            _run(_Stalled([]), HostToolRun(_registry()), timeout=run_bound)
        assert time.monotonic() - began < ceiling, "a stalled transport call outlasted the bound"

    def test_an_empty_exit_marker_is_not_a_finished_run(self):
        """A redirection creates the marker empty; reading that as an exit loses the run."""
        guest = _ScriptedGuest([], finish=False)
        guest.files[_LAYOUT.exit_code] = b""
        guest.files[_LAYOUT.output] = b"still going"
        with pytest.raises(TimeoutError, match="still going"):
            _run(guest, HostToolRun(_registry()), timeout=0.1)


class TestTheLaunchersExitMarker:
    def test_the_exit_code_lands_by_rename(self):
        """The same reason the shim stages requests: a poll must not see a half-written file."""
        layout = guest_run_layout("/maf-sandbox/work/run-1")
        inner = shlex.split(_branch(launcher_script(layout), setsid=True).removesuffix(" &"))[3]
        assert f"{layout.exit_code}.part" in inner, "the exit code is written straight to its name"
        assert inner.rstrip().endswith(f"mv '{layout.exit_code}.part' '{layout.exit_code}'")


class TestThePathsTheSupervisorPasses:
    def test_every_pull_call_names_a_path_a_backend_accepts(self):
        """The double resolves through `confine_resolve_guest_path`, so a refused path fails here too.

        Absolute paths inside the working directory are what the layout holds, and
        `posixpath.join` makes those resolve to themselves — which is why both shipped
        backends serve them. This keeps that agreement under test rather than assumed.
        """
        seen: list[str] = []

        class _RecordingPaths(_ScriptedGuest):
            async def stat_file(self, path: str, *, working_directory: str):
                seen.append(path)
                return await super().stat_file(path, working_directory=working_directory)

        guest = _RecordingPaths([("add", {"left": 1, "right": 1})])
        _run(guest, HostToolRun(_registry()))
        assert seen, "nothing was stat-ed"
        for path in seen:
            assert confine_resolve_guest_path(path, _LAYOUT.directory).startswith(_LAYOUT.directory)


class TestTheResponseCeiling:
    """What the run is charged is the bytes that cross, framing included.

    The transport declares its envelope to `dispatch` rather than checking the total after
    the fact, so all three of these are one question asked from three sides: is the size the
    ledger reserves, refuses on, and commits the size of the file the guest receives?
    """

    @staticmethod
    def _capped(limits: TransferLimits, tool: Any, name: str) -> HostToolRegistry:
        stamped = sandbox_tool(source=SourceIntegrity.TRUSTED, sink=None, identity=Identity.APP)
        registry = HostToolRegistry(response_limits=limits)
        registry.register(stamped(tool), name=name)
        return registry

    def test_a_value_that_fits_only_unwrapped_is_refused(self):
        """`dispatch` caps what crosses, and `{"value": …}` crosses with the payload.

        The ceiling has to clear the *request* as well — it is the same number in both
        directions — so the value is sized to fit the cap on its own and to overflow it once
        the framing is around it.
        """
        payload = "x" * 80

        def long_answer() -> str:
            return payload

        registry = self._capped(
            TransferLimits(
                max_bytes_per_file=len(payload) + 5,  # fits `"xxx…"`, not `{"value": "xxx…"}`
                max_total_bytes=4096,
                max_files=4,
            ),
            long_answer,
            "long_answer",
        )

        guest = _ScriptedGuest([("long_answer", {})])
        _run(guest, HostToolRun(registry))
        answered = guest.answers[0]
        assert "value" not in answered, "a response longer than the ceiling was written"
        assert "per-response cap allows" in answered["refusal"]

    def test_a_response_refused_for_its_size_leaves_the_budget_unspent(self):
        """The run pays for what it delivered, and a refusal delivered nothing.

        `max_total_bytes` is the payload's own size exactly: a run that had been charged for
        the refused value would have nothing left, and the second call — one byte of value —
        would be refused for a budget the guest never received.
        """
        payload = "x" * 80

        def long_answer() -> str:
            return payload

        def short_answer() -> int:
            return 1

        registry = self._capped(
            TransferLimits(
                max_bytes_per_file=len(payload) + 5,
                max_total_bytes=len(payload) + 2,  # room for the refused value, and no more
                max_files=4,
            ),
            long_answer,
            "long_answer",
        )
        stamped = sandbox_tool(source=SourceIntegrity.TRUSTED, sink=None, identity=Identity.APP)
        registry.register(stamped(short_answer), name="short_answer")

        guest = _ScriptedGuest([("long_answer", {}), ("short_answer", {})])
        _run(guest, HostToolRun(registry))
        assert "value" not in guest.answers[0], "the oversized value was delivered"
        assert guest.answers[1].get("value") == 1, (
            f"the refused response spent the run's budget: {guest.answers[1]}"
        )

    def test_the_framing_counts_against_the_runs_total_budget(self):
        """Two one-byte values fit any budget; two framed responses are two files of twelve.

        The per-file leg alone would let a guest spend the run's whole allowance in envelopes
        — the cheaper the values, the larger the share that never gets counted.
        """

        def zero() -> int:
            return 0

        registry = self._capped(
            TransferLimits(max_bytes_per_file=64, max_total_bytes=20, max_files=4),
            zero,
            "zero",
        )

        guest = _ScriptedGuest([("zero", {}), ("zero", {})])
        _run(guest, HostToolRun(registry))
        assert guest.answers[0].get("value") == 0, "the first framed response did not fit"
        assert "value" not in guest.answers[1], (
            f"two framed responses were written over a {20}-byte budget: {guest.answers[1]}"
        )
        assert "byte budget" in guest.answers[1]["refusal"]


class TestWhatSurvivesTheDeadline:
    def test_a_tool_that_finishes_late_still_has_its_answer_written(self):
        """The dispatch is allowed to overrun the bound; its record has to be allowed too.

        A tool that starts with a moment left and returns after the deadline has already
        acted. Bounding the response write at the run's remainder — zero, by then — throws
        the answer away and leaves exactly the effect-without-a-record this transport refuses
        to cause by cancelling.
        """

        @sandbox_tool(source=SourceIntegrity.TRUSTED, sink=None, identity=Identity.APP)
        async def slow() -> str:
            await asyncio.sleep(0.3)
            return "late"

        registry = HostToolRegistry()
        registry.register(slow, name="slow")
        guest = _ScriptedGuest([("slow", {})], finish=False)

        with pytest.raises(TimeoutError):
            _run(guest, HostToolRun(registry), timeout=0.1)

        written = guest.files.get(posixpath.join(_LAYOUT.calls, "0001.response.json"))
        assert written is not None, "the answer to a tool that had already acted was discarded"
        assert json.loads(written)["value"] == "late"

    def test_the_documented_overhead_is_what_the_constants_add_up_to(self):
        """The README quotes a total, and prose cannot notice a constant moving under it.

        Five graces stack on the worst path: the response write above, the shared last look at
        the marker and the output, the pid lookup, the signal, and the reclaim. A host sizes an
        outer deadline from that number, and one set too tight loses the
        `SandboxProgramTimeout` and cancels whatever dispatch is in flight — so a constant
        changed without the sentence is a caller's bug, not a stale document.
        """
        worst = (
            host_tools_over_exec._RESPONSE_WRITE_GRACE
            + 3 * host_tools_over_exec._FINAL_READ_GRACE
            + host_tools_over_exec._RECLAIM_GRACE
        )
        assert worst == 18.0, "the README's `timeout + 18s` no longer matches the constants"


class TestTheLayoutsOwnPromise:
    @pytest.mark.parametrize("directory", ["work/run-1", "", "run-1"])
    def test_a_run_directory_that_is_not_absolute_is_refused(self, directory: str):
        """`confine_resolve_guest_path` joins a relative one against itself, and nothing looks wrong.

        The requests then land under `work/run-1/work/run-1/`, where the supervisor is not
        polling — a run that simply never sees a call, with no error anywhere.
        """
        with pytest.raises(ValueError, match="absolute"):
            guest_run_layout(directory)

    @pytest.mark.parametrize("program", ["/etc/passwd", "sub/dir/p.py", "..", ""])
    def test_a_program_that_is_not_a_plain_file_name_is_refused(self, program: str):
        """Absolute discards the run directory outright; nested puts it where the shim is not.

        `posixpath.join("/run", "/etc/passwd")` is `/etc/passwd`, so the layout would name a
        program outside the directory every other path is inside.
        """
        with pytest.raises(ValueError, match="plain file name"):
            guest_run_layout("/maf-sandbox/work/run-1", program=program)

    @pytest.mark.parametrize(
        "program",
        ["program_output.txt", "run_program.sh", SHIM_MODULE, "program_exit_code.part"],
    )
    def test_a_program_named_after_the_layouts_own_files_is_refused(self, program: str):
        """Each collision breaks the run in its own way, and none of them announce themselves.

        `program_output.txt` is the launcher's redirection target, so the shell truncates the
        program before the interpreter opens it; the launcher and the shim are written over
        whatever the kind put there; and `program_exit_code.part` is where the launcher stages
        the exit code, so the program's own file is truncated to the exit digits and renamed
        away as it exits. Nothing in any of them points at the name that caused it.
        """
        with pytest.raises(ValueError, match="already uses"):
            guest_run_layout("/maf-sandbox/work/run-1", program=program)

    @pytest.mark.parametrize("program", ["program_exit_code", "host_tool_calls"])
    def test_the_marker_and_the_calls_directory_are_refused_as_program_names_too(
        self, program: str
    ):
        """The quiet halves of the same refusal.

        A program at the marker's name is read as a finished run by the supervisor's first
        poll, before the interpreter has even started; one at the calls directory's name
        leaves the shim's `makedirs` nowhere to put a request.
        """
        with pytest.raises(ValueError, match="already uses"):
            guest_run_layout("/maf-sandbox/work/run-1", program=program)

    @pytest.mark.parametrize("directory", [r"/work/run\1", r"/work\run"])
    def test_a_run_directory_the_backends_cannot_resolve_is_refused(self, directory: str):
        """Absolute is not the same as valid: `confine_resolve_guest_path` refuses a backslash.

        Without this the layout builds and every pull call raises instead — after the
        launcher has started a detached program that outlives the failure.
        """
        with pytest.raises(ValueError, match="backslash"):
            guest_run_layout(directory)

    def test_a_directory_python_cannot_be_told_about_is_refused(self):
        """`PYTHONPATH` separates on ':' and cannot quote one, and the shim's directory goes
        there. `/runs/job:slot` would reach the interpreter as `/runs/job` plus a *relative*
        `slot/host_tools`, resolved against the guest's own working directory — where a guest
        file at that path becomes the module the program imports."""
        with pytest.raises(ValueError, match="must not contain ':'"):
            guest_run_layout("/runs/job:slot")

    @pytest.mark.parametrize(
        "name",
        [
            "maf_host_tools.so",
            "maf_host_tools.pyc",
            "maf_host_tools.cpython-313-x86_64-linux-gnu.so",
        ],
    )
    def test_a_program_that_would_be_its_own_shim_is_refused(self, name: str):
        """The shim's own name is the one importable member of the transport's filenames, so it
        needs the stem rule rather than the exact-name refusal the others get.

        The three fail differently and both ways are dead. An extension is tried before source,
        so a `.so` twin *answers* the `import maf_host_tools` that is the program's own first
        act and the run dies on an invalid header. A `.pyc` twin does not answer it — sourceless
        bytecode ranks after source, and the real shim is beside it by construction — but a
        program the interpreter must load as bytecode is not a program, and the run dies on a
        bad magic number instead.
        """
        with pytest.raises(ValueError, match="the shim's own module name"):
            guest_run_layout("/maf-sandbox/work/run-1", program=name)

    def test_every_module_the_shim_imports_is_refused_as_a_program_name(self):
        """Derived from the shim rather than listed, so adding an import cannot outrun it.

        The program sits beside the shim and their directory is first on the path, so a
        program named for something the shim imports *is* what the shim gets — and the
        traceback names a missing attribute rather than the collision that caused it.
        """
        source = host_tool_shim(("a", "b"))
        tree = ast.parse(source)
        imported = {
            alias.name.split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        } | {
            node.module.split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        }
        assert imported, "the shim parsed to no imports, so this would prove nothing"

        for module in sorted(imported):
            with pytest.raises(ValueError, match="the generated shim imports"):
                guest_run_layout("/maf-sandbox/work/run-1", program=f"{module}.py")

    @pytest.mark.parametrize(
        "name",
        [
            "encodings.py",
            "site.py",
            "sitecustomize.py",
            "usercustomize.py",
            # Matched by stem, because the suffix decides only which loader answers. An
            # extension is tried before source and a sourceless `.pyc` after it, so a `.so`
            # twin outranks the `.py` above while a `.pyc` twin answers when no source is
            # there — which is this directory, where the kind writes one file.
            "encodings.pyc",
            "sitecustomize.pyc",
            "encodings.so",
            "site.cpython-313-x86_64-linux-gnu.so",
            # Reached during startup on a guest older than 3.11, whose stdlib is not frozen.
            # Listed rather than derived from `_STARTUP_STEMS`: a test that iterates the set
            # under test cannot notice a name being dropped from it, which is the mistake this
            # list exists to catch.
            "abc.py",
            "codecs.py",
            "genericpath.py",
            "io.py",
            "posixpath.py",
            "stat.py",
            "_collections_abc.py",
            "_sitebuiltins.py",
            "_bootlocale.py",
        ],
    )
    def test_a_program_the_interpreter_imports_on_its_way_up_is_refused(self, name: str):
        """The transport's directory is on the path from startup, not from when the script is
        found, so a program under one of these is reached during initialisation —
        `sitecustomize.py` running twice over, `encodings.py` ending the interpreter before it
        can say why."""
        with pytest.raises(ValueError, match="imports at startup"):
            guest_run_layout("/maf-sandbox/work/run-1", program=name)

    @pytest.mark.parametrize(
        "name",
        [
            # No loader suffix is `.backup.py`, so `import json` never probes this file. The
            # refusal used to split on the first dot and take it for `json`.
            "json.backup.py",
            "sitecustomize.old.py",
            "encodings.v2.py",
            "maf_host_tools.backup.py",
            # One dot, but no loader answers to the suffix — the same over-refusal wearing a
            # shorter name, and the reason the check is against a suffix set rather than
            # against "has exactly one dot".
            "json.txt",
            "site.sh",
            "maf_host_tools.custom",
            # No suffix at all: nothing imports it, and the launcher runs it by path.
            "program",
        ],
    )
    def test_a_dotted_name_no_loader_claims_is_allowed(self, name: str):
        """The refusal is on the module a file would answer to, not on its first component.

        `FileFinder` looks for a name plus one of its loaders' suffixes, so `json.py` and every
        extension spelling of `json` answer to `json` while `json.backup.py` answers to
        nothing. Refusing the second is refusing a name that cannot collide — a breaking
        validation wider than the collision behind it.
        """
        layout = guest_run_layout("/maf-sandbox/work/run-1", program=name)

        assert layout.program == f"/maf-sandbox/work/run-1/host_tools/{name}"

    @pytest.mark.parametrize(
        "name", ["json.abi3.so", "json.cpython-313-x86_64-linux-gnu.so", "json.anything.so"]
    )
    def test_a_tagged_extension_spelling_is_still_refused(self, name: str):
        """A suffix with an interior dot is always an extension one, and its tag belongs to the
        guest's interpreter rather than to this package.

        `json.anything.so` is in the list on purpose: it cannot be told from a real ABI tag
        without knowing the guest's, so it is refused too. That is the one place this still
        answers wider than the collision, and it is the safe direction — the alternative is
        letting a genuine `json.cpython-...-.so` through on an interpreter we did not predict.
        """
        with pytest.raises(ValueError, match="the generated shim imports"):
            guest_run_layout("/maf-sandbox/work/run-1", program=name)

    def test_a_directory_spelled_the_long_way_round_is_kept_the_short_way(self):
        """One directory, two spellings, is a difference waiting to matter.

        `confine_resolve_guest_path` normalises, so the pull calls address `/work/run-1` whatever the
        caller wrote — while the launcher's `cd` and every path the layout built would still
        carry the original. Keeping the normalised result is what makes those the same string.
        """
        layout = guest_run_layout("/work/missing/../run-1")

        assert layout.directory == "/work/run-1"
        assert layout.work == "/work/run-1/work"
        assert layout.program == "/work/run-1/host_tools/program.py"
        assert ".." not in layout.calls

    def test_what_a_model_can_name_and_what_the_transport_owns_are_two_directories(self):
        """Nothing a kind writes on a model's say-so goes near the transport's directory.

        The separation is the guarantee, so there is no name for a kind to get wrong —
        including the ones a lexical check misses, like a package directory or a compiled
        extension resolving ahead of the shim's source.
        """
        layout = guest_run_layout("/work/run-1")
        owned = (
            layout.program,
            layout.shim,
            layout.launcher,
            layout.calls,
            layout.output,
            layout.exit_code,
        )

        assert all(path.startswith(f"{layout.directory}/host_tools/") for path in owned)
        assert not any(path.startswith(f"{layout.work}/") for path in owned)
        assert layout.work != layout.directory, "the guest's own directory is the run's root"

    def test_the_paths_it_does_accept_are_all_inside_the_run_directory(self):
        """The promise the two checks above exist to keep."""
        layout = guest_run_layout("/maf-sandbox/work/run-2")
        for path in (layout.program, layout.shim, layout.launcher, layout.calls, layout.output):
            assert path.startswith("/maf-sandbox/work/run-2/")


class _Shifted:
    """`time`, as the supervisor sees it, with an offset a test can move."""

    def __init__(self, real, ahead) -> None:
        self._real = real
        self._ahead = ahead

    def monotonic(self) -> float:
        return self._real() + self._ahead["seconds"]


class TestWhatAFinishedRunIsAllowedToSay:
    @staticmethod
    def _lands_during_the_sleep(monkeypatch: pytest.MonkeyPatch, marker: bytes) -> _ScriptedGuest:
        """A guest whose marker appears in the interval the supervisor is asleep for.

        The clock is moved rather than waited on, and moved on the iteration's *last* call —
        the look for a request. Jumping earlier would expire the calls that come after it in
        the same iteration, and the run would reach the same answer through the handler
        instead of through the check at the top of the loop, which is the site under test.
        """
        # The jump is what ends the run, so a caller's `timeout` only has to outlast the
        # setup before it. Tight bounds here bought nothing and flaked on a stalled runner.
        ahead = {"seconds": 0.0}
        real = time.monotonic
        # Patched where the supervisor reads it, not globally: the event loop's own timers
        # keep the real clock, so the transport's bounds still mean seconds.
        monkeypatch.setattr(host_tools_over_exec, "time", _Shifted(real, ahead))

        class _LandsDuringTheSleep(_ScriptedGuest):
            async def stat_file(self, path: str, *, working_directory: str):
                if path.endswith(".request.json") and _LAYOUT.exit_code not in self.files:
                    self.files[_LAYOUT.output] = b"it finished quietly"
                    self.files[_LAYOUT.exit_code] = marker
                    ahead["seconds"] += 60.0
                return await super().stat_file(path, working_directory=working_directory)

        return _LandsDuringTheSleep([], finish=False)

    def test_a_program_that_finishes_at_the_wire_keeps_its_output(self):
        """The output read is the last thing left, and the run's remainder can be zero by then.

        Bounded at that remainder it expires, and the run comes back with its exit code and
        an empty stdout — correct about the program, silent about what it printed. The floor
        is what keeps the output as well as the code.
        """

        class _SlowFinalRead(_ScriptedGuest):
            async def read_file(self, path: str, *, working_directory: str, max_bytes: int):
                if path.endswith("program_output.txt"):
                    await asyncio.sleep(1.4)
                return await super().read_file(
                    path, working_directory=working_directory, max_bytes=max_bytes
                )

        # A second of bound against a read that takes longer: the read must overrun the
        # deadline and finish inside the grace, with enough headroom that a stalled runner
        # cannot expire the run before the marker is even read.
        guest = _SlowFinalRead([], output="it ran")
        result = _run(guest, HostToolRun(_registry()), timeout=1.0)

        assert result.exit_code == 0, "a finished run came back as something else"
        assert result.stdout == "it ran"

    def test_output_larger_than_one_response_may_be_still_comes_back_whole(self):
        """The output is capped at the run's total, not at the per-response ceiling.

        The program's stdout is not a tool response, and a program may print more than one
        response may return — capped at the per-file leg, this output would be dropped whole
        by a number chosen for something else.
        """
        limits = TransferLimits(max_bytes_per_file=64, max_total_bytes=4096, max_files=4)
        guest = _ScriptedGuest([], output="x" * 200)
        result = _run(guest, HostToolRun(_registry(response_limits=limits)))

        assert result.exit_code == 0
        assert result.stdout == "x" * 200
        assert result.stderr == ""

    def test_a_backends_own_failure_to_hand_over_output_keeps_the_exit_code(self):
        """The other `TimeoutError`: a backend's own, raised while the run still has time.

        `_within` passes one straight through — the diagnosis in it is worth more than a
        relabelling — but by then the marker has proved the program finished, and letting it
        escape says the opposite. Both facts survive: the exit code is returned, the sentence
        goes to `stderr`.
        """

        class _RefusesToHandItOver(_ScriptedGuest):
            async def read_file(self, path: str, *, working_directory: str, max_bytes: int):
                if path.endswith("program_output.txt"):
                    raise TimeoutError("a FIFO the service reports as a regular file")
                return await super().read_file(
                    path, working_directory=working_directory, max_bytes=max_bytes
                )

        guest = _RefusesToHandItOver([], output="unreachable", exit_code=7)
        result = _run(guest, HostToolRun(_registry()), timeout=1.0)

        assert result.exit_code == 7, "a finished run was reported as something else"
        assert result.stdout == ""
        assert "could not be read" in result.stderr
        # A client's timeout text is where an endpoint, a subscription or a request id lives,
        # and every kind renders this value for a model. The diagnosis belongs in the log; what
        # a caller is owed here is that the output is missing.
        assert "a FIFO the service reports as a regular file" not in result.stderr

    def test_a_marker_that_lands_in_the_last_poll_interval_is_still_found(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """The loop sleeps up to a whole interval, and a marker can land inside one.

        Nothing raises here and no read is slow — the run simply expires between one poll and
        the next, with the program already finished. Without a look on the way out, that wait
        is charged against the guest and its exit code is lost.
        """
        guest = self._lands_during_the_sleep(monkeypatch, b"4")
        result = _run(guest, HostToolRun(_registry()), timeout=5.0)

        assert result.exit_code == 4, "a run that finished inside the last interval was lost"
        assert result.stdout == "it finished quietly"

    def test_an_unreadable_marker_ends_the_run_at_the_next_poll(self):
        """The loop side of the same property, which the way-out test takes as given.

        A marker too large to read is still a marker. Waiting for one that will never be
        readable costs the guest its whole bound — five seconds here, five minutes at the
        default — for a program that has already finished.
        """
        guest = _ScriptedGuest([], finish=False)
        guest.files[_LAYOUT.output] = b"it ran, whatever the marker says"
        guest.files[_LAYOUT.exit_code] = b"9" * 40  # over the 32-byte marker cap

        began = time.monotonic()
        result = _run(guest, HostToolRun(_registry()), timeout=5.0)

        assert result.exit_code == 1, "an unreadable marker stopped meaning a finished run"
        assert result.stdout == "it ran, whatever the marker says"
        assert time.monotonic() - began < 1.0, "the run waited out its bound for a finished program"

    def test_an_unreadable_marker_means_the_same_on_the_way_out_as_it_did_in_the_loop(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """One file, one meaning, whichever side of the deadline reads it.

        A marker too large to read is still a marker: in the loop it means the program
        finished and its code could not be trusted, which `_exit_code_from` answers with 1.
        Filtering it out of the last look would make the same bytes mean "never finished" a
        poll later, losing the output with it.
        """

        guest = self._lands_during_the_sleep(monkeypatch, b"9" * 40)  # over the 32-byte cap
        result = _run(guest, HostToolRun(_registry()), timeout=5.0)

        assert result.exit_code == 1, "an unreadable marker stopped meaning a finished run"
        assert result.stdout == "it finished quietly"

    def test_a_marker_read_that_runs_out_is_looked_for_once_more(self):
        """A stat that found the marker proves the program finished, whatever the read did.

        The read of it gets whatever is left of the run, which can be a millisecond, and its
        expiry lands in the handler that announces the run did not finish. One more look, on
        a grace of its own, is what tells those two apart — the first read ran out, the
        program did not.
        """
        slow = {"first": True}

        class _SlowFirstMarkerRead(_ScriptedGuest):
            async def read_file(self, path: str, *, working_directory: str, max_bytes: int):
                if path.endswith("program_exit_code") and slow["first"]:
                    slow["first"] = False
                    await asyncio.sleep(3600)
                return await super().read_file(
                    path, working_directory=working_directory, max_bytes=max_bytes
                )

        guest = _SlowFirstMarkerRead([], output="it did run", exit_code=5)
        result = _run(guest, HostToolRun(_registry()), timeout=1.0)

        assert result.exit_code == 5, "a finished run was reported as unfinished"
        assert result.stdout == "it did run"

    def test_output_the_deadline_outlasts_is_reported_as_itself(self):
        """The grace is a floor, not a licence: a read that runs past it still has to say so.

        Failing here must still return the recorded exit code — the program did finish — with
        the reason stdout is empty, rather than a timeout blaming the program.
        """

        class _NeverFinishesTheRead(_ScriptedGuest):
            async def read_file(self, path: str, *, working_directory: str, max_bytes: int):
                if path.endswith("program_output.txt"):
                    await asyncio.sleep(3600)
                return await super().read_file(
                    path, working_directory=working_directory, max_bytes=max_bytes
                )

        guest = _NeverFinishesTheRead([], output="never arrives", exit_code=3)
        result = _run(guest, HostToolRun(_registry()), timeout=1.0)

        assert result.exit_code == 3, "the recorded exit code was lost with the output"
        assert result.stdout == ""
        assert "could not be read" in result.stderr
        assert "program_output.txt" in result.stderr, "the reason names no file"

    def test_a_backend_failure_that_is_no_timeout_still_propagates_after_the_marker(self):
        """Only a timeout on the output read is absorbed into "could not be read".

        A permission error is the backend saying something is wrong, and swallowing it
        would report a healthy run with missing output — losing the one sentence that says
        what actually happened.
        """

        class _CannotHandItOver(_ScriptedGuest):
            async def read_file(self, path: str, *, working_directory: str, max_bytes: int):
                if path.endswith("program_output.txt"):
                    raise PermissionError("the daemon said no")
                return await super().read_file(
                    path, working_directory=working_directory, max_bytes=max_bytes
                )

        guest = _CannotHandItOver([], exit_code=7)
        with pytest.raises(PermissionError, match="the daemon said no"):
            _run(guest, HostToolRun(_registry()))

    def test_output_that_is_not_utf8_comes_back_repaired_rather_than_dropped(self):
        """One bad byte in a program's own output must not read as a program that said nothing.

        The output is quoted back to a human, so a replacement character beats losing the
        whole of it — unlike a request, where a repaired byte would be acted on.
        """
        guest = _ScriptedGuest([], finish=False)
        guest.files[_LAYOUT.output] = b"caf\xe9 done"
        guest.files[_LAYOUT.exit_code] = b"0"
        result = _run(guest, HostToolRun(_registry()))

        assert result.exit_code == 0
        assert result.stdout == "caf� done"
        assert result.stderr == ""

    def test_a_marker_that_is_not_a_number_still_means_a_finished_run(self):
        """The marker's appearance is the fact; its content is only the guest's claim.

        Content this cannot trust reads as exit 1 — raising instead would crash the
        supervisor on bytes any guest can write.
        """
        guest = _ScriptedGuest([], finish=False)
        guest.files[_LAYOUT.output] = b"it ran"
        guest.files[_LAYOUT.exit_code] = b"oops"
        result = _run(guest, HostToolRun(_registry()))

        assert result.exit_code == 1
        assert result.stdout == "it ran"


class TestTheOutputLimit:
    """`output_limit` bounds the program's stdout by the caller's own number rather than the run's
    borrowed host-tool total leg (#354, option A). The default preserves today's behaviour."""

    def test_a_limit_tighter_than_the_borrowed_leg_is_the_one_that_bites(self):
        """Output over `output_limit` is not returned even when `max_total_bytes` would admit it,
        so the bound stops being a side effect of unrelated host-tool configuration."""
        limits = TransferLimits(max_bytes_per_file=64, max_total_bytes=4096, max_files=4)
        guest = _ScriptedGuest([], output="x" * 100)
        result = _run(guest, HostToolRun(_registry(response_limits=limits)), output_limit=32)
        assert result.exit_code == 0
        assert result.stdout == ""
        assert "larger than the host will read" in result.stderr

    def test_a_limit_looser_than_the_borrowed_leg_admits_what_it_would_have_dropped(self):
        """The override raises the ceiling as well as lowering it: 100 bytes come back whole under
        a 256-byte limit even though the borrowed max_total_bytes of 32 would have dropped them."""
        limits = TransferLimits(max_bytes_per_file=64, max_total_bytes=32, max_files=4)
        guest = _ScriptedGuest([], output="x" * 100)
        result = _run(guest, HostToolRun(_registry(response_limits=limits)), output_limit=256)
        assert result.stdout == "x" * 100
        assert result.stderr == ""

    def test_none_borrows_the_response_total_leg(self):
        """The default is today's behaviour: the run's `max_total_bytes` is the bound."""
        limits = TransferLimits(max_bytes_per_file=64, max_total_bytes=32, max_files=4)
        guest = _ScriptedGuest([], output="x" * 100)
        result = _run(guest, HostToolRun(_registry(response_limits=limits)))
        assert result.stdout == ""
        assert "larger than the host will read" in result.stderr

    @pytest.mark.parametrize("bad", [0, -1, True, False, 3.5, "32"])
    def test_a_limit_that_is_not_a_positive_integer_is_refused(self, bad: object):
        with pytest.raises(ValueError, match="output_limit"):
            _run(_ScriptedGuest([]), HostToolRun(_registry()), output_limit=bad)  # type: ignore[arg-type]


class TestWhatAnEmptyOutputMeans:
    def test_an_output_dropped_for_its_size_says_so(self):
        """Empty stdout beside exit code 0 is a report of a program that printed nothing.

        For output refused by the cap that report is false, and false in the direction a
        caller cannot check. The host's note goes in `stderr`, which on this transport is the
        host's own channel — the launcher merges the guest's stderr into the output file.
        """
        limits = TransferLimits(max_bytes_per_file=64, max_total_bytes=32, max_files=4)
        guest = _ScriptedGuest([], output="x" * 200)
        result = _run(guest, HostToolRun(_registry(response_limits=limits)))

        assert result.exit_code == 0
        assert result.stdout == ""
        assert "larger than the host will read" in result.stderr

    def test_a_timed_out_run_says_why_it_is_quoting_nothing(self):
        """ "Output so far:" followed by nothing reads as a program that printed nothing.

        A timed-out run whose output was refused for its size is the case where that is false,
        and the failure message is the only place a reader is looking.
        """

        class _PrintsThenHangs(_ScriptedGuest):
            """Output on disk, no exit marker — the shape the timeout message exists for."""

            async def exec(self, command: str | Any, *, working_directory: str, timeout: float):
                started = await super().exec(
                    command, working_directory=working_directory, timeout=timeout
                )
                self.files[_LAYOUT.output] = b"x" * 200
                return started

        limits = TransferLimits(max_bytes_per_file=64, max_total_bytes=32, max_files=4)
        guest = _PrintsThenHangs([], finish=False)

        with pytest.raises(TimeoutError, match="larger than the host will read"):
            _run(guest, HostToolRun(_registry(response_limits=limits)), timeout=0.1)

    def test_a_program_that_really_printed_nothing_says_nothing(self):
        """The note has to distinguish the two cases, or it is noise on every quiet run."""
        guest = _ScriptedGuest([], output="")
        result = _run(guest, HostToolRun(_registry()))

        assert result.exit_code == 0
        assert result.stdout == ""
        assert result.stderr == ""


class TestWhoOwnsStderrHere:
    """`stderr` is this transport's own field, and the result says so rather than a caller
    inferring it from having built the transport itself."""

    def test_a_finished_run_declares_the_ownership(self):
        assert _run(
            _ScriptedGuest([], output="printed"), HostToolRun(_registry())
        ).producer_owns_stderr

    def test_a_launcher_that_never_started_the_program_declares_it_too(self):
        """No program ran on this leg, so the sentence on `stderr` is the launcher's."""
        result = _run(_ScriptedGuest([], launcher_exit_code=127), HostToolRun(_registry()))
        assert result.exit_code == 127
        assert result.producer_owns_stderr

    def test_the_launchers_own_stdout_is_not_returned_as_the_programs(self):
        """`stdout` is the program's field, and on this leg no program ran.

        What the launcher prints there is this module's own marker for which launch path it
        took, so returning it would hand a kind host text to render as the guest's.
        """

        class _FailsAfterItsMarker(_ScriptedGuest):
            async def exec(self, command: str | Any, *, working_directory: str, timeout: float):
                if _LAYOUT.launcher in str(command):
                    return ExecResult(stdout=SESSION_MADE, stderr="", exit_code=126)
                return await super().exec(
                    command, working_directory=working_directory, timeout=timeout
                )

        result = _run(_FailsAfterItsMarker([]), HostToolRun(_registry()))

        assert result.exit_code == 126
        assert result.stdout == ""
        assert SESSION_MADE in result.stderr, (
            "the launcher's own word was dropped rather than moved"
        )

    def test_a_dropped_output_declares_it_beside_an_empty_stdout(self):
        """The flag is about who owns `stderr`, never about how much of the output came back.

        This is the combination a completeness reading gets wrong: the program printed 200
        bytes, `stdout` is empty because the cap refused them, and the note saying so is on
        the field the flag claims.
        """
        limits = TransferLimits(max_bytes_per_file=64, max_total_bytes=32, max_files=4)
        guest = _ScriptedGuest([], output="x" * 200)
        result = _run(guest, HostToolRun(_registry(response_limits=limits)))

        assert result.stdout == ""
        assert "larger than the host will read" in result.stderr
        assert result.producer_owns_stderr

    def test_an_output_that_could_not_be_read_declares_it_too(self):
        class _RefusesToHandItOver(_ScriptedGuest):
            async def read_file(self, path: str, *, working_directory: str, max_bytes: int):
                if path.endswith("program_output.txt"):
                    raise TimeoutError("the service stopped answering")
                return await super().read_file(
                    path, working_directory=working_directory, max_bytes=max_bytes
                )

        guest = _RefusesToHandItOver([], output="unreachable")
        result = _run(guest, HostToolRun(_registry()), timeout=1.0)
        assert "could not be read" in result.stderr
        assert result.producer_owns_stderr

    def test_every_result_this_module_builds_declares_it(self):
        """The three above cover the exit paths that exist; this covers the next one.

        A path added without the flag hands a caller a host sentence labelled as the guest's,
        and no behavioural test would fail — the flag would simply read false.
        """
        source = Path(host_tools_over_exec.__file__).read_text(encoding="utf-8")
        built = [
            node
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "ExecResult"
        ]
        assert built, "the module builds no ExecResult at all, so this asserts nothing"
        for call in built:
            assert any(
                keyword.arg == "producer_owns_stderr" and keyword.value.value is True
                for keyword in call.keywords
                if isinstance(keyword.value, ast.Constant)
            ), f"an ExecResult built on line {call.lineno} does not declare producer_owns_stderr"


class TestWhoseTimeoutItWas:
    def test_a_clock_that_reads_behind_the_timer_still_names_this_runs_own_expiry(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """Whose timeout it was is asyncio's to answer, not the clock's.

        A timer may fire up to one clock resolution early — 16ms where `time.monotonic` is
        `GetTickCount64`, which is CPython 3.12 on Windows and inside this package's supported
        range. A supervisor that re-reads the clock then sees time left, calls its own expiry
        a backend's, and sends it past the handler that takes one more look for the marker:
        the run reports that nothing finished when something had.

        A clock that cannot advance is that reading taken to its limit, and it makes the
        misclassification certain rather than a matter of tick phase.
        """
        monkeypatch.setattr(
            host_tools_over_exec, "time", _Shifted(lambda: 1000.0, {"seconds": 0.0})
        )

        async def _stalls() -> None:
            await asyncio.sleep(3600)

        with pytest.raises(TimeoutError) as caught:
            asyncio.run(host_tools_over_exec._within(1000.05, "a stalled call", _stalls()))

        assert isinstance(caught.value, host_tools_over_exec._DeadlineExpired), (
            "this run's own expiry was handed on as a backend's"
        )

    def test_a_backends_own_timeout_reaches_the_caller_intact(self):
        """Two unrelated things raise `TimeoutError`, and only one of them is the run ending.

        acas bounds a read because a guest can plant a FIFO the service reports as a regular
        file and never serves, and it says so in the message. Catching that as the supervisor
        deadline replaces the only sentence that names the actual cause with a generic one
        about a slow program — and the run has time left, which is the tell.
        """
        planted = "a guest can make an entry the service never serves"

        class _BoundsItsOwnRead(_ScriptedGuest):
            async def read_file(self, path: str, *, working_directory: str, max_bytes: int):
                raise TimeoutError(planted)

        with pytest.raises(TimeoutError, match=planted):
            _run(_BoundsItsOwnRead([("add", {"left": 1, "right": 1})]), HostToolRun(_registry()))

    def test_the_runs_own_deadline_still_reports_the_run(self):
        """The other half: what the supervisor's bound raises must keep saying so."""

        class _Stalls(_ScriptedGuest):
            async def stat_file(self, path: str, *, working_directory: str):
                await asyncio.sleep(3600)
                raise AssertionError("unreachable")

        with pytest.raises(TimeoutError, match="did not finish within"):
            _run(_Stalls([]), HostToolRun(_registry()), timeout=0.2)

    def test_the_two_are_told_apart_by_type_and_not_by_reading_the_message(self):
        """The two are told apart by type, which is the only thing a caller can branch on.

        One means the program ran out and may still be running; the other means a
        control-plane call ran out and says nothing about the program at all.
        """

        class _BoundsItsOwnStat(_ScriptedGuest):
            async def stat_file(self, path: str, *, working_directory: str):
                raise TimeoutError("the service bounds this read")

        with pytest.raises(TimeoutError) as backends:
            _run(_BoundsItsOwnStat([], finish=False), HostToolRun(_registry()), timeout=30.0)
        assert not isinstance(backends.value, SandboxProgramTimeout), (
            "a backend's own bound was typed as the run's, with 29s still on the clock"
        )

        wedged = _ScriptedGuest([], finish=False)
        wedged.files[_LAYOUT.output] = b"step 1 done"
        with pytest.raises(SandboxProgramTimeout) as expired:
            _run(wedged, HostToolRun(_registry()), timeout=0.05)
        assert expired.value.output == "step 1 done", "the quote a caller surfaces was not carried"
        assert "step 1 done" in str(expired.value), "the message stopped carrying it too"

    def test_the_runs_bound_expiring_before_the_program_starts_is_still_the_runs(self):
        """Budget exhausted during the upload is the run's own timeout, publicly typed.

        This `_within` sits outside the supervisor loop, so nothing else converts what it
        raises; untranslated, a module-private type would cross the public boundary.
        """

        class _StallsOnTheUpload(_ScriptedGuest):
            async def write_file(
                self, path: str, content: str | bytes, *, working_directory: str
            ) -> None:
                await asyncio.sleep(3600)

        with pytest.raises(SandboxProgramTimeout, match="before the program was started") as gone:
            _run(_StallsOnTheUpload([], finish=False), HostToolRun(_registry()), timeout=0.1)

        assert gone.value.output == "", "a program that never started printed something"

    def test_the_budget_running_out_while_starting_is_the_runs_own_either_way(self):
        """Both startup legs spend the same budget, so both classify as the run's own.

        The upload and the `exec` that starts the launcher are one situation — the budget gone
        before the program was going — and which leg it lands on is a matter of milliseconds.
        """

        class _BoundsTheStart(_ScriptedGuest):
            async def exec(self, command: str | Any, *, working_directory: str, timeout: float):
                raise TimeoutError("the service bounds this exec")

        with pytest.raises(SandboxProgramTimeout, match="while starting the program") as spent:
            _run(_BoundsTheStart([], finish=False), HostToolRun(_registry()), timeout=30.0)

        # A client's timeout text is where an endpoint or a request id lives, and this
        # message reaches a model. It stays on `__cause__` for a caller that wants it.
        assert "the service bounds this exec" not in str(spent.value)
        assert str(spent.value.__cause__) == "the service bounds this exec"

    def test_a_bare_backend_timeout_leaves_no_dangling_reason(self):
        """A backend's own text never reaches the message, and its absence leaves no seam.

        The shipped backends bound `exec` with `asyncio.wait_for`, whose `TimeoutError` is
        empty, so this is the ordinary shape rather than an edge of one."""

        class _BoundsTheStartSilently(_ScriptedGuest):
            async def exec(self, command: str | Any, *, working_directory: str, timeout: float):
                raise TimeoutError  # exactly what `asyncio.wait_for` raises

        with pytest.raises(SandboxProgramTimeout) as spent:
            _run(_BoundsTheStartSilently([], finish=False), HostToolRun(_registry()), timeout=30.0)

        assert str(spent.value) == (
            "the run's 30s were gone while starting the program"
            " (whether it got as far as starting one could not be established)"
        )
        assert not str(spent.value).endswith("—"), "the message trails off into nothing"

    def test_the_hosts_note_is_not_passed_off_as_the_programs_own_words(self):
        """`output` is what a caller quotes under "Output so far", so only stdout belongs in it.

        An output dropped for its size leaves the host explaining why. On the success path that
        explanation goes to `stderr`; putting it in `output` here would hand a model host prose
        in the position its program's own words occupy.
        """

        class _PrintsTooMuchThenHangs(_ScriptedGuest):
            async def exec(self, command: str | Any, *, working_directory: str, timeout: float):
                started = await super().exec(
                    command, working_directory=working_directory, timeout=timeout
                )
                self.files[_LAYOUT.output] = b"x" * 200
                return started

        limits = TransferLimits(max_bytes_per_file=64, max_total_bytes=32, max_files=4)
        guest = _PrintsTooMuchThenHangs([], finish=False)

        with pytest.raises(SandboxProgramTimeout) as expired:
            _run(guest, HostToolRun(_registry(response_limits=limits)), timeout=0.1)

        assert expired.value.output == "", "the host's note was handed over as program output"
        assert "larger than the host will read" in str(expired.value), "the note went missing"
        # The attribute is only half of it: `Output so far:` is a promise about whose words
        # follow, and the note appearing under that label breaks it in the message too.
        assert "Output so far" not in str(expired.value), (
            "the host's note is wearing the label that means the program's own stdout"
        )

    def test_the_hosts_reason_for_reading_nothing_is_readable_without_the_message(self):
        """A caller that must not surface guest text cannot read the message at all.

        The message carries the program's words or the host's reason, so taking it whole to
        get the second hands over the first wherever there is one. Both of `_output_clause`'s
        inputs are therefore attributes.
        """

        class _PrintsTooMuchThenHangs(_ScriptedGuest):
            async def exec(self, command: str | Any, *, working_directory: str, timeout: float):
                started = await super().exec(
                    command, working_directory=working_directory, timeout=timeout
                )
                self.files[_LAYOUT.output] = b"THE-SECRET-IS-42" * 20
                return started

        limits = TransferLimits(max_bytes_per_file=64, max_total_bytes=32, max_files=4)
        guest = _PrintsTooMuchThenHangs([], finish=False)

        with pytest.raises(SandboxProgramTimeout) as expired:
            _run(guest, HostToolRun(_registry(response_limits=limits)), timeout=0.1)

        assert "larger than the host will read" in expired.value.output_reason
        assert "THE-SECRET-IS-42" not in expired.value.output_reason, (
            "the host's reason is quoting the program"
        )

    def test_a_run_whose_output_was_read_gives_no_reason_for_reading_none(self):
        """Empty is what says the output beside it is the whole answer."""
        wedged = _ScriptedGuest([], finish=False)
        wedged.files[_LAYOUT.output] = b"step 1 done"

        with pytest.raises(SandboxProgramTimeout) as expired:
            _run(wedged, HostToolRun(_registry()), timeout=0.05)

        assert expired.value.output == "step 1 done"
        assert expired.value.output_reason == ""

    def test_a_run_that_expired_before_the_program_started_gives_no_reason_either(self):
        """Nothing had been started, so there is no output to have a reason about."""

        class _BoundsTheStart(_ScriptedGuest):
            async def exec(self, command: str | Any, *, working_directory: str, timeout: float):
                raise TimeoutError

        with pytest.raises(SandboxProgramTimeout) as spent:
            _run(_BoundsTheStart([], finish=False), HostToolRun(_registry()), timeout=30.0)

        assert spent.value.output_reason == ""

    def test_an_ordinary_expiry_blames_no_transport_call(self, monkeypatch: pytest.MonkeyPatch):
        """A run that simply ran out must not report a sandbox that answered promptly.

        The clause naming a stalled call exists for the call that ran out. On the plain
        path every stat came back on time, and a message carrying it anyway sends the
        reader chasing a healthy backend.
        """
        ahead = {"seconds": 0.0}
        monkeypatch.setattr(host_tools_over_exec, "time", _Shifted(time.monotonic, ahead))

        class _OutlivedInSilence(_ScriptedGuest):
            async def stat_file(self, path: str, *, working_directory: str):
                if path.endswith(".request.json"):
                    ahead["seconds"] += 60.0
                return await super().stat_file(path, working_directory=working_directory)

        guest = _OutlivedInSilence([], finish=False)
        with pytest.raises(SandboxProgramTimeout) as expired:
            _run(guest, HostToolRun(_registry()), timeout=5.0)

        assert "the sandbox did not answer" not in str(expired.value), (
            "a plain expiry was blamed on a transport call"
        )


class TestTheGuestsOwnDiagnostic:
    def test_a_fractional_patience_is_reported_as_itself(self, tmp_path: Path):
        """`%d` on 0.5 says "within 0 seconds", which is not a duration anyone waited.

        Any finite positive number is a legal patience, so the message cannot round.
        """
        module_path = tmp_path / SHIM_MODULE
        module_path.write_text(host_tool_shim(call_timeout=0.05), encoding="utf-8")
        spec = importlib.util.spec_from_file_location("maf_host_tools_patience", module_path)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        (tmp_path / CALLS_DIRECTORY).mkdir(parents=True, exist_ok=True)

        with pytest.raises(module.HostToolError, match="within 0.05 seconds"):
            module.call("nobody_is_listening")


class TestWhatAFailedReadMeans:
    def test_a_backend_that_cannot_read_ends_the_run_with_its_own_error(self):
        """A permanent failure retried every interval reads back as a slow guest.

        `PermissionError` will not resolve itself, so polling past it spends the whole bound
        and then reports "the guest program did not finish" — which is false, and points the
        reader at the program instead of at the backend.
        """

        class _Unreadable(_ScriptedGuest):
            async def read_file(self, path: str, *, working_directory: str, max_bytes: int):
                raise PermissionError("the daemon said no")

        with pytest.raises(PermissionError, match="the daemon said no"):
            _run(_Unreadable([("add", {"left": 1, "right": 1})]), HostToolRun(_registry()))

    def test_a_file_that_vanished_between_stat_and_read_is_only_a_missed_poll(self):
        """The one failure polling again *is* the answer to, so it must stay a retry."""
        vanished: list[str] = []

        class _Vanishing(_ScriptedGuest):
            async def read_file(self, path: str, *, working_directory: str, max_bytes: int):
                if not vanished:
                    vanished.append(path)
                    raise FileNotFoundError(path)
                return await super().read_file(
                    path, working_directory=working_directory, max_bytes=max_bytes
                )

        guest = _Vanishing([("add", {"left": 2, "right": 2})])
        _run(guest, HostToolRun(_registry()))
        assert vanished, "the race never happened, so this proves nothing"
        assert guest.answers[0].get("value") == 4

    def test_a_backend_refusing_after_the_fact_is_the_over_cap_refusal(self):
        """`SandboxTransferCapExceeded` is how a client that buffered first has to refuse."""

        class _RefusesLate(_ScriptedGuest):
            async def read_file(self, path: str, *, working_directory: str, max_bytes: int):
                if path.endswith("request.json"):
                    raise SandboxTransferCapExceeded("buffered it all, then looked")
                return await super().read_file(
                    path, working_directory=working_directory, max_bytes=max_bytes
                )

        guest = _RefusesLate([("add", {"left": 1, "right": 1})])
        _run(guest, HostToolRun(_registry()))
        assert "larger than the host will read" in guest.answers[0]["refusal"]

    def test_a_backend_failure_on_the_way_out_does_not_replace_the_timeout(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """The last look and the output read are diagnostics, and a diagnostic must not win.

        Both run only once the run is already being reported as expired, so whatever the
        backend raises there — a timeout or anything else — has to read as "nothing found":
        the caller is owed the run's own reason, not the failure of reading the reason.
        """
        ahead = {"seconds": 0.0}
        monkeypatch.setattr(host_tools_over_exec, "time", _Shifted(time.monotonic, ahead))

        class _DiesOnceExpired(_ScriptedGuest):
            expired = False

            async def stat_file(self, path: str, *, working_directory: str):
                if self.expired:
                    raise PermissionError("the daemon said no")
                if path.endswith(".request.json"):
                    self.expired = True
                    ahead["seconds"] += 60.0
                return await super().stat_file(path, working_directory=working_directory)

        guest = _DiesOnceExpired([], finish=False)
        with pytest.raises(SandboxProgramTimeout, match="did not finish within"):
            _run(guest, HostToolRun(_registry()), timeout=5.0)


class TestWhatABackendMayHaveIgnored:
    def test_a_read_over_its_cap_is_refused_rather_than_parsed(self):
        """`max_bytes` is what a backend was asked for, not what it is known to have honoured.

        The protocol says so where `read_file` is defined — a backend whose SDK buffers the
        whole response can only refuse after the fact, "which is why the caller re-counts what
        actually arrived" — and `collect_outputs` counts. This is the third caller.
        """

        class _OverCap(_ScriptedGuest):
            async def read_file(self, path: str, *, working_directory: str, max_bytes: int):
                content = self.files[self._resolved(path, working_directory)]
                del max_bytes  # a backend that buffered first and returns it all anyway
                return content

        class _Unstinting(_OverCap):
            async def stat_file(self, path: str, *, working_directory: str):
                entry = await super().stat_file(path, working_directory=working_directory)
                if entry is not None and entry.path.endswith("request.json"):
                    # A stat that under-reports, which is the case the recount is for.
                    return SandboxEntry(path=entry.path, kind=entry.kind, size_bytes=1)
                return entry

        limits = TransferLimits(max_bytes_per_file=48, max_total_bytes=4096, max_files=4)
        guest = _Unstinting([("add", {"left": 1, "right": 1})], request_bytes=200)
        _run(guest, HostToolRun(_registry(response_limits=limits)))
        assert "larger than the host will read" in guest.answers[0]["refusal"]


class TestTheArgumentsTheSupervisorTakes:
    @pytest.mark.parametrize("bound", [float("inf"), float("nan"), 0.0, -1.0])
    def test_a_run_bound_that_is_not_finite_and_positive_is_refused(self, bound: float):
        """`inf` passes every range check and then removes the bound the docstring promises."""
        with pytest.raises(ValueError, match="timeout"):
            _run(_ScriptedGuest([]), HostToolRun(_registry()), timeout=bound)

    @pytest.mark.parametrize("interval", [float("nan"), -1.0, 0.0])
    def test_a_poll_interval_that_is_not_finite_and_positive_is_refused(self, interval: float):
        """Zero as well as negative: `sleep(0)` comes straight back, so nothing is throttled."""
        with pytest.raises(ValueError, match="poll_interval"):
            asyncio.run(
                host_tool_calls_over_exec(
                    _ScriptedGuest([]),
                    HostToolRun(_registry()),
                    _LAYOUT,
                    timeout=1.0,
                    poll_interval=interval,
                )
            )


class TestWhatTheSupervisorStepsOver:
    def test_an_abandoned_number_is_not_answered_and_does_not_stall_the_next(self):
        """The marker exists so a hole needs no third call to fill it.

        One caller took `0001` and could not publish; another has already published `0002`
        and is waiting. Serving is by name and in order, so without the marker `0002` is
        never reached. Nothing waits on `0001`, so answering it would be a round trip spent
        on nobody — the supervisor steps over it instead.
        """
        guest = _ScriptedGuest([(_ABANDONED, {}), ("add", {"left": 2, "right": 3})])
        _run(guest, HostToolRun(_registry()))

        assert guest.answers and guest.answers[0].get("value") == 5, (
            f"the call behind the abandoned number went unserved: {guest.answers}"
        )
        assert posixpath.join(_LAYOUT.calls, "0001.response.json") not in guest.files, (
            "the supervisor answered a number nobody was waiting on"
        )


class TestWhatTheSupervisorWillKeepPayingFor:
    def test_serving_stops_once_the_run_has_spent_its_allowance(self):
        """A refusal costs the guest nothing and the host a stat, a read and a write.

        With a cap of one: the first call is served, the second is refused because the cap is
        gone, and the third is not read at all. A guest that ignores the refusal and loops
        would otherwise keep the supervisor paying until the deadline, leaving a response file
        behind each time — and a malformed request never reaches `dispatch`, so it never
        spends the cap that is supposed to bound this.

        The run then times out, which is the honest end state: the guest is still calling and
        the host has stopped answering.
        """
        registry = _registry(max_host_tool_calls_per_run=1)
        guest = _ScriptedGuest([("add", {"left": 1, "right": 1})] * 3)

        with pytest.raises(TimeoutError):
            _run(guest, HostToolRun(registry), timeout=0.5)

        assert len(guest.answers) == 2, f"the allowance did not hold: {guest.answers}"
        assert guest.answers[0].get("value") == 2
        assert "host-tool-call cap" in guest.answers[1]["refusal"]


class TestTheShimsSequenceAllocation:
    @staticmethod
    def _loaded(tmp_path: Path, name: str) -> Any:
        """The generated shim, importable, with its calls directory beside it."""
        module_path = tmp_path / SHIM_MODULE
        module_path.write_text(host_tool_shim(), encoding="utf-8")
        spec = importlib.util.spec_from_file_location(name, module_path)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        (tmp_path / CALLS_DIRECTORY).mkdir(parents=True, exist_ok=True)
        return module

    def test_a_call_that_never_publishes_leaves_a_marker_instead_of_a_hole(self, tmp_path: Path):
        """A gap in the sequence is not a lost call — it is the rest of the run, lost.

        The supervisor answers `0001` before it looks for `0002`, so a number nobody publishes
        under is one it waits on until the deadline. Handing the number back is not enough:
        the caller who would take it next is often past it already, having claimed and
        published `0002`, and only a *third* call would fill the hole. So the failing caller
        publishes a marker instead, which the supervisor can step over on its own.

        An argument `json.dumps` cannot serialize is the ordinary way in, and a program that
        catches the `TypeError` — most do — keeps calling.
        """
        module = self._loaded(tmp_path, "maf_host_tools_gap")
        calls = tmp_path / CALLS_DIRECTORY
        (calls / "0002.response.json").write_text(json.dumps({"value": "second"}), encoding="utf-8")

        with pytest.raises(TypeError):
            module.call("anything", unserializable=object())

        published = calls / "0001.request.json"
        assert published.exists(), "the number was left holding nothing"
        assert json.loads(published.read_text(encoding="utf-8"))["abandoned"] is True
        assert module.call("anything") == "second", "the next call did not move on"

    def test_separate_processes_do_not_share_an_identifier(self, tmp_path: Path):
        """A program that forks or spawns gets a second copy of the shim, not a second lock.

        Two copies counting privately both start at one, so both write `0001.request.json` —
        one call overwrites the other, and each reads the same answer as its own. A lock in
        the module cannot see across a process boundary; the claim file can, because
        `O_CREAT | O_EXCL` is one filesystem operation and only one caller wins it.

        Real processes rather than a mocked fork: the property under test is what the *kernel*
        does with two callers, which a double cannot stand in for.
        """
        module_path = tmp_path / SHIM_MODULE
        module_path.write_text(host_tool_shim(), encoding="utf-8")
        calls = tmp_path / CALLS_DIRECTORY
        calls.mkdir(parents=True, exist_ok=True)
        for index in range(1, 5):
            (calls / f"{index:04d}.response.json").write_text(
                json.dumps({"value": index}), encoding="utf-8"
            )

        caller = (
            "import importlib.util, sys;"
            f"spec = importlib.util.spec_from_file_location('shim', {str(module_path)!r});"
            "module = importlib.util.module_from_spec(spec);"
            "spec.loader.exec_module(module);"
            "print(module.call('anything'))"
        )
        workers = [
            subprocess.Popen(  # noqa: S603 - the interpreter running this test, no shell
                [sys.executable, "-c", caller], stdout=subprocess.PIPE, text=True
            )
            for _ in range(3)
        ]
        answers = [worker.communicate(timeout=60)[0].strip() for worker in workers]

        assert all(answers), f"a worker never got an answer: {answers}"
        assert len(set(answers)) == 3, f"two processes shared an identifier: {answers}"

    def test_concurrent_callers_get_distinct_identifiers(self, tmp_path: Path):
        """Each caller takes its own sequence number, whatever the program's thread count.

        The same claim the process test pins, from inside one process: `O_CREAT | O_EXCL`
        settles threads and processes alike, which is why there is no lock here to go with it.
        """
        source = host_tool_shim()
        assert "os.O_EXCL" in source, "the sequence is allocated without an exclusive claim"
        module_path = tmp_path / SHIM_MODULE
        module_path.write_text(source, encoding="utf-8")
        spec = importlib.util.spec_from_file_location("maf_host_tools_threads", module_path)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        calls = tmp_path / CALLS_DIRECTORY
        calls.mkdir(parents=True, exist_ok=True)
        for index in range(1, 65):
            (calls / f"{index:04d}.response.json").write_text(
                json.dumps({"value": index}), encoding="utf-8"
            )

        answers: list[int] = []
        guard = threading.Lock()

        def one_call() -> None:
            value = module.call("anything")
            with guard:
                answers.append(value)

        threads = [threading.Thread(target=one_call) for _ in range(32)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        assert len(answers) == 32
        assert len(set(answers)) == 32, f"two callers shared an identifier: {sorted(answers)}"


# ---------------------------------------------------------------------------------------------
# Stopping a run that overran (#375)
# ---------------------------------------------------------------------------------------------


async def _spend(timeout: float, command: str) -> None:
    """Refuse a command the transport gave no time to run, as a real backend would.

    Every backend bounds its own call with the `timeout` it was handed — docker with
    `asyncio.wait_for`, acas the same — so a zero or negative budget is a `TimeoutError`
    before anything reaches the guest. A fake that ignores the argument reports success for
    work that could never have happened.
    """
    if timeout <= 0:
        raise TimeoutError(f"no time left to run {command!r}")
    await asyncio.sleep(0)


class _GuestThatRecordsTheKill(_ScriptedGuest):
    """A guest whose launcher writes a pid, and which remembers every command exec'd at it.

    The pid lands on `exec` rather than in the constructor, because that is when the real
    launcher writes it: a pid present before the program started would let a test pass against
    a transport that killed something it had no business killing.
    """

    def __init__(
        self,
        *args: Any,
        pid: str | None = "4242",
        session: str | None = None,
        announces_session: bool | None = None,
        kill_exit_code: int = 0,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.commands: list[str] = []
        self._pid = pid
        self._session = session
        # What the launcher printed. Defaults to agreeing with the file, so a test that
        # only cares about the group form says one thing; set it apart to model a guest
        # whose program planted a session file the launcher never made.
        self._announces = session is not None if announces_session is None else announces_session
        self._kill_exit_code = kill_exit_code

    async def exec(
        self, command: str | Any, *, working_directory: str, timeout: float
    ) -> ExecResult:
        if "kill" in str(command):
            # Recorded *after* the yield and the budget check, so a command the transport
            # issued with no time to spend does not count as one the guest ran. A fake that
            # records on entry cannot tell "sent" from "attempted", and every assertion in
            # this file about what the guest was made to do rests on that distinction.
            await _spend(timeout, str(command))
            self.commands.append(str(command))
            return ExecResult(stdout="", exit_code=self._kill_exit_code)
        self.commands.append(str(command))
        started = await super().exec(command, working_directory=working_directory, timeout=timeout)
        if self._announces:
            started = ExecResult(
                stdout=f"{started.stdout}{SESSION_MADE}\n",
                stderr=started.stderr,
                exit_code=started.exit_code,
            )
        if self._pid is not None:
            self.files[_LAYOUT.pid] = self._pid.encode("utf-8")
        if self._session is not None:
            self.files[_LAYOUT.session] = self._session.encode("utf-8")
        return started

    @property
    def kills(self) -> list[str]:
        return [command for command in self.commands if "kill" in command]


class TestStoppingTakesTheChildrenWhereItCan:
    """A stopped program takes its children with it where the guest can.

    `kill` reaches one process, so a program that spawned anything left it running in a
    sandbox the next call warm-reuses. Where the guest has `setsid` the launcher puts the
    program in its own session and the signal goes to that group instead.
    """

    def test_a_recorded_session_is_signalled_as_a_group(self):
        guest = _GuestThatRecordsTheKill([], finish=False, pid="4242", session="4200")
        with pytest.raises(SandboxProgramTimeout):
            _run(guest, HostToolRun(_registry()), timeout=0.2)
        assert len(guest.kills) == 1, guest.commands
        assert "-KILL -4200" in guest.kills[0], f"the group was not signalled: {guest.kills}"

    def test_without_a_session_the_lone_pid_is_signalled(self):
        """A guest without `setsid` shares the launcher's session, where a group signal
        would reach the whole container."""
        guest = _GuestThatRecordsTheKill([], finish=False, pid="4242", session=None)
        with pytest.raises(SandboxProgramTimeout):
            _run(guest, HostToolRun(_registry()), timeout=0.2)
        assert "-KILL 4242" in guest.kills[0], guest.kills

    @pytest.mark.parametrize("session", ["0", "1", "-4200", "4200; rm -rf /", "", "  "])
    def test_a_session_that_would_reach_past_the_run_is_not_used(self, session: str):
        """`kill -KILL -1` signals every process the caller may reach.

        The file is the guest's to write, and the argument is negated before it is used, so
        `1` here is the supervisor's own `exec` and every other run in the sandbox. Anything
        that is not a plain number above 1 falls back to the pid, which is still stopped.
        """
        guest = _GuestThatRecordsTheKill([], finish=False, pid="4242", session=session)
        with pytest.raises(SandboxProgramTimeout):
            _run(guest, HostToolRun(_registry()), timeout=0.2)
        assert guest.kills == ["kill -KILL 4242 2>/dev/null"], guest.kills

    def test_the_reach_attribute_carries_the_group(self):
        """The machine-readable half. A host deciding whether it still has to dispose
        the sandbox reads this, not the sentence.
        """
        guest = _GuestThatRecordsTheKill([], finish=False, pid="4242", session="4200")
        with pytest.raises(SandboxProgramTimeout) as expired:
            _run(guest, HostToolRun(_registry()), timeout=0.2)
        assert expired.value.reach == "group"

    def test_the_reach_attribute_carries_a_lone_pid(self):
        guest = _GuestThatRecordsTheKill([], finish=False, pid="4242", session=None)
        with pytest.raises(SandboxProgramTimeout) as expired:
            _run(guest, HostToolRun(_registry()), timeout=0.2)
        assert expired.value.reach == "program"

    def test_reach_is_nothing_when_no_signal_was_sent(self):
        """The default, and what an unsignalled program is owed: no claim at all."""
        guest = _GuestThatRecordsTheKill([], finish=False, pid=None)
        with pytest.raises(SandboxProgramTimeout) as expired:
            _run(guest, HostToolRun(_registry()), timeout=0.2)
        assert expired.value.reach == "nothing"

    def test_the_message_says_the_group_went(self):
        guest = _GuestThatRecordsTheKill([], finish=False, pid="4242", session="4200")
        with pytest.raises(SandboxProgramTimeout) as expired:
            _run(guest, HostToolRun(_registry()), timeout=0.2)
        assert "process group" in str(expired.value)

    def test_a_session_file_the_launcher_never_announced_is_refused(self):
        """The file is inside the run, so a program can write one whether or not
        a session exists.

        On a guest without `setsid` the group it names is the launcher's own — the whole
        container, including the supervisor's `exec` and every other run in the sandbox.
        The branch is taken from what the launcher printed, so a planted file buys nothing.
        """
        guest = _GuestThatRecordsTheKill(
            [], finish=False, pid="4242", session="7", announces_session=False
        )
        with pytest.raises(SandboxProgramTimeout) as expired:
            _run(guest, HostToolRun(_registry()), timeout=0.2)
        assert guest.kills == ["kill -KILL 4242 2>/dev/null"], guest.kills
        assert "process group" not in str(expired.value)

    def test_the_message_says_a_lone_kill_leaves_children(self):
        """The claim varies by image, so it is reported rather than hidden."""
        guest = _GuestThatRecordsTheKill([], finish=False, pid="4242", session=None)
        with pytest.raises(SandboxProgramTimeout) as expired:
            _run(guest, HostToolRun(_registry()), timeout=0.2)
        assert "still running" in str(expired.value)
        assert "process group" not in str(expired.value)

    def test_the_launcher_records_the_session_from_inside_it(self):
        """`$$` and not `$!`, and from the shell `setsid` runs.

        `setsid` execs in place when its caller is not a process-group leader and forks when
        it is, so the outer `$!` is the session leader on some backends and a spent
        intermediary on others. The process it ends up exec'ing leads the session either
        way, so the shell that writes this is the right one.
        """
        layout = guest_run_layout("/maf-sandbox/work/run-1")
        script = launcher_script(layout)
        inner = shlex.split(_branch(script, setsid=True).removesuffix(" &"))[3]

        assert inner.startswith(f"printf %s $$ > '{layout.session}.part'"), inner[:120]
        assert f"mv '{layout.session}.part' '{layout.session}'" in inner
        assert "$$" not in _branch(script, setsid=False), (
            "the fallback records a session it did not make"
        )


class TestAStopThatDidNotReachEverythingNotesTheCall:
    """The transport tells the running tool call when its sandbox is not clean after a stop.

    Only a signal to the whole process group says what the program spawned went with it.
    Anything less leaves something that can write a path back once the call's directory is
    removed, so the framework's cleanup has to dispose the sandbox — and it learns that from
    this note, not from the message a kind shows the model.
    """

    def _noted(self, guest) -> list[str]:
        async def drive() -> list[str]:
            notes, token = open_unclean_notes()
            try:
                with pytest.raises(SandboxProgramTimeout):
                    await host_tool_calls_over_exec(
                        guest, HostToolRun(_registry()), _LAYOUT, timeout=0.2, poll_interval=_FAST
                    )
            finally:
                close_unclean_notes(token)
            return [reason for _sandbox, reason in notes]

        return asyncio.run(drive())

    def test_a_group_signal_leaves_no_note(self):
        guest = _GuestThatRecordsTheKill([], finish=False, pid="4242", session="4200")
        assert self._noted(guest) == []

    def test_a_lone_pid_signal_notes_what_it_left(self):
        guest = _GuestThatRecordsTheKill([], finish=False, pid="4242", session=None)
        notes = self._noted(guest)
        assert len(notes) == 1
        assert "reaches it alone" in notes[0]

    def test_a_signal_that_could_not_be_sent_notes_it(self):
        guest = _GuestThatRecordsTheKill([], finish=False, pid=None)
        notes = self._noted(guest)
        assert len(notes) == 1
        assert "could not be signalled" in notes[0]

    def test_outside_a_call_the_note_goes_nowhere(self):
        """A transport driven directly has no call to note, and must not fail for it."""
        guest = _GuestThatRecordsTheKill([], finish=False, pid="4242", session=None)
        with pytest.raises(SandboxProgramTimeout):
            _run(guest, HostToolRun(_registry()), timeout=0.2)

    def test_an_upload_failure_before_the_launcher_ran_notes_nothing(self):
        """A backend error writing the launcher started no program, so the finally must not
        stop-and-note over it — noting would dispose a clean sandbox, and maybe a sibling."""

        class _FailsTheUpload(_ScriptedGuest):
            async def write_file(
                self, path: str, content: str | bytes, *, working_directory: str
            ) -> None:
                if path == _LAYOUT.launcher:
                    raise RuntimeError("upload boom")
                await super().write_file(path, content, working_directory=working_directory)

        async def drive() -> list[str]:
            notes, token = open_unclean_notes()
            try:
                with pytest.raises(RuntimeError, match="upload boom"):
                    await host_tool_calls_over_exec(
                        _FailsTheUpload([]),
                        HostToolRun(_registry()),
                        _LAYOUT,
                        timeout=0.5,
                        poll_interval=_FAST,
                    )
            finally:
                close_unclean_notes(token)
            return [reason for _sandbox, reason in notes]

        assert asyncio.run(drive()) == []


class TestStoppingARunThatOverran:
    """A dispatched program that overruns is signalled, and no more than signalled (#375).

    What these establish is that a `SIGKILL` was aimed at the pid the launcher wrote — the
    most this transport can prove, for the reasons `_stop_the_program` sets out. The program
    starts detached, so the timeout fires in the supervisor rather than inside an `exec` a
    backend could tear down with its container; the pid and that same `exec` are what stopping
    has to work with, which is why it needs no protocol method and no capability past the
    `EXEC` and `FILES_OUT` the dispatch path already requires.
    """

    def test_the_program_is_killed_by_the_pid_the_launcher_wrote(self):
        guest = _GuestThatRecordsTheKill([], finish=False, pid="4242")
        with pytest.raises(SandboxProgramTimeout):
            _run(guest, HostToolRun(_registry()), timeout=0.2)
        assert len(guest.kills) == 1, guest.commands
        assert "4242" in guest.kills[0]
        assert "-KILL" in guest.kills[0], "a runaway that already overran is not owed a TERM"

    def test_a_landed_kill_is_not_narrated(self):
        """A stopped program is what a timeout should mean; saying so every time is noise."""
        guest = _GuestThatRecordsTheKill([], finish=False, kill_exit_code=0)
        with pytest.raises(SandboxProgramTimeout) as expired:
            _run(guest, HostToolRun(_registry()), timeout=0.2)
        assert "may still be running" not in str(expired.value)

    def test_a_kill_that_did_not_land_says_the_program_may_still_be_running(self):
        """The one case that costs somebody something, so the one that gets the words."""
        guest = _GuestThatRecordsTheKill([], finish=False, kill_exit_code=1)
        with pytest.raises(SandboxProgramTimeout) as expired:
            _run(guest, HostToolRun(_registry()), timeout=0.2)
        assert "may still be running" in str(expired.value)

    def test_no_pid_hedges_rather_than_going_quiet(self):
        """The launcher returned 0, so something started; a missing pid does not say otherwise.

        The hedge errs towards a needless disposal rather than a silent leak: a caller told
        that nothing is running has no way back, while one told to check does.
        """
        guest = _GuestThatRecordsTheKill([], finish=False, pid=None)
        with pytest.raises(SandboxProgramTimeout) as expired:
            _run(guest, HostToolRun(_registry()), timeout=0.2)
        assert guest.kills == [], "a kill was issued with no pid to aim it at"
        assert "may still be running" in str(expired.value)

    def test_a_pid_that_is_not_a_number_is_never_spliced_into_a_kill(self):
        guest = _GuestThatRecordsTheKill([], finish=False, pid="; rm -rf /")
        with pytest.raises(SandboxProgramTimeout):
            _run(guest, HostToolRun(_registry()), timeout=0.2)
        assert guest.kills == [], f"a non-numeric pid reached a command: {guest.commands}"

    def test_the_exit_marker_is_read_before_anything_is_killed(self):
        """A finished run must not be killed by pid: the guest may have reused the number.

        The marker landing is what proves the process is gone, and a pid whose process is gone
        can belong to something else by the time this looks.
        """
        guest = _GuestThatRecordsTheKill([], finish=True)
        result = _run(guest, HostToolRun(_registry()), timeout=5.0)
        assert result.exit_code == 0
        assert guest.kills == [], "a finished run was killed by pid"

    def test_a_backend_that_refuses_the_kill_does_not_replace_the_runs_own_reason(self):
        """The kill is a remedy on the way out, and a remedy must never win over the report."""

        class _RefusesToKill(_GuestThatRecordsTheKill):
            async def exec(self, command: str | Any, *, working_directory: str, timeout: float):
                if "kill" in str(command):
                    raise PermissionError("the daemon said no")
                return await super().exec(
                    command, working_directory=working_directory, timeout=timeout
                )

        with pytest.raises(SandboxProgramTimeout, match="did not finish within"):
            _run(_RefusesToKill([], finish=False), HostToolRun(_registry()), timeout=0.2)

    def test_an_unreadable_pid_does_not_replace_the_runs_own_reason(self):
        class _DiesOnThePidRead(_GuestThatRecordsTheKill):
            async def read_file(self, path: str, *, working_directory: str, max_bytes: int):
                if path == _LAYOUT.pid:
                    raise PermissionError("the daemon said no")
                return await super().read_file(
                    path, working_directory=working_directory, max_bytes=max_bytes
                )

        with pytest.raises(SandboxProgramTimeout, match="did not finish within"):
            _run(_DiesOnThePidRead([], finish=False), HostToolRun(_registry()), timeout=0.2)


class TestTheLegWhereTheLauncherItselfRanOut:
    """`exec` bounding the launcher cannot say whether a program was started, so the pid does."""

    def test_a_pid_left_behind_is_killed_and_reported(self):
        class _BoundsTheStartAfterLaunching(_GuestThatRecordsTheKill):
            async def exec(self, command: str | Any, *, working_directory: str, timeout: float):
                self.commands.append(str(command))
                if "kill" in str(command):
                    return ExecResult(stdout="", exit_code=0)
                # Started, and then the call itself ran out — the case the docstring calls out.
                self.files[_LAYOUT.pid] = b"4242"
                raise TimeoutError

        guest = _BoundsTheStartAfterLaunching([], finish=False)
        with pytest.raises(SandboxProgramTimeout) as expired:
            _run(guest, HostToolRun(_registry()), timeout=30.0)
        assert "4242" in "".join(guest.kills)
        assert "it had started the program and was sent SIGKILL" in str(expired.value)

    def test_no_pid_leaves_the_message_exactly_as_it_was(self):
        """A missing pid claims nothing either way — pinned on the whole sentence."""

        class _BoundsTheStartSilently(_ScriptedGuest):
            async def exec(self, command: str | Any, *, working_directory: str, timeout: float):
                raise TimeoutError

        with pytest.raises(SandboxProgramTimeout) as expired:
            _run(_BoundsTheStartSilently([], finish=False), HostToolRun(_registry()), timeout=30.0)
        assert str(expired.value) == (
            "the run's 30s were gone while starting the program"
            " (whether it got as far as starting one could not be established)"
        )


class TestThePidFileIsTheLayoutsOwn:
    def test_a_program_cannot_be_named_for_it(self):
        for name in ("program_pid", "program_pid.part"):
            with pytest.raises(ValueError, match="name this layout already uses"):
                guest_run_layout("/runs/one", program=name)

    def test_it_sits_beside_the_exit_marker_where_a_model_cannot_name_into(self):
        layout = guest_run_layout("/runs/one")
        assert layout.pid == posixpath.join(posixpath.dirname(layout.exit_code), "program_pid")
        assert not layout.pid.startswith(layout.work + "/")


class TestThePidAgainstARealShell:
    """What the launcher writes down, asked of a shell rather than of a reading of one.

    Gated on a real POSIX platform rather than only on `sh` being present: Git Bash answers
    `which sh` on Windows, and the pid it reports is not the one `os.kill` takes there. These
    run on Linux — in CI, and in a container locally.
    """

    @staticmethod
    def _laid_out(tmp_path: Path) -> GuestRunLayout:
        directory = tmp_path.as_posix()
        served = f"{directory}/host_tools"
        pathlib.Path(served).mkdir(parents=True, exist_ok=True)
        pathlib.Path(f"{directory}/work").mkdir(parents=True, exist_ok=True)
        return GuestRunLayout(
            directory=directory,
            work=f"{directory}/work",
            program=f"{served}/program.py",
            shim=f"{served}/{SHIM_MODULE}",
            launcher=f"{served}/run_program.sh",
            calls=f"{served}/{CALLS_DIRECTORY}",
            output=f"{served}/program_output.txt",
            exit_code=f"{served}/program_exit_code",
            pid=f"{served}/program_pid",
            session=f"{served}/program_session",
        )

    @staticmethod
    def _appears(path: str, *, within: float = 30.0) -> str:
        deadline = time.monotonic() + within
        while time.monotonic() < deadline:
            found = pathlib.Path(path)
            if found.exists():
                text = found.read_text(encoding="utf-8").strip()
                if text:
                    return text
            time.sleep(0.05)
        raise AssertionError(f"{path} never appeared")

    @pytest.mark.skipif(shutil.which("sh") is None, reason="needs a POSIX shell")
    @pytest.mark.skipif(os.pathsep != ":", reason="pids here are not the ones os.kill takes")
    def test_the_pid_written_down_is_the_programs_own(self, tmp_path: Path):
        """Not the wrapper shell's, which is what a naive `$!` on the `nohup` would record.

        The distinction is the whole fix: killing the wrapper leaves the interpreter orphaned
        and running, which is indistinguishable from not having killed anything at all. So the
        program reports its own pid and the two are compared.
        """
        layout = self._laid_out(tmp_path)
        pathlib.Path(layout.program).write_text("import os\nprint(os.getpid())\n", encoding="utf-8")
        pathlib.Path(layout.launcher).write_text(
            launcher_script(layout, sys.executable), encoding="utf-8"
        )

        subprocess.run(
            ["sh", layout.launcher], cwd=layout.directory, capture_output=True, timeout=60
        )
        self._appears(layout.exit_code)

        recorded = self._appears(layout.pid)
        printed = pathlib.Path(layout.output).read_text(encoding="utf-8").strip()
        assert recorded == printed, (
            f"the launcher recorded {recorded} but the program is {printed} — the pid is the "
            "wrapper's, and killing it would leave the program running"
        )

    @pytest.mark.skipif(shutil.which("setsid") is None, reason="needs setsid")
    @pytest.mark.skipif(not os.path.isdir("/proc"), reason="reads process state from /proc")
    def test_the_group_kill_stops_the_program_and_its_child(self, tmp_path: Path):
        """The whole point of the session, end to end against a real shell.

        Everything else about the group form is asserted against a double that reads the
        command string, so a launcher that put the interpreter in a process group of its own
        would keep those green while the signal reached the wrapper shell alone.
        """
        layout = self._laid_out(tmp_path)
        kid = f"{tmp_path}/kid"
        pathlib.Path(layout.program).write_text(
            "import subprocess, pathlib, time"
            + chr(10)
            + 'child = subprocess.Popen(["sh", "-c", "while :; do sleep 1; done"])'
            + chr(10)
            + f'pathlib.Path("{kid}").write_text(str(child.pid))'
            + chr(10)
            + "while True:"
            + chr(10)
            + "    time.sleep(0.05)"
            + chr(10),
            encoding="utf-8",
        )
        pathlib.Path(layout.launcher).write_text(
            launcher_script(layout, sys.executable), encoding="utf-8"
        )

        def state(pid: str) -> str:
            """`Z` or gone is dead; nothing here reaps an orphan.

            Read without asking first: the entry can go between an `exists()` and the read,
            and the poll below is waiting for exactly that, so a vanished one is the answer
            rather than an error on the way to it.
            """
            try:
                return pathlib.Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").split()[2]
            except OSError:
                return "gone"

        subprocess.run(
            ["sh", layout.launcher], cwd=layout.directory, capture_output=True, timeout=60
        )
        session = self._appears(layout.session)
        child = self._appears(kid)

        assert state(child) not in ("Z", "gone"), "the child never ran"
        pgid = pathlib.Path(f"/proc/{child}/stat").read_text(encoding="utf-8").split()[4]
        assert pgid == session, (
            f"the child is in group {pgid} and the recorded session is {session}, so the "
            "signal would miss it"
        )

        # Through `sh`, because `kill` is a builtin on images that ship no such binary —
        # and because that is the shape the transport sends over `exec`.
        subprocess.run(["sh", "-c", f"kill -KILL -{session}"], capture_output=True, timeout=30)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if state(child) in ("Z", "gone"):
                break
            time.sleep(0.1)
        assert state(child) in ("Z", "gone"), (
            "the spawned child outlived the group kill, which is what #437 was about"
        )

    @pytest.mark.skipif(shutil.which("setsid") is None, reason="needs setsid")
    def test_a_real_shell_records_the_session_the_program_is_in(self, tmp_path: Path):
        """The launcher's claim about the session, checked against the kernel.

        `setsid` execs in place or forks depending on its caller, so the recorded value
        only means something if it is the session the program actually ended up in.
        """
        layout = self._laid_out(tmp_path)
        pathlib.Path(layout.program).write_text(
            "import os\nprint(os.getsid(0))\n", encoding="utf-8"
        )
        pathlib.Path(layout.launcher).write_text(
            launcher_script(layout, sys.executable), encoding="utf-8"
        )

        started = subprocess.run(
            ["sh", layout.launcher], cwd=layout.directory, capture_output=True, timeout=60
        )
        self._appears(layout.exit_code)

        assert (
            self._appears(layout.session)
            == pathlib.Path(layout.output).read_text(encoding="utf-8").strip()
        ), (
            "the recorded session is not the one the program is in, so killing that group reaches nothing"
        )
        assert SESSION_MADE in started.stdout.decode(), (
            "a session was made and the launcher did not say so, so the host will not use it"
        )

    @pytest.mark.skipif(shutil.which("sh") is None, reason="needs a POSIX shell")
    @pytest.mark.skipif(os.pathsep != ":", reason="pids here are not the ones os.kill takes")
    def test_killing_the_recorded_pid_stops_a_program_that_would_not_stop(self, tmp_path: Path):
        """The end-to-end claim, minus the backend: this pid, this signal, that program gone."""
        layout = self._laid_out(tmp_path)
        pathlib.Path(layout.program).write_text(
            "import time\nwhile True:\n    time.sleep(0.05)\n", encoding="utf-8"
        )
        pathlib.Path(layout.launcher).write_text(
            launcher_script(layout, sys.executable), encoding="utf-8"
        )

        subprocess.run(
            ["sh", layout.launcher], cwd=layout.directory, capture_output=True, timeout=60
        )
        running = int(self._appears(layout.pid))

        # Alive first, or "gone after the kill" would pass against a program that never started.
        os.kill(running, 0)
        os.kill(running, _SIGKILL)

        deadline = time.monotonic() + 30.0
        while time.monotonic() < deadline:
            try:
                os.kill(running, 0)
            except ProcessLookupError:
                return
            time.sleep(0.05)
        # Deliberately not signalled again. By now the number may belong to something else —
        # the wait above is exactly long enough for the guest to have recycled it — and a
        # second SIGKILL would land on a stranger on the machine running the tests. A pid
        # that outlived a SIGKILL is a failure worth reporting, not one worth chasing.
        raise AssertionError(
            f"{running} survived SIGKILL, so the recorded pid is not the program; it may "
            f"still be running on this machine"
        )

    @pytest.mark.skipif(shutil.which("sh") is None, reason="needs a POSIX shell")
    @pytest.mark.skipif(os.pathsep != ":", reason="the launcher's path handling is POSIX")
    def test_the_exit_code_still_means_the_programs_own_status(self, tmp_path: Path):
        """`wait $!` is what keeps that true now the interpreter runs in the background.

        Without it `$?` would be the `printf` that recorded the pid — zero, whatever the
        program did, which would report every failing program as a success.
        """
        layout = self._laid_out(tmp_path)
        pathlib.Path(layout.program).write_text("raise SystemExit(3)\n", encoding="utf-8")
        pathlib.Path(layout.launcher).write_text(
            launcher_script(layout, sys.executable), encoding="utf-8"
        )

        subprocess.run(
            ["sh", layout.launcher], cwd=layout.directory, capture_output=True, timeout=60
        )
        assert self._appears(layout.exit_code) == "3"


class TestTheLaunchersPidMarker:
    def test_the_pid_lands_by_rename(self):
        """The same discipline the exit marker gets, and for the same reason.

        Written straight to its name, the supervisor can read the empty file a redirection
        leaves for a moment; an empty pid is discarded, so the run it could have stopped is
        left going instead. Rare, and silent, which is the pair worth a rename.
        """
        layout = guest_run_layout("/maf-sandbox/work/run-1")
        inner = shlex.split(_branch(launcher_script(layout), setsid=True).removesuffix(" &"))[3]
        assert f"{layout.pid}.part" in inner, "the pid is written straight to its name"
        assert f"mv '{layout.pid}.part' '{layout.pid}'" in inner


class TestStoppingOnTheOtherTwoLegs:
    """Leg A is the deadline seen at the top of the loop; these are the other two."""

    def test_a_deadline_that_expires_inside_an_iteration_still_kills(self):
        """The `_DeadlineExpired` path, which reports the call that ran out rather than the loop.

        A separate leg with a separate message, so a kill wired into one and not the other is
        invisible to a test that only drives the loop's own deadline.
        """
        ahead = {"seconds": 0.0}

        class _RunsOutMidIteration(_GuestThatRecordsTheKill):
            expired = False

            async def stat_file(self, path: str, *, working_directory: str):
                if not self.expired and path == _LAYOUT.exit_code:
                    # Jump the clock past the deadline while a transport call is in flight, so
                    # the expiry surfaces as `_DeadlineExpired` rather than at the top of the
                    # loop. `_Shifted` is what the module reads the time through.
                    self.expired = True
                    ahead["seconds"] += 600.0
                return await super().stat_file(path, working_directory=working_directory)

        guest = _RunsOutMidIteration([], finish=False)
        with pytest.MonkeyPatch.context() as patched:
            patched.setattr(host_tools_over_exec, "time", _Shifted(time.monotonic, ahead))
            with pytest.raises(SandboxProgramTimeout) as expired:
                _run(guest, HostToolRun(_registry()), timeout=5.0)

        assert "4242" in "".join(guest.kills), f"nothing was killed: {guest.commands}"
        assert "may still be running" not in str(expired.value)

    def test_a_pid_that_cannot_be_used_still_hedges(self):
        """`absent` is reserved for no pid at all; anything unusable is reported as running.

        A pid file holding something that is not a number is no handle on a process — but the
        guest can put it there, so treating it as "nothing was started" would be a one-line
        opt-out from being killed *and* from being mentioned. The signal is still not sent.
        """

        class _BoundsTheStartOverGarbage(_GuestThatRecordsTheKill):
            async def exec(self, command: str | Any, *, working_directory: str, timeout: float):
                self.commands.append(str(command))
                if "kill" in str(command):
                    return ExecResult(stdout="", exit_code=0)
                self.files[_LAYOUT.pid] = b"not-a-pid"
                raise TimeoutError

        guest = _BoundsTheStartOverGarbage([], finish=False)
        with pytest.raises(SandboxProgramTimeout) as expired:
            _run(guest, HostToolRun(_registry()), timeout=30.0)

        assert guest.kills == [], f"a non-numeric pid reached a command: {guest.commands}"
        assert "may still be running" in str(expired.value)

    def test_a_pid_that_was_never_written_says_nothing_new(self):
        """No pid, and no kill to attempt — but the sentence still may not say nothing ran."""

        class _BoundsTheStartWithNothingWritten(_GuestThatRecordsTheKill):
            async def exec(self, command: str | Any, *, working_directory: str, timeout: float):
                self.commands.append(str(command))
                if "kill" in str(command):
                    return ExecResult(stdout="", exit_code=0)
                raise TimeoutError

        guest = _BoundsTheStartWithNothingWritten([], finish=False, pid=None)
        with pytest.raises(SandboxProgramTimeout) as expired:
            _run(guest, HostToolRun(_registry()), timeout=30.0)

        assert guest.kills == []
        assert str(expired.value) == (
            "the run's 30s were gone while starting the program"
            " (whether it got as far as starting one could not be established)"
        )

    def test_a_pid_the_host_cannot_read_hedges_rather_than_going_quiet(self):
        """A backend failure is not evidence that nothing was started."""

        class _PidUnreadable(_GuestThatRecordsTheKill):
            async def exec(self, command: str | Any, *, working_directory: str, timeout: float):
                self.commands.append(str(command))
                if "kill" in str(command):
                    return ExecResult(stdout="", exit_code=0)
                self.files[_LAYOUT.pid] = b"4242"
                raise TimeoutError

            async def read_file(self, path: str, *, working_directory: str, max_bytes: int):
                if path == _LAYOUT.pid:
                    raise PermissionError("the daemon said no")
                return await super().read_file(
                    path, working_directory=working_directory, max_bytes=max_bytes
                )

        with pytest.raises(SandboxProgramTimeout) as expired:
            _run(_PidUnreadable([], finish=False), HostToolRun(_registry()), timeout=30.0)

        assert "may still be running" in str(expired.value)


class _GuestThatRecordsRemovals(_GuestThatRecordsTheKill):
    """Names the base's reclaim record beside the kill record, for the tests about cleanup."""

    @property
    def removals(self) -> list[str]:
        return self.reclaimed


class TestTheTransportReclaimsItsOwnFiles:
    """`Sandbox.reclaim`, not `Sandbox.remove`: requiring `FILES_DELETE` would cut off a
    backend that serves dispatch and withholds it, and the reclaim is behind no capability.

    The files it writes carry a run's host-tool traffic — every argument a program passed to a
    host tool and every value it got back — plus the program a model wrote. Left behind, they
    are readable by the next run in the same sandbox, because `acquire` is get-or-create.
    """

    def test_a_successful_run_does_not_leave_its_files_behind(self):
        """The common path, and the one a cleanup wired only into failure would miss."""
        guest = _GuestThatRecordsRemovals([], finish=True)
        result = _run(guest, HostToolRun(_registry()), timeout=5.0)
        assert result.exit_code == 0
        assert guest.removals == [posixpath.dirname(_LAYOUT.shim)], guest.commands

    def test_a_run_that_timed_out_does_not_leave_its_files_behind(self):
        guest = _GuestThatRecordsRemovals([], finish=False)
        with pytest.raises(SandboxProgramTimeout):
            _run(guest, HostToolRun(_registry()), timeout=0.2)
        assert len(guest.removals) == 1, guest.commands

    def test_a_launcher_that_never_started_the_program_still_reclaims(self):
        guest = _GuestThatRecordsRemovals([], finish=False, launcher_exit_code=1)
        result = _run(guest, HostToolRun(_registry()), timeout=5.0)
        assert result.exit_code != 0
        assert len(guest.removals) == 1, guest.commands

    def test_a_backend_failing_mid_run_still_reclaims(self):
        """`finally`, not a happy-path call: whatever a backend raises, the files still go."""

        class _DiesMidRun(_GuestThatRecordsRemovals):
            async def stat_file(self, path: str, *, working_directory: str):
                if path == _LAYOUT.exit_code:
                    raise PermissionError("the daemon said no")
                return await super().stat_file(path, working_directory=working_directory)

        guest = _DiesMidRun([], finish=False)
        with pytest.raises(PermissionError):
            _run(guest, HostToolRun(_registry()), timeout=5.0)
        assert len(guest.removals) == 1, guest.commands

    def test_the_work_directory_is_left_for_the_kind_to_collect_from(self):
        """The split that makes this safe on the success path.

        Artifacts live in `work` and a kind collects them *after* this returns, so a transport
        that removed the run would delete the outputs of every successful run. It removes its
        own sibling and nothing else.
        """
        guest = _GuestThatRecordsRemovals([], finish=True)
        _run(guest, HostToolRun(_registry()), timeout=5.0)
        (removed,) = guest.removals
        assert removed == posixpath.dirname(_LAYOUT.shim)
        # What that equality stands for: neither the run nor the model's files went with it.
        assert removed != _LAYOUT.directory
        assert guest_path_relative_to(_LAYOUT.work, removed) is None

    def test_a_refused_removal_says_what_is_left_readable(self, caplog):
        guest = _GuestThatRecordsRemovals(
            [], finish=True, reclaim_error=PermissionError("the daemon said no")
        )
        with caplog.at_level(logging.WARNING, logger="maf_sandbox"):
            _run(guest, HostToolRun(_registry()), timeout=5.0)
        assert guest.removals == [posixpath.dirname(_LAYOUT.shim)], "no removal was attempted"
        assert "readable by the next run" in caplog.text, caplog.text

    def test_a_backend_that_refuses_the_removal_does_not_fail_the_run(self):
        """A run that worked must not be reported as failed because its cleanup did not."""
        guest = _GuestThatRecordsRemovals(
            [], finish=True, reclaim_error=PermissionError("the daemon said no")
        )
        assert _run(guest, HostToolRun(_registry()), timeout=5.0).exit_code == 0
        assert guest.removals == [posixpath.dirname(_LAYOUT.shim)], "no removal was attempted"


class TestReclaimingTheWholeRun:
    """`reclaim_run` is the kind's half — the work directory, and everything a model wrote."""

    def test_it_removes_the_run_directory(self):
        guest = _GuestThatRecordsRemovals([], finish=True)
        assert asyncio.run(reclaim_run(guest, _LAYOUT, timeout=5.0)) is True
        assert guest.removals == [_LAYOUT.directory]

    def test_a_refusal_is_reported_rather_than_raised(self):
        guest = _GuestThatRecordsRemovals(
            [], finish=True, reclaim_error=PermissionError("the daemon said no")
        )
        assert asyncio.run(reclaim_run(guest, _LAYOUT, timeout=5.0)) is False
        assert guest.removals == [_LAYOUT.directory], "no removal was attempted"

    def test_a_backend_failure_is_reported_rather_than_raised(self):
        """A sandbox too dead to answer at all, rather than one refusing this removal."""

        class _Refuses(_GuestThatRecordsRemovals):
            async def reclaim(self, directory: str, *, working_directory: str, timeout: float):
                raise ConnectionError("the daemon is gone")

        assert asyncio.run(reclaim_run(_Refuses([], finish=True), _LAYOUT, timeout=5.0)) is False


class TestWhatIsTooBroadToDelete:
    """`rm -rf` is irreversible, so the path gets a guard that does not depend on the factory."""

    @pytest.mark.parametrize(
        "directory",
        [
            "/",
            "/tmp",
            "relative/run",
            "",
            "/runs/../..",
            # Two components as written, one as meant. `rm` happens to refuse a `.` operand,
            # but the guard must answer for the directory rather than its spelling.
            "/tmp/.",
            "/etc/./",
        ],
    )
    def test_a_path_that_is_not_a_run_directory_is_refused(self, directory: str):
        from maf_sandbox._host_tools_over_exec import _removable

        assert not _removable(directory)

    def test_a_real_run_directory_passes(self):
        from maf_sandbox._host_tools_over_exec import _removable

        assert _removable("/maf-sandbox/work/run-1")

    def test_nothing_is_removed_for_a_path_it_refuses(self):
        broken = GuestRunLayout(
            directory="/",
            work="/work",
            program="/program.py",
            shim="/maf_host_tools.py",
            launcher="/run_program.sh",
            calls="/host_tool_calls",
            output="/program_output.txt",
            exit_code="/program_exit_code",
            pid="/program_pid",
        )
        guest = _GuestThatRecordsRemovals([], finish=True)
        assert asyncio.run(reclaim_run(guest, broken, timeout=5.0)) is False
        assert guest.removals == [], f"a refused path still reached the backend: {guest.removals}"

    def test_a_session_outside_the_run_is_refused_like_every_other_stray(self):
        """The newest path in the layout gets the check the older ones have.

        Missing it, a hand-built layout could put the session file outside the run and this
        would delete the run, answer `True`, and leave that file behind.
        """
        astray = GuestRunLayout(
            directory="/maf-sandbox/work/run-1",
            work="/maf-sandbox/work/run-1/work",
            program="/maf-sandbox/work/run-1/host_tools/program.py",
            shim="/maf-sandbox/work/run-1/host_tools/maf_host_tools.py",
            launcher="/maf-sandbox/work/run-1/host_tools/run_program.sh",
            calls="/maf-sandbox/work/run-1/host_tools/host_tool_calls",
            output="/maf-sandbox/work/run-1/host_tools/program_output.txt",
            exit_code="/maf-sandbox/work/run-1/host_tools/program_exit_code",
            pid="/maf-sandbox/work/run-1/host_tools/program_pid",
            session="/elsewhere/program_session",
        )
        guest = _GuestThatRecordsRemovals([], finish=True)
        assert asyncio.run(reclaim_run(guest, astray, timeout=5.0)) is False
        assert guest.removals == [], f"a stray session still reached the backend: {guest.removals}"


class TestRemovalAgainstARealFilesystem:
    """A fake proves a directory was named; only a filesystem proves what removing it takes."""

    @pytest.mark.skipif(os.pathsep != ":", reason="the guest paths here are POSIX")
    def test_the_directory_the_transport_names_is_the_one_that_should_go(self, tmp_path: Path):
        """The directory comes from a dispatch, not hand-built, so the two cannot diverge.

        How a backend removes it is the backend's; what this pins is that a real recursive
        removal of the directory core hands over takes the transport's files and no more.
        """
        directory = (tmp_path / "run-1").as_posix()
        served = f"{directory}/host_tools"
        pathlib.Path(served).mkdir(parents=True, exist_ok=True)
        pathlib.Path(served, "program_output.txt").write_text("secret output", encoding="utf-8")
        pathlib.Path(served, "host_tool_calls").mkdir()
        pathlib.Path(served, "host_tool_calls", "0001.request.json").write_text(
            '{"arguments": {"account": "sensitive"}}', encoding="utf-8"
        )

        layout = guest_run_layout(directory)
        guest = _GuestThatRecordsRemovals([], finish=True)
        asyncio.run(
            host_tools_over_exec._reclaim_the_transports_own(
                guest, layout, until=time.monotonic() + 5.0
            )
        )
        (named,) = guest.removals

        shutil.rmtree(named)

        assert not pathlib.Path(served).exists(), "the transport's files survived the removal"
        assert pathlib.Path(directory).exists(), "the run directory went with them"


class TestQuotingAgainstARealShell:
    """`_quote` is what keeps a hostile directory name one word in the commands still built
    from one — the launcher's `cd` and `sh`, and the kill. Asked of a shell, because that is
    whose grammar it has to survive; a destructive command is used because the consequence of
    getting it wrong is easiest to see there."""

    @pytest.mark.skipif(shutil.which("sh") is None, reason="needs a POSIX shell")
    @pytest.mark.skipif(os.pathsep != ":", reason="the guest paths here are POSIX")
    def test_a_directory_holding_shell_metacharacters_reaches_only_itself(self, tmp_path: Path):
        """An unquoted `*` would take the sibling with it, and `;` would run what follows."""
        from maf_sandbox._host_tools_over_exec import _quote

        awkward = (tmp_path / "run *; touch pwned").as_posix()
        pathlib.Path(awkward).mkdir(parents=True)
        pathlib.Path(awkward, "inside").write_text("x", encoding="utf-8")
        keep = tmp_path / "run-2"
        keep.mkdir()
        (keep / "survivor").write_text("x", encoding="utf-8")

        done = subprocess.run(
            ["sh", "-c", f"rm -rf {_quote(awkward)}"],
            cwd=tmp_path.as_posix(),
            capture_output=True,
            timeout=60,
        )

        assert done.returncode == 0, done.stderr
        assert not pathlib.Path(awkward).exists()
        assert (keep / "survivor").exists(), "the removal reached a directory it did not name"
        assert not (tmp_path / "pwned").exists(), "the name was evaluated as a command"


class TestTheCommandsKeepTheGuestsAnswer:
    """A fake returns the exit code it was told to; only a shell returns the real one.

    Which command status reaches the caller is a property of the shell, so it is asked of one.
    """

    @pytest.mark.skipif(shutil.which("sh") is None, reason="needs a POSIX shell")
    @pytest.mark.skipif(os.pathsep != ":", reason="pids and kill semantics here are POSIX")
    def test_a_kill_that_fails_reports_a_failure(self, tmp_path: Path):
        """The command the transport actually sent, run by a shell.

        Asserting POSIX semantics alone would prove nothing about this code, and asserting the
        string alone would prove nothing about the shell. So the string is taken from a run and
        then executed: nothing is alive at this pid, so a zero status means the transport would
        call a program stopped that it never touched.
        """
        # Above every possible `pid_max` — the kernel's ceiling is 2**22, so this cannot name
        # a process and `kill` fails without signalling anything on the machine running tests.
        gone = 2**31 - 1
        guest = _GuestThatRecordsTheKill([], finish=False, pid=str(gone))
        with pytest.raises(SandboxProgramTimeout):
            _run(guest, HostToolRun(_registry()), timeout=0.2)
        (command,) = guest.kills

        done = subprocess.run(
            ["sh", "-c", command], cwd=tmp_path.as_posix(), capture_output=True, timeout=60
        )
        assert done.returncode != 0, (
            f"{command!r} reported success against a pid that is not there, so a program that "
            "could not be stopped would be reported as stopped"
        )


class TestWhatIsNotAPidWorthSignalling:
    """`$!` is always a positive ASCII integer, and everything else is refused before a kill."""

    @pytest.mark.parametrize(
        "recorded",
        [
            pytest.param("0", id="zero-signals-the-whole-process-group"),
            pytest.param("-1", id="negative-signals-a-group-too"),
            pytest.param("٤٢", id="arabic-indic-digits-int-normalises"),
            pytest.param("²", id="superscript-is-a-digit-to-str-isdigit"),
            pytest.param(" 42 ; rm -rf /", id="a-command-dressed-as-a-pid"),
            pytest.param("", id="empty"),
        ],
    )
    def test_it_never_reaches_a_command(self, recorded: str):
        guest = _GuestThatRecordsTheKill([], finish=False, pid=recorded)
        with pytest.raises(SandboxProgramTimeout):
            _run(guest, HostToolRun(_registry()), timeout=0.2)
        assert guest.kills == [], f"{recorded!r} reached a kill: {guest.commands}"

    def test_a_real_pid_still_does(self):
        """The negative cases above are worthless if the positive one stopped working."""
        guest = _GuestThatRecordsTheKill([], finish=False, pid="42")
        with pytest.raises(SandboxProgramTimeout):
            _run(guest, HostToolRun(_registry()), timeout=0.2)
        assert len(guest.kills) == 1 and " 42 " in f" {guest.kills[0]} "


class TestStoppingGetsItsOwnBudget:
    """The diagnostics on the way out must not be able to spend the kill's time."""

    def test_a_slow_final_read_does_not_cost_the_program_its_kill(self):
        """The last look and the output read share a grace; stopping gets a fresh one.

        Sharing it means a backend slow enough to exhaust that grace leaves the kill with a
        deadline already past — it gives up before sending anything, and the runaway survives
        with the message saying only that it may have.
        """
        ahead = {"seconds": 0.0}

        class _SlowFinalRead(_GuestThatRecordsTheKill):
            async def read_file(self, path: str, *, working_directory: str, max_bytes: int):
                if path == _LAYOUT.output:
                    # Spend the whole grace inside the diagnostic read.
                    ahead["seconds"] += host_tools_over_exec._FINAL_READ_GRACE + 1.0
                return await super().read_file(
                    path, working_directory=working_directory, max_bytes=max_bytes
                )

        guest = _SlowFinalRead([], finish=False)
        guest.files[_LAYOUT.output] = b"printed something"
        with pytest.MonkeyPatch.context() as patched:
            patched.setattr(host_tools_over_exec, "time", _Shifted(time.monotonic, ahead))
            with pytest.raises(SandboxProgramTimeout):
                _run(guest, HostToolRun(_registry()), timeout=0.2)

        assert guest.kills != [], (
            "the diagnostics spent the grace and the kill was skipped, so the program is "
            f"still running: {guest.commands}"
        )


class TestWhatTheTransportWillNotDelete:
    """The run directory bounds what the transport's own cleanup may remove."""

    def test_a_transport_directory_outside_the_run_is_refused(self):
        """A hand-built layout is a supported API, and `rm -rf` is irreversible.

        The target is derived from `layout.shim`, so a layout whose shim sits somewhere else
        would otherwise point a recursive delete at a directory that has nothing to do with
        the run — `/etc/ssh` passes the component-count guard perfectly well.
        """
        stray = GuestRunLayout(
            directory="/maf-sandbox/work/run-1",
            work="/maf-sandbox/work/run-1/work",
            program="/etc/ssh/program.py",
            shim=f"/etc/ssh/{SHIM_MODULE}",
            launcher="/etc/ssh/run_program.sh",
            calls=f"/etc/ssh/{CALLS_DIRECTORY}",
            output="/etc/ssh/program_output.txt",
            exit_code="/etc/ssh/program_exit_code",
            pid="/etc/ssh/program_pid",
        )
        guest = _GuestThatRecordsRemovals([], finish=True)
        asyncio.run(
            host_tools_over_exec._reclaim_the_transports_own(
                guest, stray, until=time.monotonic() + 5.0
            )
        )
        assert guest.removals == [], f"a directory outside the run was removed: {guest.removals}"

    @pytest.mark.parametrize(
        "spelling",
        ["/maf-sandbox/work/run-1/", "/maf-sandbox/work/run-1//", "/maf-sandbox/work/run-1/."],
        ids=["trailing-slash", "doubled-separator", "dot-component"],
    )
    def test_the_run_directory_is_refused_however_it_is_spelled(self, spelling):
        """One directory has several spellings, and `GuestRunLayout` refuses none of them.

        This layout puts the transport's files directly in the run, so the directory derived
        from the shim *is* the run — the case the guard exists for. Its `work` sits outside,
        which is what leaves the run-directory comparison as the only thing standing between a
        caller's spelling and a recursive delete of the whole run.
        """
        directly_in_the_run = GuestRunLayout(
            directory=spelling,
            work="/maf-sandbox/elsewhere/work",
            program="/maf-sandbox/work/run-1/program.py",
            shim=f"/maf-sandbox/work/run-1/{SHIM_MODULE}",
            launcher="/maf-sandbox/work/run-1/run_program.sh",
            calls=f"/maf-sandbox/work/run-1/{CALLS_DIRECTORY}",
            output="/maf-sandbox/work/run-1/program_output.txt",
            exit_code="/maf-sandbox/work/run-1/program_exit_code",
            pid="/maf-sandbox/work/run-1/program_pid",
        )
        guest = _GuestThatRecordsRemovals([], finish=True)
        asyncio.run(
            host_tools_over_exec._reclaim_the_transports_own(
                guest, directly_in_the_run, until=time.monotonic() + 5.0
            )
        )
        assert guest.removals == [], f"the run itself was removed: {guest.removals}"

    def test_the_run_s_own_transport_directory_is_not(self):
        guest = _GuestThatRecordsRemovals([], finish=True)
        asyncio.run(
            host_tools_over_exec._reclaim_the_transports_own(
                guest, _LAYOUT, until=time.monotonic() + 5.0
            )
        )
        assert len(guest.removals) == 1, guest.commands
        assert posixpath.dirname(_LAYOUT.shim) in guest.removals[0]


class TestWhatEveryExitPathOwesTheRun:
    """Stopping and reclaiming are owed on paths that are easy to reach and easy to forget."""

    def test_a_slow_pid_read_does_not_cost_the_program_its_signal(self):
        """The read and the signal are separately bounded.

        Sharing one budget means a backend slow enough to spend it on the read leaves nothing
        for the `exec` that carries the kill, so the runaway survives a code path that looks
        like it stopped it.
        """
        ahead = {"seconds": 0.0}

        class _SlowPidRead(_GuestThatRecordsTheKill):
            async def read_file(self, path: str, *, working_directory: str, max_bytes: int):
                if path == _LAYOUT.pid:
                    ahead["seconds"] += host_tools_over_exec._FINAL_READ_GRACE + 1.0
                return await super().read_file(
                    path, working_directory=working_directory, max_bytes=max_bytes
                )

        guest = _SlowPidRead([], finish=False)
        with pytest.MonkeyPatch.context() as patched:
            patched.setattr(host_tools_over_exec, "time", _Shifted(time.monotonic, ahead))
            with pytest.raises(SandboxProgramTimeout):
                _run(guest, HostToolRun(_registry()), timeout=0.2)

        assert guest.kills != [], f"the read spent the kill's budget: {guest.commands}"

    def test_the_starting_leg_returns_a_run_that_finished(self):
        """A lost `exec` reply is not a lost run, and a finished pid may belong to a stranger.

        Reachable when the launcher had time to run to completion but its `exec` reply did not
        arrive. The marker settles it: the program is done, so its exit code is the answer and
        signalling by a pid that may already have been reused would reach something else.
        """

        class _FinishedButTheExecRanOut(_GuestThatRecordsTheKill):
            async def exec(self, command: str | Any, *, working_directory: str, timeout: float):
                self.commands.append(str(command))
                if "kill" in str(command):
                    return ExecResult(stdout="", exit_code=0)
                # The program ran and left both facts behind; only the reply was lost.
                self.files[_LAYOUT.pid] = b"4242"
                self.files[_LAYOUT.exit_code] = b"0"
                raise TimeoutError

        guest = _FinishedButTheExecRanOut([], finish=False)
        finished = _run(guest, HostToolRun(_registry()), timeout=30.0)

        assert finished.exit_code == 0, "a run that finished was reported as a timeout"
        assert guest.kills == [], f"a finished run was killed by pid: {guest.commands}"

    def test_a_run_that_failed_for_another_reason_is_still_stopped(self):
        """The kill belongs to every exit, not only to the ones the transport itself reports.

        A backend failing mid-run leaves a detached program nobody has stopped — and the
        cleanup is about to remove the pid, output and exit marker that would have identified
        it, so nothing later can find it either.
        """

        class _DiesMidRun(_GuestThatRecordsTheKill):
            async def stat_file(self, path: str, *, working_directory: str):
                if path == _LAYOUT.exit_code:
                    raise PermissionError("the daemon said no")
                return await super().stat_file(path, working_directory=working_directory)

        guest = _DiesMidRun([], finish=False)
        with pytest.raises(PermissionError):
            _run(guest, HostToolRun(_registry()), timeout=5.0)

        assert guest.kills != [], f"a failed run left its program running: {guest.commands}"

    def test_a_failed_run_still_stops_the_group_the_launcher_made(self):
        """The emergency stop is the one path with no launcher result of its own.

        It runs after the supervisor, so it needs the launcher's verdict carried out to it;
        the reclaim on the next line removes the files that would identify the children.
        """

        class _DiesMidRun(_GuestThatRecordsTheKill):
            async def stat_file(self, path: str, *, working_directory: str):
                if path == _LAYOUT.exit_code:
                    raise PermissionError("the daemon said no")
                return await super().stat_file(path, working_directory=working_directory)

        guest = _DiesMidRun([], finish=False, pid="4242", session="4200")
        with pytest.raises(PermissionError):
            _run(guest, HostToolRun(_registry()), timeout=5.0)

        assert guest.kills == ["kill -KILL -4200 2>/dev/null"], (
            f"the cleanup fell back to one pid on a guest that had a group: {guest.kills}"
        )

    def test_a_backends_own_program_timeout_is_not_this_runs_own(self):
        """`SandboxProgramTimeout` is public, so a backend may raise one for a bound of its own.

        Deciding on the type alone reads that as this supervisor's timeout — already stopped
        and reported — so the emergency stop is skipped and the reclaim then removes the pid
        that was the only handle on a program still going. Provenance has to decide it, not
        `isinstance`.
        """

        class _RaisesTheSharedType(_GuestThatRecordsTheKill):
            async def stat_file(self, path: str, *, working_directory: str):
                if path == _LAYOUT.exit_code:
                    raise SandboxProgramTimeout("a bound of the backend's own")
                return await super().stat_file(path, working_directory=working_directory)

        guest = _RaisesTheSharedType([], finish=False)
        with pytest.raises(SandboxProgramTimeout) as raised:
            _run(guest, HostToolRun(_registry()), timeout=5.0)

        assert "a bound of the backend's own" in str(raised.value), "the run's reason was lost"
        assert guest.kills != [], f"a backend's timeout skipped the stop: {guest.commands}"

    def test_the_transport_will_not_remove_a_work_directory_beneath_it(self):
        """The other direction: `work` under the transport's own directory goes with it.

        One-directional confinement passes this layout — the transport directory is not inside
        `work` — and `rm -rf` on it takes the model's files and every artifact anyway.
        """
        run = "/maf-sandbox/work/run-1"
        served = f"{run}/host_tools"
        nested = GuestRunLayout(
            directory=run,
            work=f"{served}/work",
            program=f"{served}/program.py",
            shim=f"{served}/{SHIM_MODULE}",
            launcher=f"{served}/run_program.sh",
            calls=f"{served}/{CALLS_DIRECTORY}",
            output=f"{served}/program_output.txt",
            exit_code=f"{served}/program_exit_code",
            pid=f"{served}/program_pid",
        )
        guest = _GuestThatRecordsRemovals([], finish=True)
        asyncio.run(
            host_tools_over_exec._reclaim_the_transports_own(
                guest, nested, until=time.monotonic() + 5.0
            )
        )
        assert guest.removals == [], f"the model's directory went with it: {guest.removals}"

    def test_the_transport_will_not_remove_the_model_s_own_directory(self):
        """Confinement to the run is not enough, because `work` is inside the run.

        A layout whose shim sits in `work` makes the transport's cleanup delete the files a
        kind shared in and every artifact it is about to collect — on the success path, where
        nothing else would look wrong.
        """
        run = "/maf-sandbox/work/run-1"
        collapsed = GuestRunLayout(
            directory=run,
            work=f"{run}/work",
            program=f"{run}/work/program.py",
            shim=f"{run}/work/{SHIM_MODULE}",
            launcher=f"{run}/work/run_program.sh",
            calls=f"{run}/work/{CALLS_DIRECTORY}",
            output=f"{run}/work/program_output.txt",
            exit_code=f"{run}/work/program_exit_code",
            pid=f"{run}/work/program_pid",
        )
        guest = _GuestThatRecordsRemovals([], finish=True)
        asyncio.run(
            host_tools_over_exec._reclaim_the_transports_own(
                guest, collapsed, until=time.monotonic() + 5.0
            )
        )
        assert guest.removals == [], f"the model's directory was removed: {guest.removals}"


class TestAPidAndALayoutThatCannotBeUsed:
    def test_a_pid_too_large_to_read_hedges_rather_than_going_quiet(self):
        """`absent` means no pid file; an oversized one is a file that exists and cannot be used.

        The read returns a sentinel rather than a string for anything over the cap. Collapsing
        that into `absent` would let a guest padding the file past 32 bytes opt out of both the
        signal and the mention of it, so it reports as a pid that cannot be used.
        """

        class _OversizedPid(_GuestThatRecordsTheKill):
            async def exec(self, command: str | Any, *, working_directory: str, timeout: float):
                self.commands.append(str(command))
                if "kill" in str(command):
                    return ExecResult(stdout="", exit_code=0)
                self.files[_LAYOUT.pid] = b"4" * 4096
                raise TimeoutError

        guest = _OversizedPid([], finish=False)
        with pytest.raises(SandboxProgramTimeout) as expired:
            _run(guest, HostToolRun(_registry()), timeout=30.0)

        assert guest.kills == []
        assert "may still be running" in str(expired.value), str(expired.value)

    def test_a_layout_that_scatters_the_transports_files_is_refused(self):
        """The directory to delete is inferred from `shim`, so the rest must agree with it.

        A layout that puts the exit marker elsewhere would have this remove a directory holding
        only some of what it owns, and leave the rest behind — wrong in both directions at once.
        """
        run = "/maf-sandbox/work/run-1"
        served = f"{run}/host_tools"
        scattered = GuestRunLayout(
            directory=run,
            work=f"{run}/work",
            program=f"{served}/program.py",
            shim=f"{served}/{SHIM_MODULE}",
            launcher=f"{served}/run_program.sh",
            calls=f"{served}/{CALLS_DIRECTORY}",
            output=f"{served}/program_output.txt",
            # Somewhere else entirely: the transport owns it and would not remove it.
            exit_code=f"{run}/elsewhere/program_exit_code",
            pid=f"{served}/program_pid",
        )
        guest = _GuestThatRecordsRemovals([], finish=True)
        asyncio.run(
            host_tools_over_exec._reclaim_the_transports_own(
                guest, scattered, until=time.monotonic() + 5.0
            )
        )
        assert guest.removals == [], f"a scattered layout was still removed: {guest.removals}"

    def test_a_layout_the_factory_built_is_not_refused(self):
        """The guard above must not reject the shape every kind actually uses."""
        guest = _GuestThatRecordsRemovals([], finish=True)
        asyncio.run(
            host_tools_over_exec._reclaim_the_transports_own(
                guest, _LAYOUT, until=time.monotonic() + 5.0
            )
        )
        assert len(guest.removals) == 1, guest.commands

    def test_a_run_whose_files_sit_outside_it_is_not_reported_reclaimed(self):
        """`reclaim_run` answers for the run, so the run has to be inside what it removes.

        A layout placing `work` elsewhere would have the removal succeed and the answer come
        back `True`, while the model's files stayed readable — and `True` is what tells a
        caller there is nothing to escalate.
        """
        elsewhere = GuestRunLayout(
            directory="/maf-sandbox/work/run-1",
            work="/maf-sandbox/work/somewhere-else",
            program="/maf-sandbox/work/run-1/host_tools/program.py",
            shim=f"/maf-sandbox/work/run-1/host_tools/{SHIM_MODULE}",
            launcher="/maf-sandbox/work/run-1/host_tools/run_program.sh",
            calls=f"/maf-sandbox/work/run-1/host_tools/{CALLS_DIRECTORY}",
            output="/maf-sandbox/work/run-1/host_tools/program_output.txt",
            exit_code="/maf-sandbox/work/run-1/host_tools/program_exit_code",
            pid="/maf-sandbox/work/run-1/host_tools/program_pid",
        )
        guest = _GuestThatRecordsRemovals([], finish=True)
        assert asyncio.run(reclaim_run(guest, elsewhere, timeout=5.0)) is False
        assert guest.removals == [], f"a run with files outside it was removed: {guest.removals}"

    def test_an_empty_pid_file_hedges_rather_than_going_quiet(self):
        """A zero-length entry is a pid that was recorded and cannot be used.

        The read answers `None` for a missing entry, an empty one and a directory alike, so
        reading its answer alone would let a guest truncate the file and opt out of the signal
        and — on the launcher-`exec` leg — of being mentioned at all.
        """

        class _EmptyPidFile(_GuestThatRecordsTheKill):
            async def exec(self, command: str | Any, *, working_directory: str, timeout: float):
                self.commands.append(str(command))
                if "kill" in str(command):
                    return ExecResult(stdout="", exit_code=0)
                self.files[_LAYOUT.pid] = b""
                raise TimeoutError

        guest = _EmptyPidFile([], finish=False)
        with pytest.raises(SandboxProgramTimeout) as expired:
            _run(guest, HostToolRun(_registry()), timeout=30.0)

        assert guest.kills == []
        assert "may still be running" in str(expired.value), str(expired.value)

    @pytest.mark.parametrize("bad", [0.0, -1.0, float("inf"), float("nan")])
    def test_reclaim_run_refuses_a_timeout_that_would_not_bound_it(self, bad: float):
        """An infinite bound reaches the backend's own `exec`, where it means never returning.

        The other public entry points in this module validate the same way, and this one is a
        `finally`-path call — a caller that hangs here loses the run's own result too.
        """
        guest = _GuestThatRecordsRemovals([], finish=True)
        with pytest.raises(ValueError, match="finite positive number"):
            asyncio.run(reclaim_run(guest, _LAYOUT, timeout=bad))
        assert guest.removals == []

    @pytest.mark.parametrize("bad", [0.0, -1.0, float("inf"), float("nan")])
    def test_reclaim_run_checks_its_timeout_before_the_layout(self, bad: float):
        """Validation of an argument may not depend on whether a different one is any good.

        A layout with files outside the run is *answered* `False`, and that is a retention
        failure a caller escalates. Judged first, it would have a bad `timeout` come back as
        one of those rather than as the programming error it is — and the escalation a caller
        then performs is a sandbox disposal earned by nothing.
        """
        elsewhere = GuestRunLayout(
            directory="/maf-sandbox/work/run-1",
            work="/maf-sandbox/work/somewhere-else",
            program="/maf-sandbox/work/run-1/host_tools/program.py",
            shim=f"/maf-sandbox/work/run-1/host_tools/{SHIM_MODULE}",
            launcher="/maf-sandbox/work/run-1/host_tools/run_program.sh",
            calls=f"/maf-sandbox/work/run-1/host_tools/{CALLS_DIRECTORY}",
            output="/maf-sandbox/work/run-1/host_tools/program_output.txt",
            exit_code="/maf-sandbox/work/run-1/host_tools/program_exit_code",
            pid="/maf-sandbox/work/run-1/host_tools/program_pid",
        )
        guest = _GuestThatRecordsRemovals([], finish=True)
        with pytest.raises(ValueError, match="finite positive number"):
            asyncio.run(reclaim_run(guest, elsewhere, timeout=bad))
        assert guest.removals == []

    def test_a_pid_the_host_could_not_look_for_claims_nothing_about_a_program(self):
        """A failed stat is evidence of neither a started program nor an absent one.

        The launcher-`exec` leg reports each state differently, so collapsing "could not look"
        into "a pid was there and could not be signalled" would have a backend hiccup assert
        that a program is running.
        """

        class _CannotStatThePid(_GuestThatRecordsTheKill):
            async def exec(self, command: str | Any, *, working_directory: str, timeout: float):
                self.commands.append(str(command))
                if "kill" in str(command):
                    return ExecResult(stdout="", exit_code=0)
                raise TimeoutError

            async def stat_file(self, path: str, *, working_directory: str):
                if path == _LAYOUT.pid:
                    raise PermissionError("the daemon said no")
                return await super().stat_file(path, working_directory=working_directory)

        guest = _CannotStatThePid([], finish=False)
        with pytest.raises(SandboxProgramTimeout) as expired:
            _run(guest, HostToolRun(_registry()), timeout=30.0)

        message = str(expired.value)
        assert "could not be established" in message, message
        assert "(it had started the program" not in message, message


class TestWhatACallerCanBranchOn:
    """`signal` carries the outcome, so acting on it needs no match against the message."""

    @pytest.mark.parametrize(
        ("pid", "kill_exit_code", "expected"),
        [
            pytest.param("4242", 0, "sent", id="sent"),
            pytest.param("4242", 1, "refused", id="a-signal-that-did-not-land"),
            pytest.param(None, 0, "unrecorded", id="no-pid-after-the-launcher-returned"),
        ],
    )
    def test_the_outcome_reaches_the_caller(self, pid, kill_exit_code, expected):
        guest = _GuestThatRecordsTheKill([], finish=False, pid=pid, kill_exit_code=kill_exit_code)
        with pytest.raises(SandboxProgramTimeout) as expired:
            _run(guest, HostToolRun(_registry()), timeout=0.2)
        assert expired.value.signal == expected

    def test_a_launcher_that_ran_out_may_still_have_started_something(self):
        """A missing pid on this leg is not evidence that nothing is running.

        The launcher backgrounds the interpreter and publishes the pid on the line after, so an
        `exec` that expires between the two leaves a program running and nothing to point at.
        Reported as `absent` — the one outcome documented as needing nothing further — a caller
        would be told to walk away from a sandbox that is still executing.
        """

        class _BoundsTheStart(_ScriptedGuest):
            async def exec(self, command: str | Any, *, working_directory: str, timeout: float):
                raise TimeoutError

        with pytest.raises(SandboxProgramTimeout) as expired:
            _run(_BoundsTheStart([], finish=False), HostToolRun(_registry()), timeout=30.0)
        assert expired.value.signal == "unknown"

    def test_only_the_leg_that_never_reached_a_launcher_says_nothing_started(self):
        """`absent` has to stay reachable, or the one safe-to-ignore outcome describes nothing.

        Here the run's budget goes on the upload, so no launcher ever ran and nothing can have
        started. This is the only leg entitled to say so.
        """

        class _StallsOnTheUpload(_ScriptedGuest):
            async def write_file(
                self, path: str, content: str | bytes, *, working_directory: str
            ) -> None:
                await asyncio.sleep(3600)

        with pytest.raises(SandboxProgramTimeout) as gone:
            _run(_StallsOnTheUpload([], finish=False), HostToolRun(_registry()), timeout=0.1)
        assert gone.value.signal == "absent"

    def test_an_exception_raised_by_anything_else_claims_nothing(self):
        """The default answers for callers who never passed a fate, so it must assert none.

        This type is public and a `TimeoutError` subclass, so a kind or a host raising it for a
        transport of its own passes a message and stops there. A default of `absent` would have
        every one of those tell a handler that no program was started and no disposal is owed —
        the one conclusion in this vocabulary that ends the matter, made by omission.
        """
        assert SandboxProgramTimeout("a run of someone else's").signal == "unknown"

    def test_the_message_and_the_attribute_cannot_drift(self):
        """The prose is generated from the same value, so one cannot contradict the other."""
        guest = _GuestThatRecordsTheKill([], finish=False, kill_exit_code=1)
        with pytest.raises(SandboxProgramTimeout) as expired:
            _run(guest, HostToolRun(_registry()), timeout=0.2)
        assert expired.value.signal == "refused"
        assert host_tools_over_exec._NOT_SIGNALLED in str(expired.value)


_TL = TransferLimits
_fold = host_tools_over_exec.fold_host_tool_call_transfer_limits
_LAUNCHER = host_tools_over_exec._LAUNCHER_CEILING
_MARKER = host_tools_over_exec._MARKER_CEILING
_MARKERS = host_tools_over_exec._MARKER_FILES
_REFUSAL = host_tools_over_exec._REFUSAL_CEILING


def _surface(response_limits: TransferLimits, dispatches: int = 1) -> Any:
    """The sealed surface a registry answers with, which is what the fold reads."""
    registry = HostToolRegistry(
        response_limits=response_limits, max_host_tool_calls_per_run=dispatches
    )
    registry.register(add)
    return registry.aggregate()


class TestTheCeilingsTheFoldPromisesAreEnforced:
    """Each constant the fold declares to a backend is a bound the transport cannot exceed."""

    def test_a_launcher_the_run_paths_blow_past_the_ceiling_is_refused(self):
        """The template repeats the layout's paths many times, so a long enough `work_dir`
        outgrows the ceiling the fold declared for the upload."""
        layout = host_tools_over_exec.guest_run_layout("/w/" + "d" * 5000)
        with pytest.raises(ValueError, match="over the .* ceiling"):
            host_tools_over_exec.launcher_script(layout, "python3")

    def test_an_ordinary_launcher_still_builds(self):
        layout = host_tools_over_exec.guest_run_layout("/maf-sandbox/work/run-1")
        script = host_tools_over_exec.launcher_script(layout, "python3")
        assert len(script.encode("utf-8")) <= host_tools_over_exec._LAUNCHER_CEILING

    def test_a_refusal_quoting_non_bmp_text_still_fits_the_ceiling(self):
        """`_bounded` counts characters and the ceiling counts bytes: `json.dumps` escapes one
        non-BMP character to twelve bytes, so a sentence inside every character bound this
        package applies can still serialize past the byte one the fold declared."""
        sentence = f"Error: {'😀' * 120!r} is not a registered host tool"
        envelope = host_tools_over_exec._refusal(sentence)
        assert len(envelope.encode("utf-8")) <= host_tools_over_exec._REFUSAL_CEILING

    def test_a_refusal_that_does_not_fit_is_replaced_whole_not_truncated(self):
        envelope = host_tools_over_exec._refusal("😀" * 4000)
        assert json.loads(envelope)["refusal"] == host_tools_over_exec._REFUSAL_TOO_LONG

    def test_an_ordinary_refusal_is_passed_through_unchanged(self):
        envelope = host_tools_over_exec._refusal("Error: 'nope' is not a registered host tool")
        assert json.loads(envelope)["refusal"] == "Error: 'nope' is not a registered host tool"


class TestFoldDispatchTransferLimits:
    """Fold the dispatch transport's own traffic into a workload's caps, so the router refuses a
    backend the transport would overrun.

    Every leg moves. The transport writes the launcher and one answer per request it serves, and
    reads the program's output, every request and the run markers — none of it declared by the
    workload, and none of it bounded by a single leg of the registry's own limits.
    """

    _WL = _TL(max_bytes_per_file=64 * 1024, max_total_bytes=256 * 1024, max_files=4)
    _RL = _TL(max_bytes_per_file=8_000_000, max_total_bytes=32_000_000, max_files=64)
    #: `_serving_bound` for the surfaces below: one dispatch plus the refusal past the cap.
    _SERVES = 2

    def _folded(self, dispatches: int = 1):
        return _fold(self._WL, self._WL, _surface(self._RL, dispatches))

    def test_files_out_per_file_reaches_the_response_total_leg(self):
        # The output is read as ONE file up to response_limits.max_total_bytes, so a small
        # files_out is lifted to cover it — the correction a naive per-leg max misses.
        assert self._folded().files_out.max_bytes_per_file == 32_000_000

    def test_files_in_per_file_covers_one_response(self):
        assert self._folded().files_in.max_bytes_per_file == 8_000_000

    def test_files_in_total_holds_the_launcher_every_response_and_the_refusals(self):
        """Refusals are the part no ledger carries: `response_limits` bounds what a tool
        *delivered*, and a refusal delivered nothing, yet the transport writes one per request."""
        inn = self._folded().files_in
        assert inn.max_total_bytes == (
            self._WL.max_total_bytes
            + _LAUNCHER
            + self._RL.max_total_bytes
            + self._SERVES * _REFUSAL
        )

    def test_files_in_count_holds_the_launcher_and_every_answer(self):
        """`max_files` is a transfer ceiling like the byte legs: a backend allowing exactly the
        workload's count would pass attach and then be handed the transport's own files."""
        assert self._folded().files_in.max_files == self._WL.max_files + 1 + self._SERVES

    def test_files_out_total_holds_the_output_every_request_and_the_markers(self):
        """Request bytes are deliberately not charged to the response ledger (`_request_cap`), so
        cumulative read traffic is bounded by the per-file cap times the serving bound — a number
        only this fold puts in front of the backend."""
        out = self._folded().files_out
        assert out.max_total_bytes == (
            self._WL.max_total_bytes
            + self._RL.max_total_bytes
            + self._SERVES * self._RL.max_bytes_per_file
            + _MARKERS * _MARKER
        )

    def test_files_out_count_holds_the_output_every_request_and_the_markers(self):
        assert self._folded().files_out.max_files == (
            self._WL.max_files + 1 + self._SERVES + _MARKERS
        )

    def test_the_counts_follow_the_registrys_dispatch_bound(self):
        """The bound is carried in the surface for exactly this: a registry serving more calls
        moves more files, and a fold blind to the count would under-declare every one of them."""
        few, many = self._folded(dispatches=1), self._folded(dispatches=50)
        assert many.files_in.max_files - few.files_in.max_files == 49
        assert many.files_out.max_files - few.files_out.max_files == 49

    def test_a_workload_that_already_asks_for_more_keeps_its_own_ceiling(self):
        # max(), not overwrite — on the per-file legs, which are a largest-single-transfer.
        big = _TL(max_bytes_per_file=99_000_000, max_total_bytes=99_000_000, max_files=9)
        folded = _fold(big, big, _surface(self._RL))
        assert folded.files_in.max_bytes_per_file == 99_000_000
        assert folded.files_out.max_bytes_per_file == 99_000_000  # 99M > the 32M output

    def test_the_fold_makes_a_backend_the_bare_spec_would_pass_refused(self):
        # A spec small enough to pass attach today no longer fits a backend permitting 1 MiB per
        # file, because the output read needs the response total.
        backend = _TL(
            max_bytes_per_file=1024 * 1024, max_total_bytes=64 * 1024 * 1024, max_files=64
        )
        assert self._WL.within(backend)  # unfolded: admitted
        assert not self._folded().files_out.within(backend)  # folded: refused

    def test_files_in_per_file_covers_the_launcher_when_nothing_else_reaches_it(self):
        """The launcher is one file too, not only bytes in the total. A workload and a registry
        both capped below it would otherwise fold to a per-file requirement smaller than the
        upload the transport always makes, and a backend admitted at attach refuses the very
        first write."""
        small = _TL(max_bytes_per_file=1024, max_total_bytes=4096, max_files=4)
        assert _fold(small, small, _surface(small)).files_in.max_bytes_per_file == _LAUNCHER

    def test_files_out_per_file_covers_a_request_read_that_outgrows_the_output(self):
        """Nothing orders a registry's legs: `max_bytes_per_file` may exceed `max_total_bytes`,
        and then the largest read out is a *request*, not the program's output. Folding only the
        output leg would leave that read over a per-file ceiling the router just approved."""
        lopsided = _TL(max_bytes_per_file=9_000_000, max_total_bytes=1_000_000, max_files=8)
        folded = _fold(self._WL, self._WL, _surface(lopsided))
        assert folded.files_out.max_bytes_per_file == 9_000_000

    def test_files_out_per_file_never_falls_below_a_marker_read(self):
        """Every leg is validated only as positive, so a registry may sit below the handful of
        bytes the pid, session and exit markers are read with — reads the transport makes on
        every run, whatever the registry says."""
        tiny = _TL(max_bytes_per_file=1, max_total_bytes=2, max_files=1)
        assert _fold(tiny, tiny, _surface(tiny)).files_out.max_bytes_per_file == _MARKER

    def test_files_in_per_file_never_falls_below_a_refusal(self):
        """A refusal is a fixed sentence plus bounded quoted text, so it can outgrow a registry
        whose own per-response cap is a byte."""
        tiny = _TL(max_bytes_per_file=1, max_total_bytes=2, max_files=1)
        small = _TL(max_bytes_per_file=8, max_total_bytes=16, max_files=1)
        folded = _fold(small, small, _surface(tiny))
        assert folded.files_in.max_bytes_per_file == _LAUNCHER  # launcher still the largest
        assert folded.files_in.max_total_bytes >= 2 * _REFUSAL  # and refusals are budgeted
