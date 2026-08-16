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
    dispatch_over_exec,
    guest_run_layout,
    host_tool_shim,
    launcher_script,
    sandbox_tool,
)
from maf_sandbox import _host_tools_over_exec as host_tools_over_exec
from maf_sandbox.paths import confine_guest_path

#: Fast enough for a suite, and still an interval — the API refuses zero, because
#: `sleep(0)` is not a throttle.
_FAST = 0.001

_RUN = "/maf-sandbox/work/run-1"
_LAYOUT = guest_run_layout(_RUN)

#: Scripted in place of a name: the caller took this number and could not publish under it.
_ABANDONED = "<abandoned>"


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

    def _resolved(self, path: str, working_directory: str) -> str:
        """What a backend would make of this path — `confine_guest_path`, as they all use.

        A double keying on the string it was handed cannot tell a path a real backend
        accepts from one it refuses, which is most of what these tests are for.
        """
        return confine_guest_path(path, working_directory)

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


def _registry(**kwargs: Any) -> HostToolRegistry:
    registry = HostToolRegistry(**kwargs)
    registry.register(add)
    return registry


def _run(
    guest: _ScriptedGuest, run: HostToolRun, *, timeout: float = 5.0, poll: float = _FAST
) -> ExecResult:
    return asyncio.run(dispatch_over_exec(guest, run, _LAYOUT, timeout=timeout, poll_interval=poll))


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

    def test_the_launcher_upload_is_spent_from_the_same_budget(self):
        """`exec` gets what is left after writing the launcher, not another full timeout."""
        seen: list[float] = []

        class _SlowUpload(_ScriptedGuest):
            async def write_file(self, path: str, content):
                await asyncio.sleep(0.1)
                await super().write_file(path, content)

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
                dispatch_over_exec(
                    guest, HostToolRun(registry), _LAYOUT, timeout=1.0, poll_interval=_FAST
                )
            )
        assert dispatched == [], "a request read inside the bound was dispatched after it"


class TestTheLauncher:
    """What the guest is actually asked to run, and whether a shell can read it."""

    def test_it_passes_one_argument_to_sh_however_the_path_reads(self):
        """Quoting fragments inside an already quoted `sh -c '…'` ends the outer argument."""
        layout = guest_run_layout("/maf-sandbox/work/run 1")
        command = launcher_script(layout).splitlines()[-1]
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
        assert 'case "$maf_entry" in /*)' in script, "a relative inherited entry is not filtered"
        # Unquoted for the word splitting `IFS=:` gives it, which also invites globbing against
        # the guest's own directory; and the loop's names are the launcher's, not the image's.
        assert "\nset -f\n" in script, "the inherited value is globbed before it is filtered"
        assert "\nunset maf_kept maf_entry\n" in script, "launcher variables reach the guest"
        assert "\nkept=" not in script and "for entry in" not in script, (
            "an unprefixed name collides with one an image may already export"
        )

    def test_the_interpreter_is_a_shell_word_like_every_path(self):
        """An interpreter path with a space is split unless it is quoted like the rest.

        The environment assignment in front of it is why this reads the second word: `sh`
        takes `NAME=value cmd` as one command with one variable set for it.
        """
        layout = guest_run_layout("/maf-sandbox/work/run-1")
        command = launcher_script(layout, "/opt/py 3.12/bin/python3").splitlines()[-1]
        inner = shlex.split(command.removesuffix(" &"))[3]

        assert shlex.split(inner)[:2] == ["PYTHONUNBUFFERED=1", "/opt/py 3.12/bin/python3"]


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
    @pytest.mark.skipif(
        os.pathsep != ":",
        reason="the launcher splits PYTHONPATH on ':' and tests entries for a leading '/', "
        "which is the guest it targets; a Windows interpreter parses neither that way",
    )
    def test_an_inherited_relative_path_entry_cannot_reach_into_the_run(self, tmp_path: Path):
        """The launcher's own filtering, against a real `sh` and a real interpreter.

        An image with `.` on `PYTHONPATH` makes the guest's working directory importable at
        interpreter *startup*, where `site` imports `sitecustomize` before the program runs —
        a hook that can seed `sys.modules` outright, which no amount of ordering prevents.
        Absolute entries survive: one cannot name a run directory that did not exist when the
        image was built.
        """
        directory = tmp_path.as_posix()
        served, work = f"{directory}/host_tools", f"{directory}/work"
        pathlib.Path(served).mkdir(parents=True, exist_ok=True)
        pathlib.Path(work).mkdir(parents=True, exist_ok=True)
        pathlib.Path(served, SHIM_MODULE).write_text("SPEAKING = 'the shim'\n", encoding="utf-8")
        pathlib.Path(work, "sitecustomize.py").write_text(
            "import sys, types\n"
            "m = types.ModuleType('maf_host_tools')\n"
            "m.SPEAKING = 'the guest'\n"
            "sys.modules['maf_host_tools'] = m\n",
            encoding="utf-8",
        )
        pathlib.Path(served, "program.py").write_text(
            "import maf_host_tools\nprint(maf_host_tools.SPEAKING)\n", encoding="utf-8"
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
            # What an image does, not what this run does: a relative entry, and an absolute
            # one that must survive alongside the shim's.
            env={**os.environ, "PYTHONPATH": f".:{served}", "PYTHONSAFEPATH": "1"},
        )
        marker = pathlib.Path(layout.exit_code)
        for _ in range(200):
            if marker.exists():
                break
            time.sleep(0.05)

        printed = pathlib.Path(layout.output).read_text(encoding="utf-8").strip()
        assert printed == "the shim", f"a startup hook in the work directory won: {printed!r}"

    @pytest.mark.skipif(shutil.which("sh") is None, reason="needs a POSIX shell")
    def test_a_work_directory_that_cannot_be_entered_stops_the_run(self, tmp_path: Path):
        """`sh` does not stop on a failed command, so the `cd` has to say so itself.

        Unguarded, a failed `mkdir`/`cd` pair leaves the program running wherever the launcher
        was exec'd: artifacts land where no kind collects them, the exit marker still appears,
        and the run reports success. A non-zero launcher is already handled — `dispatch_over_exec`
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
        """The check above is only worth having if what it admits actually loads."""
        for index, patience in enumerate((0.001, 1.0, 86_400.0)):
            module_path = tmp_path / f"patience_{index}.py"
            module_path.write_text(host_tool_shim(call_timeout=patience), encoding="utf-8")
            spec = importlib.util.spec_from_file_location(f"patience_{index}", module_path)
            assert spec and spec.loader
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            assert module._TIMEOUT == patience


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


class TestWhatTheDeadlineCovers:
    """The bound is on the run, and sandbox I/O is part of the run."""

    def test_a_stalled_backend_call_does_not_hold_the_supervisor(self):
        """A hung `stat_file` is what this bound is for: no guest, no exit marker, no end."""

        class _Stalled(_ScriptedGuest):
            async def stat_file(self, path: str, *, working_directory: str):
                await asyncio.sleep(3600)
                raise AssertionError("unreachable")

        began = time.monotonic()
        with pytest.raises(TimeoutError):
            _run(_Stalled([]), HostToolRun(_registry()), timeout=0.2)
        assert time.monotonic() - began < 4.0, "a stalled transport call outlasted the bound"

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
        inner = shlex.split(launcher_script(layout).splitlines()[-1].removesuffix(" &"))[3]
        assert f"{layout.exit_code}.part" in inner, "the exit code is written straight to its name"
        assert inner.rstrip().endswith(f"mv '{layout.exit_code}.part' '{layout.exit_code}'")


class TestThePathsTheSupervisorPasses:
    def test_every_pull_call_names_a_path_a_backend_accepts(self):
        """The double resolves through `confine_guest_path`, so a refused path fails here too.

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
            assert confine_guest_path(path, _LAYOUT.directory).startswith(_LAYOUT.directory)


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


class TestTheLayoutsOwnPromise:
    @pytest.mark.parametrize("directory", ["work/run-1", "", "run-1"])
    def test_a_run_directory_that_is_not_absolute_is_refused(self, directory: str):
        """`confine_guest_path` joins a relative one against itself, and nothing looks wrong.

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

    @pytest.mark.parametrize("program", ["program_output.txt", "run_program.sh", SHIM_MODULE])
    def test_a_program_named_after_the_layouts_own_files_is_refused(self, program: str):
        """Each collision breaks the run in its own way, and none of them announce themselves.

        `program_output.txt` is the launcher's redirection target, so the shell truncates the
        program before the interpreter opens it; the launcher and the shim are written over
        whatever the kind put there. All three end as a program that will not run, with
        nothing pointing at the name that caused it.
        """
        with pytest.raises(ValueError, match="already uses"):
            guest_run_layout("/maf-sandbox/work/run-1", program=program)

    @pytest.mark.parametrize("directory", [r"/work/run\1", r"/work\run"])
    def test_a_run_directory_the_backends_cannot_resolve_is_refused(self, directory: str):
        """Absolute is not the same as valid: `confine_guest_path` refuses a backslash.

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
        "name", ["encodings.py", "site.py", "sitecustomize.py", "usercustomize.py"]
    )
    def test_a_program_the_interpreter_imports_on_its_way_up_is_refused(self, name: str):
        """The transport's directory is on the path from startup, not from when the script is
        found, so a program under one of these is reached during initialisation —
        `sitecustomize.py` running twice over, `encodings.py` ending the interpreter before it
        can say why."""
        with pytest.raises(ValueError, match="imports at startup"):
            guest_run_layout("/maf-sandbox/work/run-1", program=name)

    def test_a_directory_spelled_the_long_way_round_is_kept_the_short_way(self):
        """One directory, two spellings, is a difference waiting to matter.

        `confine_guest_path` normalises, so the pull calls address `/work/run-1` whatever the
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
            async def write_file(self, path: str, content: str | bytes) -> None:
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

        assert str(spent.value) == "the run's 30s were gone while starting the program"
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
                dispatch_over_exec(
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
        registry = _registry(max_dispatches_per_run=1)
        guest = _ScriptedGuest([("add", {"left": 1, "right": 1})] * 3)

        with pytest.raises(TimeoutError):
            _run(guest, HostToolRun(registry), timeout=0.5)

        assert len(guest.answers) == 2, f"the allowance did not hold: {guest.answers}"
        assert guest.answers[0].get("value") == 2
        assert "dispatch cap" in guest.answers[1]["refusal"]


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
