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
import pathlib
import posixpath
import shlex
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
from maf_sandbox.paths import confine_guest_path

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

        The double used to ignore `working_directory` and key on the string it was handed,
        which meant it could not tell a path a real backend accepts from one it refuses.
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
                    guest, HostToolRun(registry), _LAYOUT, timeout=1.0, poll_interval=0.0
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

    def test_the_interpreter_is_a_shell_word_like_every_path(self):
        """An interpreter path with a space is split unless it is quoted like the rest."""
        layout = guest_run_layout("/maf-sandbox/work/run-1")
        command = launcher_script(layout, "/opt/py 3.12/bin/python3").splitlines()[-1]
        inner = shlex.split(command.removesuffix(" &"))[3]
        assert shlex.split(inner)[0] == "/opt/py 3.12/bin/python3"


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
        """
        assert self._wrappers({"call", "json", "os", "open", "_claim", "_CALLS", "int"}) == set()


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
