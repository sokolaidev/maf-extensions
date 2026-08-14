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

import asyncio
import importlib.util
import json
import posixpath
import shlex
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from maf_sandbox import (
    CALLS_DIRECTORY,
    SHIM_MODULE,
    EntryKind,
    ExecResult,
    HostToolRegistry,
    HostToolRun,
    Identity,
    SandboxEntry,
    SourceIntegrity,
    TransferLimits,
    dispatch_over_exec,
    guest_run_layout,
    host_tool_shim,
    launcher_script,
    sandbox_tool,
)

_RUN = "/maf-sandbox/work/run-1"
_LAYOUT = guest_run_layout(_RUN)


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
    ) -> None:
        self.files: dict[str, bytes] = {}
        self.calls = calls
        self.answers: list[Any] = []
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

    async def write_file(self, path: str, content: str | bytes) -> None:
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
        del working_directory
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
        del working_directory
        content = self.files[path]
        if len(content) > max_bytes:
            raise AssertionError("the supervisor read past its own cap")
        return content

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


def _registry(**kwargs: Any) -> HostToolRegistry:
    registry = HostToolRegistry(**kwargs)
    registry.register(add)
    return registry


def _run(guest: _ScriptedGuest, run: HostToolRun, *, timeout: float = 5.0) -> ExecResult:
    return asyncio.run(dispatch_over_exec(guest, run, _LAYOUT, timeout=timeout, poll_interval=0.0))


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
        result = _run(guest, HostToolRun(_registry(max_dispatches_per_run=1)))
        assert guest.answers[0]["value"] == 2
        assert "dispatch cap (1) is exhausted" in guest.answers[1]["refusal"]
        assert result.stdout == "finished after a refusal"
        assert result.exit_code == 0

    def test_a_request_over_the_ceiling_is_refused_unread(self):
        limits = TransferLimits(max_bytes_per_file=256, max_total_bytes=4096, max_files=4)
        guest = _ScriptedGuest([("add", {"left": 1, "right": 1})], request_bytes=1024)
        _run(guest, HostToolRun(_registry(response_limits=limits)))
        assert "larger than the host will read" in guest.answers[0]["refusal"]

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


class TestWhatReviewFound:
    """Five defects from #327's review, each pinned where it would come back."""

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

    def test_the_launcher_passes_one_argument_to_sh_however_the_path_reads(self):
        """Quoting fragments inside an already quoted `sh -c '…'` ends the outer argument."""
        layout = guest_run_layout("/maf-sandbox/work/run 1")
        command = launcher_script(layout).splitlines()[-1]
        tokens = shlex.split(command.removesuffix(" &"))
        assert tokens[:3] == ["nohup", "sh", "-c"]
        assert layout.program in tokens[3], "the inner command did not survive as one argument"
        assert layout.output in tokens[3]

    def test_the_shim_publishes_a_request_only_once_it_is_whole(self, tmp_path: Path):
        """`open` creates the file empty; a poll in that window would refuse a valid call."""
        source = host_tool_shim()
        assert "os.replace(" in source, "the request is published without an atomic rename"
        module_path = tmp_path / SHIM_MODULE
        module_path.write_text(source, encoding="utf-8")
        spec = importlib.util.spec_from_file_location("maf_host_tools_atomic", module_path)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        request = tmp_path / CALLS_DIRECTORY / "0001.request.json"
        seen: list[str] = []

        def poll_like_the_supervisor() -> None:
            """Read the first thing that appears, then answer — whatever it turned out to be.

            Answering unconditionally is what keeps this test *fast* when it fails: a watcher
            that gave up on an unparseable request would leave `call` blocking for the shim's
            own five-minute patience, and a hang reads as a hung suite rather than as a defect.
            """
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if request.exists():
                    seen.append(request.read_text(encoding="utf-8"))
                    (tmp_path / CALLS_DIRECTORY / "0001.response.json").write_text(
                        json.dumps({"value": None}), encoding="utf-8"
                    )
                    return
                time.sleep(0.001)

        watcher = threading.Thread(target=poll_like_the_supervisor)
        watcher.start()
        module.call("anything", padding="x" * 200_000)
        watcher.join(timeout=5)
        assert seen, "nothing was ever published"
        try:
            published = json.loads(seen[0])
        except json.JSONDecodeError:
            pytest.fail(
                f"the supervisor saw a request that was not yet whole: {seen[0][:40]!r} — a "
                "valid call would have been answered with a refusal it could never retry"
            )
        assert published["name"] == "anything"

    def test_starting_the_program_spends_the_same_deadline_the_run_does(self):
        """A slow launcher must not hand supervision a second full timeout."""

        class _SlowToStart(_ScriptedGuest):
            async def exec(self, command, *, working_directory: str, timeout: float):
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
                dispatch_over_exec(
                    guest, HostToolRun(_registry()), _LAYOUT, timeout=0.05, poll_interval=5.0
                )
            )
        assert time.monotonic() - began < 1.0, "one poll interval outlasted the whole timeout"
