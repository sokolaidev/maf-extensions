"""Offline tests for the CodeAct sandbox workload.

The whole kind runs here against the fakes in :mod:`maf_sandbox.testing` — attach, write,
exec, format — with no container, no interpreter and no host application.

Two things are CodeAct-specific and both are pinned below.  The **argv shape**: model-written
source reaches the interpreter as a file, never as part of a command line, so no test here may
pass if the code ever gets interpolated into a string.  The **result format**: it is what a
model reads when it has to fix its own program, so it is a contract rather than a rendering
detail.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Sequence
from dataclasses import replace
from typing import Any

import pytest
from maf_sandbox import (
    DEFAULT_CAPABILITIES,
    DEFAULT_TRANSFER_LIMITS,
    MAX_ARTIFACT_NAME_BYTES,
    Artifact,
    Capability,
    EntryKind,
    ExecResult,
    Isolation,
    LandedArtifact,
    NameNormalization,
    OutputSink,
    SandboxCapabilityNotSupported,
    SandboxEntry,
    SandboxOutputSinkRequired,
    SandboxRouter,
    TransferLimits,
    WorkspaceContext,
)
from maf_sandbox.testing import InMemoryStore, InProcessSandbox, InProcessSandboxBackend

from maf_sandbox_codeact import (
    CODEACT_KIND,
    EXECUTE_CODE_TOOL_NAME,
    CodeactOutputs,
    codeact_sandbox_spec,
    make_codeact_tools,
)
from maf_sandbox_codeact._tool import (
    _MANIFEST_FILENAME,
    _MANIFEST_MAX_BYTES,
    _PROGRAM_FILENAME,
    _WORK_DIR,
)

#: What a backend must declare before this kind may collect anything.
_PULLS = DEFAULT_CAPABILITIES | {Capability.FILES_OUT}

# ---------------------------------------------------------------------------
# Fakes: a sandbox that keeps the command it was handed, unjoined
# ---------------------------------------------------------------------------


class _ScriptedSandbox(InProcessSandbox):
    """Records the raw ``command`` argument, and answers with a whole :class:`ExecResult`.

    :class:`~maf_sandbox.testing.InProcessSandbox` joins an argv sequence with
    :func:`shlex.join` before recording it, and scripts stdout alone. This kind's tests need
    the command *unjoined* — that it is a sequence at all is the property under test, and a
    joined string cannot be told apart from a shell line — and need stderr and exit codes
    alongside stdout to exercise the result format.
    """

    def __init__(self, result: ExecResult | None = None, **kwargs) -> None:
        super().__init__(**kwargs)
        self.result = result
        self.raw_commands: list[str | Sequence[str]] = []

    async def exec(self, command, *, working_directory, timeout):
        self.raw_commands.append(command)
        answer = await super().exec(command, working_directory=working_directory, timeout=timeout)
        return self.result if self.result is not None else answer


class _WriteFailingSandbox(_ScriptedSandbox):
    async def write_file(self, path: str, content: str) -> None:
        raise RuntimeError("no space left at https://internal.invalid subscription 0000-1111")


class _ProducingSandbox(_ScriptedSandbox):
    """A sandbox whose ``exec`` writes files, standing in for a program that produced them.

    The run directory is a fresh uuid chosen inside the call, so an output cannot be seeded
    before it — which is the honest shape anyway: these files appear when the program runs.
    """

    def __init__(self, produces: dict[str, bytes] | None = None, **kwargs) -> None:
        super().__init__(**kwargs)
        self.produces = produces or {}

    async def exec(self, command, *, working_directory, timeout):
        result = await super().exec(command, working_directory=working_directory, timeout=timeout)
        for name, content in self.produces.items():
            self.contents[f"{working_directory}/{name}"] = content
        return result


class _StatOnlySandbox(_ScriptedSandbox):
    """Reports a manifest of a given size without holding one, and records every read.

    `InProcessSandbox` is honest, so it cannot report a size it does not have — which is the
    only way to show that an oversized or unmeasurable entry is refused *before* the read.
    """

    def __init__(self, *, size_bytes: int | None, **kwargs) -> None:
        super().__init__(**kwargs)
        self.reads: list[str] = []
        self._size = size_bytes

    async def stat_file(self, path, *, working_directory):
        if path.endswith(_MANIFEST_FILENAME):
            return SandboxEntry(path=path, kind=EntryKind.FILE, size_bytes=self._size)
        return await super().stat_file(path, working_directory=working_directory)

    async def read_file(self, path, *, working_directory, max_bytes):
        self.reads.append(path)
        return await super().read_file(
            path, working_directory=working_directory, max_bytes=max_bytes
        )


class _RecordingSink:
    """A host sink that records what it was handed and answers with a reference.

    ``handle`` carries a token on purpose: nothing this kind returns may render it.
    """

    def __init__(self, normalization: NameNormalization = NameNormalization.NFC) -> None:
        self.delivered: list[Artifact] = []
        self.sink = OutputSink(deliver=self.deliver, normalization=normalization)

    async def deliver(self, artifact: Artifact) -> LandedArtifact:
        self.delivered.append(artifact)
        return LandedArtifact(
            name=artifact.name,
            display=f"saved {artifact.name}",
            handle=f"blob://{artifact.name}?sig=secret",
        )

    @property
    def names(self) -> list[str]:
        return [artifact.name for artifact in self.delivered]

    @property
    def media_types(self) -> list[str | None]:
        return [artifact.media_type for artifact in self.delivered]


class _CountingStore(InMemoryStore):
    """Records every read, so a cap can be shown to answer before it spends anything."""

    def __init__(self, files: dict[str, str]) -> None:
        super().__init__(files)
        self.reads: list[str] = []

    async def read(self, name: str) -> str | None:
        self.reads.append(name)
        return await super().read(name)


class _ListedButGoneStore:
    """A store whose listing outlives its content, which `AgentFileStore.read` reports as `None`."""

    def __init__(self, name: str) -> None:
        self._name = name

    async def read(self, name: str) -> str | None:
        return None

    async def list(self) -> list[str]:
        return [self._name]


def _backend(
    sandbox: InProcessSandbox | None = None,
    *,
    acquire_error: BaseException | None = None,
    capabilities: frozenset[Capability] | None = None,
) -> InProcessSandboxBackend:
    return InProcessSandboxBackend(
        sandbox if sandbox is not None else _ScriptedSandbox(),
        acquire_error=acquire_error,
        **({} if capabilities is None else {"capabilities": capabilities}),
    )


async def _listing(store: Any) -> list[str]:
    """Enumerate whatever store the host wired; without one this kind shares nothing."""
    return [] if store is None else await store.list()


def _context(*, thread_id: str | None = "thread-1") -> WorkspaceContext:
    return WorkspaceContext(
        current_scope=lambda: "scope-a",
        current_thread_id=lambda: thread_id,
        list_files=_listing,
    )


def _tool(backend: InProcessSandboxBackend, *, thread_id: str | None = "thread-1", **kw):
    tools = make_codeact_tools(
        # Below the default floor: this suite exercises the workload, not the floor check.
        SandboxRouter([backend], min_isolation=Isolation.PROCESS),
        "data-analyst",
        _context(thread_id=thread_id),
        image="registry.invalid/python:3.13",
        **kw,
    )
    assert len(tools) == 1
    return tools[0]


def _landing(mode: CodeactOutputs, sink: _RecordingSink | None = None) -> dict[str, Any]:
    """The pair `make_codeact_tools` requires together: a mode, and somewhere to land."""
    return {"outputs": mode, "output_sink": (sink or _RecordingSink()).sink}


def _pulling_tool(
    sandbox: InProcessSandbox,
    mode: CodeactOutputs,
    sink: _RecordingSink,
    *,
    files_out: TransferLimits | None = None,
    **kw,
):
    return _tool(
        _backend(sandbox, capabilities=_PULLS),
        **_landing(mode, sink),
        **({} if files_out is None else {"files_out": files_out}),
        **kw,
    )


def _callable(tool):
    """The tool body, off whichever attribute the MAF decorator carries it on."""
    return getattr(tool, "func", None) or getattr(tool, "__wrapped__", None) or tool


def _run(tool, code: str, **kw) -> str:
    return asyncio.run(_callable(tool)(code=code, **kw))


def _run_producing(tool, sandbox: _ProducingSandbox, produced: dict[str, bytes], **kw) -> str:
    """Run one call whose program writes ``produced`` into its own working directory."""
    sandbox.produces = produced
    return _run(tool, "print('hi')", **kw)


# ---------------------------------------------------------------------------
# The spec — containment that must not be configurable away
# ---------------------------------------------------------------------------


class TestCodeactSandboxSpec:
    def test_kind_is_codeact(self):
        assert codeact_sandbox_spec().kind == CODEACT_KIND == "codeact"

    def test_work_dir_is_the_programs_own_root(self):
        assert codeact_sandbox_spec().work_dir == _WORK_DIR == "/work"

    def test_egress_is_closed(self):
        """The empty allowlist is half of what makes running model-written code defensible.

        A spec that names no host denies every host, so the program can compute but cannot
        fetch — and with no host tools dispatchable from inside either, nothing external can
        enter the sandbox and nothing leaves it but what the program printed.
        """
        assert codeact_sandbox_spec().egress_allow == ()

    def test_requires_exec_and_files_in(self):
        assert codeact_sandbox_spec().requires == frozenset({Capability.EXEC, Capability.FILES_IN})

    def test_it_does_not_raise_the_hosts_isolation_floor(self):
        """The host's floor governs: this kind runs only code the model itself wrote."""
        assert codeact_sandbox_spec().min_isolation is None

    def test_the_image_is_passed_through(self):
        assert codeact_sandbox_spec("registry.invalid/python:3.13").image == (
            "registry.invalid/python:3.13"
        )

    def test_the_image_id_is_passed_through(self):
        assert codeact_sandbox_spec(image_id="disk-image-7").image_id == "disk-image-7"


# ---------------------------------------------------------------------------
# The program reaches the interpreter as a file, never as a command line
# ---------------------------------------------------------------------------


def _run_dirs(sandbox: InProcessSandbox) -> list[str]:
    """The distinct run directories this sandbox was written into, in first-seen order."""
    seen: list[str] = []
    for path in sandbox.contents:
        parent = path.removeprefix(f"{_WORK_DIR}/").split("/", 1)[0]
        if parent not in seen:
            seen.append(parent)
    return [f"{_WORK_DIR}/{name}" for name in seen]


class TestTheProgramIsWrittenThenRun:
    def test_the_program_is_written_into_this_calls_own_directory(self):
        sandbox = _ScriptedSandbox()
        _run(_tool(_backend(sandbox)), "print('hi')")

        (run_dir,) = _run_dirs(sandbox)
        assert run_dir.startswith(f"{_WORK_DIR}/")
        assert sandbox.files == {f"{run_dir}/{_PROGRAM_FILENAME}": "print('hi')"}

    def test_the_interpreter_is_run_with_an_argv_sequence(self):
        """A sequence, not a string: a shell never sees any of this.

        The whole security posture of the kind rests here. The code the model wrote travels
        as file *content*, and the command is a fixed two-element argv, so there is no
        command line for it to be part of and nothing to quote or escape.
        """
        sandbox = _ScriptedSandbox()
        _run(_tool(_backend(sandbox)), "print('hi')")

        assert len(sandbox.raw_commands) == 1
        argv = sandbox.raw_commands[0]
        assert not isinstance(argv, str)
        (run_dir,) = _run_dirs(sandbox)
        assert list(argv) == ["python3", f"{run_dir}/{_PROGRAM_FILENAME}"]

    def test_the_command_never_carries_the_model_written_source(self):
        code = "import os; os.system('id'); print('$(whoami)`id`; rm -rf /')"
        sandbox = _ScriptedSandbox()
        _run(_tool(_backend(sandbox)), code)

        argv = sandbox.raw_commands[0]
        assert all(part == "python3" or part.endswith(_PROGRAM_FILENAME) for part in argv)
        assert list(sandbox.files.values()) == [code]

    def test_the_program_runs_in_its_own_directory(self):
        """So a program addresses everything it was given, and everything it produces, by a
        bare relative name."""
        sandbox = _ScriptedSandbox()
        _run(_tool(_backend(sandbox)), "print('hi')")

        _, working_directory, _ = sandbox.commands[0]
        assert working_directory == _run_dirs(sandbox)[0]

    def test_the_exec_timeout_is_passed_through(self):
        sandbox = _ScriptedSandbox()
        _run(_tool(_backend(sandbox), exec_timeout_seconds=7), "print('hi')")

        _, _, timeout = sandbox.commands[0]
        assert timeout == 7

    def test_each_call_gets_a_directory_of_its_own(self):
        """`acquire` is get-or-create, so the sandbox is reused across calls — and a file left
        behind by one round must not be readable as the next round's input, nor collectable as
        its output."""
        sandbox = _ScriptedSandbox()
        tool = _tool(_backend(sandbox))
        _run(tool, "print(1)")
        _run(tool, "print(2)")

        first, second = _run_dirs(sandbox)
        assert first != second
        assert sandbox.files == {
            f"{first}/{_PROGRAM_FILENAME}": "print(1)",
            f"{second}/{_PROGRAM_FILENAME}": "print(2)",
        }

    def test_the_key_carries_the_hosts_scope_and_thread_not_model_input(self):
        backend = _backend()
        _run(_tool(backend), "print('hi')")

        assert backend.keys[0].scope == "scope-a"
        assert backend.keys[0].thread_id == "thread-1"
        assert backend.keys[0].agent_dir == "data-analyst"


# ---------------------------------------------------------------------------
# The result format — what a model reads when it has to fix its own program
# ---------------------------------------------------------------------------


class TestResultFormat:
    def _out(self, result: ExecResult) -> str:
        return _run(_tool(_backend(_ScriptedSandbox(result))), "print('hi')")

    def test_stdout_alone(self):
        assert self._out(ExecResult(stdout="42\n")) == "stdout:\n42"

    def test_stderr_is_shown_when_the_program_wrote_any(self):
        out = self._out(ExecResult(stdout="42\n", stderr="warning: slow\n"))
        assert out == "stdout:\n42\n\nstderr:\nwarning: slow"

    def test_a_non_zero_exit_code_is_named(self):
        out = self._out(ExecResult(stdout="", stderr="Traceback ...\nNameError\n", exit_code=1))
        assert out == "stderr:\nTraceback ...\nNameError\n\nexit code: 1"

    def test_a_zero_exit_code_is_not_named(self):
        assert "exit code" not in self._out(ExecResult(stdout="42\n"))

    def test_all_three_sections_in_a_fixed_order(self):
        out = self._out(ExecResult(stdout="partial\n", stderr="boom\n", exit_code=2))
        assert out == "stdout:\npartial\n\nstderr:\nboom\n\nexit code: 2"

    def test_a_silent_program_is_told_to_print(self):
        """The commonest CodeAct mistake: writing an expression and expecting a REPL echo."""
        out = self._out(ExecResult(stdout="\n"))
        assert "printed nothing" in out
        assert "print(" in out


# ---------------------------------------------------------------------------
# Attach / do not attach
# ---------------------------------------------------------------------------


class TestMakeCodeactTools:
    """A host with no sandbox gets no tool, not a tool that fails when called."""

    def test_returns_empty_without_a_router(self):
        assert make_codeact_tools(None, "data-analyst", _context()) == []

    def test_returns_empty_when_the_router_has_no_backend(self):
        assert make_codeact_tools(SandboxRouter([]), "data-analyst", _context()) == []

    def test_the_tool_is_named_execute_code(self):
        tool = _tool(_backend())
        name = getattr(tool, "name", None) or getattr(
            getattr(tool, "__tool_definition__", None), "name", None
        )
        assert name == EXECUTE_CODE_TOOL_NAME == "execute_code"

    def test_a_backend_that_cannot_exec_is_refused_at_attach(self):
        """Attach time, not call time: the model is never shown a tool that cannot work.

        A backend offering only `FILES_IN` can take the program and never run it, and a
        workload allowed past this point fails inside the sandbox, where the reason is
        hardest to see.
        """
        backend = _backend(capabilities=frozenset({Capability.FILES_IN}))
        with pytest.raises(SandboxCapabilityNotSupported):
            _tool(backend)


class TestFidesDeclarations:
    """This tool declares no `source_integrity`, and that is a decision, not an omission.

    `sandbox_tool_declarations`'s default is `"trusted"` — right for a workload whose result
    is a compiler's own diagnostics. It is wrong here: what comes back is whatever a
    model-written `print(...)` chose to emit, so the tracker's untrusted default is the
    honest reading, and it is also the fail-safe direction.
    """

    def test_it_declares_nothing(self):
        tool = _tool(_backend())
        assert dict(tool.additional_properties or {}) == {}


# ---------------------------------------------------------------------------
# Degrading — a failure is an answer to the model, never an exception in the loop
# ---------------------------------------------------------------------------


class TestDegrades:
    def test_no_thread_context_is_refused(self):
        out = _run(_tool(_backend(), thread_id=None), "print('hi')")
        assert "no active thread context" in out

    def test_an_unavailable_sandbox_degrades_without_leaking_provider_detail(self):
        """Provider errors carry endpoint/subscription/tenant, and tool results persist."""
        secret = "https://management.eastus.azuredevcompute.io subscription 0000-1111"
        out = _run(_tool(_backend(acquire_error=RuntimeError(secret))), "print('hi')")

        assert "degrading to T0" in out
        assert "azuredevcompute" not in out
        assert "0000-1111" not in out

    def test_a_configuration_error_is_surfaced_because_we_authored_it(self):
        error = ValueError("No disk image ... was built from 'x'")
        out = _run(_tool(_backend(acquire_error=error)), "print('hi')")
        assert "No disk image" in out

    def test_a_failed_write_is_an_answer_not_an_exception(self):
        out = _run(_tool(_backend(_WriteFailingSandbox())), "print('hi')")

        assert out.startswith("Error:")
        assert "0000-1111" not in out

    def test_a_failed_exec_is_an_answer_not_an_exception(self):
        sandbox = _ScriptedSandbox(raises=RuntimeError("subscription 0000-1111"))
        out = _run(_tool(_backend(sandbox)), "print('hi')")

        assert out.startswith("Error:")
        assert "0000-1111" not in out

    def test_a_timeout_says_how_long_it_waited(self):
        sandbox = _ScriptedSandbox(raises=TimeoutError())
        out = _run(_tool(_backend(sandbox), exec_timeout_seconds=7), "print('hi')")

        assert "timed out" in out
        assert "7" in out


# ---------------------------------------------------------------------------
# The description is the whole surface: v0 registers nothing else
# ---------------------------------------------------------------------------


class TestToolDescription:
    def _description(self, **kw) -> str:
        return _callable(_tool(_backend(capabilities=_PULLS), **kw)).__doc__ or ""

    def test_it_says_only_printed_output_comes_back(self):
        assert "print" in self._description()

    def test_it_says_the_sandbox_has_no_network(self):
        assert "no network" in self._description().lower()

    def test_a_channel_the_host_did_not_wire_is_never_described(self):
        """The description is what the model plans against, so a parameter it cannot pass and
        a directory nothing collects must not appear in it."""
        plain = self._description()
        assert "``files``" not in plain
        assert "``outputs``" not in plain
        assert _MANIFEST_FILENAME not in plain

    def test_the_files_channel_is_described_when_it_exists(self):
        described = self._description(workspace_store=InMemoryStore({}))
        assert "``files``" in described

    def test_the_declared_outputs_channel_names_the_parameter(self):
        described = self._description(**_landing(CodeactOutputs.DECLARED))
        assert "``outputs``" in described
        assert _MANIFEST_FILENAME not in described

    def test_the_manifest_channel_names_the_file_and_shows_its_shape(self):
        described = self._description(**_landing(CodeactOutputs.MANIFEST))
        assert _MANIFEST_FILENAME in described
        assert '"outputs"' in described
        assert "``outputs``" not in described


# ---------------------------------------------------------------------------
# Files in — the caller's listing is the authority, and every run starts empty
# ---------------------------------------------------------------------------


class TestFilesIn:
    def _shared(self, sandbox: _ScriptedSandbox) -> dict[str, str]:
        """What was written into the run directory, keyed by the name the program sees."""
        run_dir = _run_dirs(sandbox)[0]
        return {
            path.removeprefix(f"{run_dir}/"): content
            for path, content in sandbox.files.items()
            if path != f"{run_dir}/{_PROGRAM_FILENAME}"
        }

    def test_a_listed_file_is_shared_under_its_own_name(self):
        sandbox = _ScriptedSandbox()
        store = InMemoryStore({"data/sales.csv": "a,b\n1,2\n"})
        tool = _tool(_backend(sandbox), workspace_store=store)

        _run(tool, "print('hi')", files=["data/sales.csv"])
        assert self._shared(sandbox) == {"data/sales.csv": "a,b\n1,2\n"}

    def test_a_name_outside_the_listing_is_refused_with_a_hint(self):
        """The listing is the injection-pinning boundary: a name the model invented, or read
        out of a file it was given, has nowhere to go."""
        sandbox = _ScriptedSandbox()
        store = InMemoryStore({"data/sales.csv": "x"})
        tool = _tool(_backend(sandbox), workspace_store=store)

        out = _run(tool, "print('hi')", files=["data/secrets.csv"])
        assert "not in this tool's file listing" in out
        assert "data/sales.csv" in out
        assert sandbox.files == {}

    @pytest.mark.parametrize("name", ["../../etc/passwd", "/etc/passwd", "a/../../b"])
    def test_a_traversing_name_is_refused_without_echoing_the_listing(self, name: str):
        """Echoing it would invite a retry with another spelling."""
        sandbox = _ScriptedSandbox()
        store = InMemoryStore({name: "x", "data/sales.csv": "y"})
        tool = _tool(_backend(sandbox), workspace_store=store)

        out = _run(tool, "print('hi')", files=[name])
        assert "cannot be shared" in out
        assert "data/sales.csv" not in out
        assert sandbox.files == {}

    @pytest.mark.parametrize(
        ("name", "reason"),
        [("a\\b.csv", "backslash"), ("a//b.csv", "segment"), ("a\tb.csv", "control character")],
    )
    def test_a_refusal_names_the_rule_that_was_broken(self, name: str, reason: str):
        """A fixed sentence about traversal and leading slashes tells a caller refused for a
        backslash that its name satisfies everything the tool asked for."""
        sandbox = _ScriptedSandbox()
        store = InMemoryStore({name: "x"})
        tool = _tool(_backend(sandbox), workspace_store=store)

        out = _run(tool, "print('hi')", files=[name])
        assert reason in out
        assert sandbox.files == {}

    def test_the_program_file_cannot_be_shadowed_by_a_shared_file(self):
        sandbox = _ScriptedSandbox()
        store = InMemoryStore({_PROGRAM_FILENAME: "print('theirs')"})
        tool = _tool(_backend(sandbox), workspace_store=store)

        out = _run(tool, "print('mine')", files=[_PROGRAM_FILENAME])
        assert "cannot be shared" in out
        assert sandbox.files == {}

    def test_a_file_deleted_between_rounds_does_not_survive_in_the_guest(self):
        """The reason each call gets its own directory: the sandbox is reused, so a stale
        input would otherwise be read by the next program as a live one."""
        sandbox = _ScriptedSandbox()
        store = InMemoryStore({"a.csv": "1", "b.csv": "2"})
        tool = _tool(_backend(sandbox), workspace_store=store)

        _run(tool, "print(1)", files=["a.csv", "b.csv"])
        del store.files["a.csv"]
        _run(tool, "print(2)", files=["b.csv"])

        second = _run_dirs(sandbox)[1]
        assert f"{second}/a.csv" not in sandbox.files
        assert f"{second}/b.csv" in sandbox.files

    def test_a_listed_file_with_no_content_is_reported_rather_than_written_as_none(self):
        """A store read can miss without raising — the file was listed, then removed. Writing
        `None` through would put the string "None" into the sandbox for the program to parse."""
        sandbox = _ScriptedSandbox()
        tool = _tool(_backend(sandbox), workspace_store=_ListedButGoneStore("gone.csv"))

        out = _run(tool, "print('hi')", files=["gone.csv"])
        assert "no content" in out
        assert sandbox.contents == {}

    def test_no_files_parameter_exists_without_a_store(self):
        assert "files" not in inspect.signature(_callable(_tool(_backend()))).parameters


class TestTheInboundCapsAreEnforcedHere:
    """No backend's `write_file` takes a limit, so a `files_in` bound applied here is applied
    nowhere else — and a spec declaring a bound nothing honours is worse than one declaring
    none. Every refusal lands before the sandbox is acquired, and before any write."""

    def _tool(self, sandbox, store, **kw):
        return _tool(_backend(sandbox), workspace_store=store, **kw)

    def test_more_files_than_the_count_allows_are_refused(self):
        sandbox = _ScriptedSandbox()
        store = InMemoryStore({"a": "1", "b": "2", "c": "3"})
        tool = self._tool(sandbox, store, files_in=replace(DEFAULT_TRANSFER_LIMITS, max_files=2))

        out = _run(tool, "print(1)", files=["a", "b", "c"])
        assert "at most 2" in out
        assert sandbox.contents == {}

    def test_a_file_over_the_per_file_ceiling_is_refused(self):
        sandbox = _ScriptedSandbox()
        store = InMemoryStore({"big.csv": "x" * 100})
        tool = self._tool(
            sandbox, store, files_in=replace(DEFAULT_TRANSFER_LIMITS, max_bytes_per_file=10)
        )

        out = _run(tool, "print(1)", files=["big.csv"])
        assert "at most 10 bytes per file" in out
        assert sandbox.contents == {}

    def test_a_set_over_the_total_is_refused_before_any_of_it_is_written(self):
        """Half an input set is worse than none: the program computes a confident wrong answer
        from whichever files happened to be written before the ceiling was reached."""
        sandbox = _ScriptedSandbox()
        store = InMemoryStore({"a.csv": "x" * 8, "b.csv": "y" * 8})
        tool = self._tool(
            sandbox, store, files_in=replace(DEFAULT_TRANSFER_LIMITS, max_total_bytes=10)
        )

        out = _run(tool, "print(1)", files=["a.csv", "b.csv"])
        assert "at most 10 per call" in out
        assert sandbox.contents == {}

    def test_the_count_is_of_encoded_bytes_not_characters(self):
        """A character ceiling would be a different, larger bound for every non-ASCII file."""
        sandbox = _ScriptedSandbox()
        store = InMemoryStore({"a.txt": "é" * 6})  # 6 characters, 12 bytes of UTF-8
        tool = self._tool(
            sandbox, store, files_in=replace(DEFAULT_TRANSFER_LIMITS, max_bytes_per_file=10)
        )

        assert "at most 10 bytes per file" in _run(tool, "print(1)", files=["a.txt"])

    def test_nothing_is_acquired_when_the_caps_refuse(self):
        backend = _backend(_ScriptedSandbox())
        store = InMemoryStore({"a": "1", "b": "2"})
        tool = _tool(
            backend,
            workspace_store=store,
            files_in=replace(DEFAULT_TRANSFER_LIMITS, max_files=1),
        )

        _run(tool, "print(1)", files=["a", "b"])
        assert backend.keys == []

    def test_a_set_within_the_caps_is_shared(self):
        sandbox = _ScriptedSandbox()
        store = InMemoryStore({"a.csv": "1", "b.csv": "2"})
        # Three, not two: the program is one of the files written into the sandbox.
        tool = self._tool(sandbox, store, files_in=replace(DEFAULT_TRANSFER_LIMITS, max_files=3))

        _run(tool, "print(1)", files=["a.csv", "b.csv"])
        run_dir = _run_dirs(sandbox)[0]
        assert {f"{run_dir}/a.csv", f"{run_dir}/b.csv"} <= set(sandbox.files)

    def test_the_program_itself_counts_against_the_file_count(self):
        """The spec requires `FILES_IN` even with no store, because `program.py` crosses this
        boundary too — so a tally that skipped it let `max_files=1` write two files."""
        sandbox = _ScriptedSandbox()
        store = InMemoryStore({"a.csv": "1"})
        tool = self._tool(sandbox, store, files_in=replace(DEFAULT_TRANSFER_LIMITS, max_files=1))

        out = _run(tool, "print(1)", files=["a.csv"])
        assert "at most 1" in out
        assert sandbox.contents == {}

    def test_an_over_count_call_reads_nothing_from_the_store(self):
        """A count cap that answers only once every requested file is in memory has already
        spent what it exists to bound."""
        sandbox = _ScriptedSandbox()
        store = _CountingStore({f"f{i}.csv": "x" for i in range(10)})
        tool = self._tool(sandbox, store, files_in=replace(DEFAULT_TRANSFER_LIMITS, max_files=3))

        out = _run(tool, "print(1)", files=sorted(store.files))
        assert "at most 3" in out
        assert store.reads == []
        assert sandbox.contents == {}

    def test_a_file_over_the_per_file_ceiling_stops_the_next_read(self):
        """A tally applied to the finished set bounds what crosses into the sandbox and nothing
        about what this process spent getting there."""
        sandbox = _ScriptedSandbox()
        store = _CountingStore({"a.csv": "x" * 100, "b.csv": "y", "c.csv": "z"})
        tool = self._tool(
            sandbox, store, files_in=replace(DEFAULT_TRANSFER_LIMITS, max_bytes_per_file=10)
        )

        out = _run(tool, "print(1)", files=["a.csv", "b.csv", "c.csv"])
        assert "at most 10 bytes per file" in out
        assert store.reads == ["a.csv"]

    def test_the_running_total_stops_the_next_read_too(self):
        sandbox = _ScriptedSandbox()
        store = _CountingStore({"a.csv": "x" * 8, "b.csv": "y" * 8, "c.csv": "z" * 8})
        tool = self._tool(
            sandbox, store, files_in=replace(DEFAULT_TRANSFER_LIMITS, max_total_bytes=20)
        )

        out = _run(tool, "print(1)", files=["a.csv", "b.csv", "c.csv"])
        assert "at most 20 per call" in out
        assert store.reads == ["a.csv", "b.csv"]

    def test_the_program_is_measured_before_the_store_is_touched(self):
        sandbox = _ScriptedSandbox()
        store = _CountingStore({"a.csv": "x"})
        tool = self._tool(
            sandbox, store, files_in=replace(DEFAULT_TRANSFER_LIMITS, max_bytes_per_file=10)
        )

        out = _run(tool, "print('" + "x" * 100 + "')", files=["a.csv"])
        assert "at most 10 bytes per file" in out
        assert store.reads == []

    def test_a_name_listed_twice_is_refused(self):
        """One read and one write per name; repeating one only multiplies both."""
        sandbox = _ScriptedSandbox()
        store = _CountingStore({"a.csv": "x"})
        tool = self._tool(sandbox, store)

        assert "listed twice" in _run(tool, "print(1)", files=["a.csv", "a.csv"])
        assert store.reads == []

    def test_the_program_itself_counts_against_the_byte_ceilings(self):
        """A large `code` cleared both ceilings while every shared file was measured."""
        sandbox = _ScriptedSandbox()
        tool = _tool(
            _backend(sandbox), files_in=replace(DEFAULT_TRANSFER_LIMITS, max_bytes_per_file=10)
        )

        out = _run(tool, "print('" + "x" * 100 + "')")
        assert "at most 10 bytes per file" in out
        assert sandbox.contents == {}

    def test_the_spec_carries_the_caps_the_host_chose(self):
        limits = replace(DEFAULT_TRANSFER_LIMITS, max_files=3)
        assert codeact_sandbox_spec(files_in=limits).files_in == limits


# ---------------------------------------------------------------------------
# Files out — two roads to a name, neither of them enumeration
# ---------------------------------------------------------------------------


class TestOutputsAreNeverEnumerated:
    def test_the_spec_requires_files_out_and_never_files_list(self):
        """A kind requiring `FILES_LIST` when it does not need one has made itself ACAS-only,
        and would attach locally and refuse in production or the reverse."""
        for mode in (CodeactOutputs.DECLARED, CodeactOutputs.MANIFEST):
            requires = codeact_sandbox_spec(outputs=mode).requires
            assert Capability.FILES_OUT in requires
            assert Capability.FILES_LIST not in requires

    def test_the_stdout_only_spec_requires_neither(self):
        assert codeact_sandbox_spec().requires == frozenset({Capability.EXEC, Capability.FILES_IN})

    def test_only_a_collecting_spec_says_it_names_outputs_later(self):
        assert codeact_sandbox_spec().outputs_named_at_call_time is False
        assert codeact_sandbox_spec(outputs=CodeactOutputs.DECLARED).outputs_named_at_call_time

    def test_a_backend_without_the_pull_surface_is_refused_at_attach(self):
        with pytest.raises(SandboxCapabilityNotSupported):
            _tool(_backend(), **_landing(CodeactOutputs.DECLARED))


class TestDeclaredOutputs:
    def test_a_declared_file_lands_under_its_own_name_not_the_run_directorys(self):
        """The guest path carries a run id and the landing name must not: a host writing files
        to disk would otherwise get one directory per call, named after nothing."""
        sandbox = _ProducingSandbox()
        sink = _RecordingSink()
        tool = _pulling_tool(sandbox, CodeactOutputs.DECLARED, sink)

        out = _run_producing(tool, sandbox, {"report.csv": b"a,b\n"}, outputs=["report.csv"])
        assert sink.names == ["report.csv"]
        assert _run_dirs(sandbox)[0] not in out

    def test_the_media_type_is_not_guessed(self):
        """Sniffing would let guest-produced content decide how the host handles it, and this
        kind genuinely does not know what its program wrote."""
        sandbox = _ProducingSandbox()
        sink = _RecordingSink()
        tool = _pulling_tool(sandbox, CodeactOutputs.DECLARED, sink)

        _run_producing(tool, sandbox, {"report.csv": b"a,b\n"}, outputs=["report.csv"])
        assert sink.media_types == [None]

    def test_a_name_declared_and_not_written_is_reported_rather_than_dropped(self):
        """The trade this kind would otherwise have to document: a file that goes uncollected
        with no error at all."""
        sandbox = _ProducingSandbox()
        sink = _RecordingSink()
        tool = _pulling_tool(sandbox, CodeactOutputs.DECLARED, sink)

        out = _run(tool, "print('hi')", outputs=["report.csv"])
        assert "Not written by the program" in out
        assert "report.csv" in out
        assert sink.names == []

    def test_what_was_written_still_lands_when_a_sibling_is_missing(self):
        """`required=False` throughout: failing the whole collection over one forgotten name
        would throw away the files the program did write."""
        sandbox = _ProducingSandbox()
        sink = _RecordingSink()
        tool = _pulling_tool(sandbox, CodeactOutputs.DECLARED, sink)

        out = _run_producing(tool, sandbox, {"a.csv": b"1"}, outputs=["a.csv", "b.csv"])
        assert sink.names == ["a.csv"]
        assert "b.csv" in out.rsplit("Not written", 1)[-1]

    def test_more_names_than_the_cap_are_refused_before_the_program_runs(self):
        sandbox = _ProducingSandbox()
        tool = _pulling_tool(
            sandbox,
            CodeactOutputs.DECLARED,
            _RecordingSink(),
            files_out=replace(DEFAULT_TRANSFER_LIMITS, max_files=2),
        )

        out = _run(tool, "print('hi')", outputs=["a", "b", "c"])
        assert "at most 2" in out
        assert sandbox.raw_commands == []

    @pytest.mark.parametrize("name", ["../escape.csv", "/etc/passwd", "a/./b.csv", "a\\b.csv"])
    def test_a_name_breaking_the_narrow_invariant_is_refused_before_the_program_runs(
        self, name: str
    ):
        sandbox = _ProducingSandbox()
        tool = _pulling_tool(sandbox, CodeactOutputs.DECLARED, _RecordingSink())

        out = _run(tool, "print('hi')", outputs=[name])
        assert "cannot be saved" in out
        assert sandbox.raw_commands == []

    def test_a_name_too_long_once_the_run_directory_is_counted_is_refused_up_front(self):
        """The guest path carries a 13-byte prefix, so judging the bare name accepts a
        250-byte one here and has `collect_outputs` refuse the 263-byte declaration it becomes
        — after the program has run, for a reason the model could not have foreseen."""
        sandbox = _ProducingSandbox()
        sink = _RecordingSink()
        tool = _pulling_tool(sandbox, CodeactOutputs.DECLARED, sink)
        name = "a" * (MAX_ARTIFACT_NAME_BYTES - 5)  # valid on its own, over budget with a prefix

        out = _run(tool, "print('hi')", outputs=[name])
        assert "over the 255-byte ceiling" in out
        assert sandbox.raw_commands == []
        assert sink.names == []

    def test_a_name_that_only_grows_past_the_ceiling_once_normalized_is_refused_up_front(self):
        """The delivered spelling is what `collect_outputs` judges: 43 × U+0958 is 129 bytes as
        declared and 258 after NFC, so checking the bare name lets the program run first."""
        sandbox = _ProducingSandbox()
        sink = _RecordingSink()
        tool = _pulling_tool(sandbox, CodeactOutputs.DECLARED, sink)

        out = _run(tool, "print('hi')", outputs=["क़" * 43])
        assert "ceiling" in out
        assert sandbox.raw_commands == []

    def test_two_names_that_are_one_file_once_saved_are_refused_up_front(self):
        """`collect_outputs` keys collisions on the NFC-lowered name, so an exact-match check
        here would let the pair through and have the whole collection refused after the run."""
        sandbox = _ProducingSandbox()
        tool = _pulling_tool(sandbox, CodeactOutputs.DECLARED, _RecordingSink())

        out = _run(tool, "print('hi')", outputs=["Report.csv", "report.csv"])
        assert "one file once saved" in out
        assert sandbox.raw_commands == []

    def test_a_sink_that_rewrites_nothing_still_gets_the_normalized_collision_check(self):
        """Opting out of normalization disables the *rewrite*, never the comparison — which is
        `collect_outputs`' rule, so keying on the raw spelling here lets the pair run first."""
        sandbox = _ProducingSandbox()
        sink = _RecordingSink(normalization=NameNormalization.NONE)
        tool = _pulling_tool(sandbox, CodeactOutputs.DECLARED, sink)

        # Escapes, not literals: the two spellings are indistinguishable in a source file and
        # an editor that normalizes one of them turns this into a test of nothing.
        out = _run(tool, "print('hi')", outputs=["cafe\u0301.csv", "caf\u00e9.csv"])
        assert "one file once saved" in out
        assert sandbox.raw_commands == []

    def test_a_name_that_fits_with_the_prefix_is_accepted(self):
        """The other side of the bound, so the budget cannot drift into refusing everything."""
        sandbox = _ProducingSandbox()
        sink = _RecordingSink()
        tool = _pulling_tool(sandbox, CodeactOutputs.DECLARED, sink)
        name = "a" * (MAX_ARTIFACT_NAME_BYTES - 13)

        _run_producing(tool, sandbox, {name: b"1"}, outputs=[name])
        assert sink.names == [name]

    def test_a_name_declared_twice_is_refused(self):
        sandbox = _ProducingSandbox()
        tool = _pulling_tool(sandbox, CodeactOutputs.DECLARED, _RecordingSink())

        assert "declared twice" in _run(tool, "print('hi')", outputs=["a.csv", "a.csv"])

    def test_the_program_file_cannot_be_declared_as_an_output(self):
        sandbox = _ProducingSandbox()
        tool = _pulling_tool(sandbox, CodeactOutputs.DECLARED, _RecordingSink())

        assert "cannot be saved" in _run(tool, "print('hi')", outputs=[_PROGRAM_FILENAME])

    def test_a_failed_program_reports_its_traceback_and_nothing_about_files(self):
        """A missing-file report stacked on a traceback buries what the model has to fix."""
        sandbox = _ProducingSandbox(result=ExecResult(stdout="", stderr="Traceback", exit_code=1))
        sink = _RecordingSink()
        tool = _pulling_tool(sandbox, CodeactOutputs.DECLARED, sink)

        out = _run(tool, "print('hi')", outputs=["report.csv"])
        assert "Traceback" in out
        assert "Not written" not in out
        assert sink.names == []

    def test_declaring_nothing_says_nothing_about_files(self):
        sandbox = _ProducingSandbox(result=ExecResult(stdout="42\n"))
        tool = _pulling_tool(sandbox, CodeactOutputs.DECLARED, _RecordingSink())

        assert _run(tool, "print(42)") == "stdout:\n42"

    def test_a_name_normalized_on_the_way_out_is_not_also_reported_missing(self):
        """`collect_outputs` normalizes a landing name to NFC, so a declared `e` + combining
        acute is delivered as the precomposed `é`. Comparing the two spellings exactly reports
        a file that landed perfectly well as never written."""
        decomposed, composed = "café.csv", "café.csv"
        sandbox = _ProducingSandbox()
        sink = _RecordingSink()
        tool = _pulling_tool(sandbox, CodeactOutputs.DECLARED, sink)

        out = _run_producing(tool, sandbox, {decomposed: b"1"}, outputs=[decomposed])
        assert sink.names == [composed]
        assert "Not written" not in out

    def test_a_genuinely_missing_name_is_still_reported_under_normalization(self):
        """The other direction, so the fix above cannot be 'never report anything missing'."""
        decomposed = "café.csv"
        sandbox = _ProducingSandbox()
        sink = _RecordingSink()
        tool = _pulling_tool(sandbox, CodeactOutputs.DECLARED, sink)

        out = _run(tool, "print(1)", outputs=[decomposed])
        assert "Not written" in out
        assert sink.names == []

    def test_neither_the_bytes_nor_the_hosts_handle_reach_the_model(self):
        """A handle can be a SAS URL with a bearer token in its query string, and a tool result
        is persisted into the transcript and replayed every turn after."""
        sandbox = _ProducingSandbox()
        sink = _RecordingSink()
        tool = _pulling_tool(sandbox, CodeactOutputs.DECLARED, sink)

        out = _run_producing(tool, sandbox, {"r.csv": b"SECRET-BYTES"}, outputs=["r.csv"])
        assert "SECRET-BYTES" not in out
        assert "sig=secret" not in out
        assert "saved r.csv" in out


class TestManifestOutputs:
    def test_files_the_manifest_lists_are_landed(self):
        sandbox = _ProducingSandbox()
        sink = _RecordingSink()
        tool = _pulling_tool(sandbox, CodeactOutputs.MANIFEST, sink)

        out = _run_producing(
            tool,
            sandbox,
            {
                _MANIFEST_FILENAME: b'{"outputs": [{"path": "r.csv"}]}',
                "r.csv": b"1,2",
            },
        )
        assert sink.names == ["r.csv"]
        assert "saved r.csv" in out

    def test_a_media_type_in_the_manifest_is_ignored_rather_than_forwarded(self):
        """The guest declaring how the host should handle its own bytes is worse than the
        sniffing `DeclaredOutput.media_type` exists to forbid: a sink may route on that value
        to choose inline rendering. This kind does not know what its program wrote."""
        sandbox = _ProducingSandbox()
        sink = _RecordingSink()
        tool = _pulling_tool(sandbox, CodeactOutputs.MANIFEST, sink)

        _run_producing(
            tool,
            sandbox,
            {
                _MANIFEST_FILENAME: (
                    b'{"outputs": [{"path": "r.svg", "media_type": "image/svg+xml"}]}'
                ),
                "r.svg": b"<svg/>",
            },
        )
        assert sink.names == ["r.svg"]
        assert sink.media_types == [None]

    def test_no_manifest_means_nothing_was_saved(self):
        sandbox = _ProducingSandbox()
        sink = _RecordingSink()
        tool = _pulling_tool(sandbox, CodeactOutputs.MANIFEST, sink)

        out = _run(tool, "print('hi')")
        assert _MANIFEST_FILENAME in out
        assert sink.names == []

    @pytest.mark.parametrize(
        "manifest",
        [b"not json", b"[]", b'{"outputs": {}}', b'{"outputs": [{"media_type": "text/csv"}]}'],
    )
    def test_a_malformed_manifest_is_a_diagnostic_and_lands_nothing(self, manifest: bytes):
        sandbox = _ProducingSandbox()
        sink = _RecordingSink()
        tool = _pulling_tool(sandbox, CodeactOutputs.MANIFEST, sink)

        out = _run_producing(tool, sandbox, {_MANIFEST_FILENAME: manifest})
        assert "Error:" in out
        assert sink.names == []

    def test_a_deeply_nested_manifest_is_a_diagnostic_rather_than_a_dead_turn(self):
        """`RecursionError` is neither a decode error nor a JSON one, and a few thousand nested
        arrays fit in a fraction of the size ceiling — so it used to leave the tool body and
        take the caller's turn with it, from a file the guest program chose to write."""
        sandbox = _ProducingSandbox()
        sink = _RecordingSink()
        tool = _pulling_tool(sandbox, CodeactOutputs.MANIFEST, sink)
        nested = b"[" * 20_000 + b"]" * 20_000
        assert len(nested) < _MANIFEST_MAX_BYTES

        out = _run_producing(tool, sandbox, {_MANIFEST_FILENAME: nested})
        assert "nested too deeply" in out
        assert sink.names == []

    def test_a_manifest_naming_a_path_outside_the_run_is_refused(self):
        """The names are the guest's here rather than the model's, and the same invariant
        holds: this is the first channel where a guest-chosen name reaches host state."""
        sandbox = _ProducingSandbox()
        sink = _RecordingSink()
        tool = _pulling_tool(sandbox, CodeactOutputs.MANIFEST, sink)

        out = _run_producing(
            tool, sandbox, {_MANIFEST_FILENAME: b'{"outputs": [{"path": "../escape"}]}'}
        )
        assert "cannot be saved" in out
        assert sink.names == []

    def test_the_manifest_itself_is_never_landed(self):
        sandbox = _ProducingSandbox()
        sink = _RecordingSink()
        tool = _pulling_tool(sandbox, CodeactOutputs.MANIFEST, sink)

        out = _run_producing(
            tool,
            sandbox,
            {_MANIFEST_FILENAME: f'{{"outputs": [{{"path": "{_MANIFEST_FILENAME}"}}]}}'.encode()},
        )
        assert "cannot be saved" in out
        assert sink.names == []

    def test_a_manifest_over_the_file_cap_lands_nothing(self):
        sandbox = _ProducingSandbox()
        sink = _RecordingSink()
        tool = _pulling_tool(
            sandbox,
            CodeactOutputs.MANIFEST,
            sink,
            files_out=replace(DEFAULT_TRANSFER_LIMITS, max_files=1),
        )

        out = _run_producing(
            tool,
            sandbox,
            {
                _MANIFEST_FILENAME: b'{"outputs": [{"path": "a"}, {"path": "b"}]}',
                "a": b"1",
                "b": b"2",
            },
        )
        assert "at most 1" in out
        assert sink.names == []

    def test_an_oversized_manifest_is_refused_before_it_is_read(self):
        """Stat, refuse, then read — the pull surface's contract. A backend whose SDK buffers
        the whole response has spent the memory before `max_bytes` is looked at, so passing a
        ceiling down is not a bound on its own."""
        sandbox = _StatOnlySandbox(size_bytes=_MANIFEST_MAX_BYTES + 1)
        sink = _RecordingSink()
        tool = _pulling_tool(sandbox, CodeactOutputs.MANIFEST, sink)

        out = _run(tool, "print('hi')")
        assert "reads at most" in out
        assert sandbox.reads == []
        assert sink.names == []

    def test_a_manifest_of_unknown_size_fails_closed(self):
        """Coercing an unknown size to zero would make the ceiling read it as free."""
        sandbox = _StatOnlySandbox(size_bytes=None)
        sink = _RecordingSink()
        tool = _pulling_tool(sandbox, CodeactOutputs.MANIFEST, sink)

        out = _run(tool, "print('hi')")
        assert "unknown" in out
        assert sandbox.reads == []

    def test_no_outputs_parameter_is_offered_in_this_mode(self):
        """The program's own listing is the channel; a second one would contradict it."""
        sandbox = _ProducingSandbox()
        tool = _pulling_tool(sandbox, CodeactOutputs.MANIFEST, _RecordingSink())
        assert "outputs" not in inspect.signature(_callable(tool)).parameters


class TestTheSinkIsTheHostsChoice:
    def test_an_output_mode_without_a_sink_is_refused_at_attach(self):
        """With `OutputSink` the kind never chooses where artifacts go — and a kind that
        landed them where the agent's own file tools write would have handed model-written
        code an unapproved `file_access_write`."""
        with pytest.raises(SandboxOutputSinkRequired):
            _tool(
                _backend(capabilities=_PULLS),
                outputs=CodeactOutputs.DECLARED,
            )

    def test_a_sink_with_no_output_mode_is_refused_at_attach(self):
        with pytest.raises(ValueError, match="nothing would ever be landed"):
            _tool(_backend(), output_sink=_RecordingSink().sink)

    @pytest.mark.parametrize("router", [None, SandboxRouter([])])
    def test_an_unconfigured_host_gets_an_empty_list_from_that_refusal_too(self, router):
        """Rule 1 of `sandboxed_tool`: a host that simply left sandboxing off keeps its
        ungrounded behaviour. A check placed before the attach gate would raise out of that
        host's agent factory instead — which is what this one did."""
        assert (
            make_codeact_tools(
                router, "data-analyst", _context(), output_sink=_RecordingSink().sink
            )
            == []
        )

    def test_an_unconfigured_host_gets_one_from_the_missing_sink_refusal_as_well(self):
        assert (
            make_codeact_tools(None, "data-analyst", _context(), outputs=CodeactOutputs.DECLARED)
            == []
        )

    def test_the_cap_the_host_asked_for_reaches_the_tool(self):
        """Closed egress and a sink: bytes still reach host state, so the flow is real."""
        tool = _tool(
            _backend(capabilities=_PULLS),
            **_landing(CodeactOutputs.DECLARED),
            outbound_max_confidentiality="private",
        )
        assert dict(tool.additional_properties or {}) == {"max_allowed_confidentiality": "private"}

    def test_it_still_declares_no_source_integrity(self):
        """Landing files changes nothing about where the tool's *result* came from."""
        tool = _tool(_backend(capabilities=_PULLS), **_landing(CodeactOutputs.DECLARED))
        assert "source_integrity" not in dict(tool.additional_properties or {})


# ---------------------------------------------------------------------------
# Dependency discipline — every import must be traceable to a reason
# ---------------------------------------------------------------------------

#: A requirement string's distribution name is not always its import name: `pip install
#: agent-framework-core` puts `agent_framework` on the path and `maf-sandbox` puts
#: `maf_sandbox` on it. Anything not listed here is assumed to import under its distribution
#: name with hyphens turned to underscores. A dependency where that guess is wrong fails the
#: test below with a readable "imports X" message, which is the right place to notice a new
#: exception belongs here.
_DISTRIBUTION_TO_IMPORT_NAME = {
    "agent-framework-core": "agent_framework",
    "maf-sandbox": "maf_sandbox",
}


def _package_modules():
    """Every module in the installed `maf_sandbox_codeact`, as `{stem: path}`."""
    import pathlib

    import maf_sandbox_codeact

    root = pathlib.Path(maf_sandbox_codeact.__file__).parent  # type: ignore[arg-type]
    return {path.stem: path for path in root.rglob("*.py")}


def _imported_top_levels(path):
    """The absolute top-level module names imported by the file at `path`."""
    import ast

    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level > 0:
                continue  # relative import — within this package, not a dependency
            top = (node.module or "").split(".")[0]
            if top:
                names.append(top)
    return names


def _declared_import_names():
    """The import names `pyproject.toml` licenses `maf_sandbox_codeact` to reach for, or `None`.

    `None` means there is no `pyproject.toml` next to the installed package — an
    sdist/wheel-only install with no source tree alongside it — and the caller must skip
    rather than let an empty dependency list pass the scan below vacuously.
    """
    import pathlib
    import re
    import tomllib

    import maf_sandbox_codeact

    root = pathlib.Path(maf_sandbox_codeact.__file__).parents[2]  # type: ignore[arg-type]
    pyproject_path = root / "pyproject.toml"
    if not pyproject_path.is_file():
        return None

    with pyproject_path.open("rb") as fh:
        requirements = tomllib.load(fh)["project"]["dependencies"]

    names: set[str] = set()
    for requirement in requirements:
        match = re.match(r"[A-Za-z0-9][A-Za-z0-9._-]*", requirement)
        assert match is not None, f"unparseable dependency requirement: {requirement!r}"
        distribution = match.group(0)
        names.add(_DISTRIBUTION_TO_IMPORT_NAME.get(distribution, distribution.replace("-", "_")))
    return names


class TestOnlyDeclaredDependencies:
    """Every module here imports only the standard library, itself, or a declared dependency.

    Nothing else would notice a stray import: the workspace running this suite has every
    sibling package importable, so an undeclared name resolves fine here regardless. The
    first sign of trouble is a downstream consumer who installs the published wheel alone,
    and what they get is an ``ImportError`` with no test pointing at the cause.
    """

    def test_sources_exist(self):
        """Guards the scan below against silently finding nothing."""
        assert len(_package_modules()) >= 2

    def test_every_module_only_imports_what_it_is_declared_to_need(self):
        import sys

        declared = _declared_import_names()
        if declared is None:
            pytest.skip(
                "pyproject.toml is not next to the installed maf_sandbox_codeact package — "
                "this check only runs against a source checkout, not an installed-only wheel"
            )

        allowed = set(sys.stdlib_module_names) | declared | {"maf_sandbox_codeact"}
        offenders = [
            f"{path.name}: import {name}"
            for _, path in sorted(_package_modules().items())
            for name in _imported_top_levels(path)
            if name not in allowed
        ]
        assert offenders == [], (
            f"these maf_sandbox_codeact modules import something outside the standard "
            f"library, the package itself, and pyproject.toml's declared dependencies: "
            f"{offenders}. Either the import is a mistake, or the dependency belongs in "
            "pyproject.toml."
        )


class TestNoDirectAzureImport:
    """Acceptance criterion for this split: the same kind must run on any backend.

    ``azure`` is not a declared dependency, so ``TestOnlyDeclaredDependencies`` above already
    catches an ``import azure`` here — this test is kept alongside it because it names the
    specific portability property and its failure message says what actually broke: the kind
    reaching around ``maf_sandbox`` for a provider directly.
    """

    def test_the_workload_does_not_import_azure(self):
        import pathlib
        import re

        import maf_sandbox_codeact

        root = pathlib.Path(maf_sandbox_codeact.__file__).parent  # type: ignore[arg-type]
        pattern = re.compile(r"(?m)^\s*(?:from\s+azure[.\s]|import\s+azure[.\s])")
        offenders = [
            str(p) for p in root.rglob("*.py") if pattern.search(p.read_text(encoding="utf-8"))
        ]
        assert offenders == [], (
            f"the codeact workload imports Azure directly: {offenders}. "
            "It must reach a sandbox through maf_sandbox, or it stops being portable."
        )
