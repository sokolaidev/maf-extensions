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
import json
import warnings
from collections.abc import Callable, Sequence
from dataclasses import replace
from typing import Any

import pytest
from maf_sandbox import (
    DEFAULT_CAPABILITIES,
    DEFAULT_TRANSFER_LIMITS,
    MAX_ARTIFACT_NAME_BYTES,
    SHIM_MODULE,
    WORK_DIRECTORY,
    Artifact,
    CallerContext,
    Capability,
    EntryKind,
    ExecResult,
    GuestRunLayout,
    HostToolRegistry,
    Identity,
    LandedArtifact,
    MafSandboxHostToolsWarning,
    NameNormalization,
    OutputSink,
    SandboxCapabilityNotSupported,
    SandboxEntry,
    SandboxOutputSinkRequired,
    SandboxRouter,
    SourceIntegrity,
    TransferLimits,
    guest_run_layout,
    host_tool_shim,
    launcher_script,
    sandbox_tool,
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
    _SMALLEST_MANIFEST,
    _WORK_DIR,
)

#: What a backend must declare before this kind may collect anything.
_PULLS = DEFAULT_CAPABILITIES | {Capability.FILES_OUT}

#: And before a program may reach a host tool: dispatch carries its requests over the same
#: pull surface, so it needs everything a collection needs and the capability besides.
_DISPATCHES = _PULLS | {Capability.HOST_TOOLS}

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

    def _program_cwd(self, working_directory: str) -> str:
        """Where a program writing a relative filename lands it — the directory `exec` was
        given, for a run this kind starts itself."""
        return working_directory

    async def exec(self, command, *, working_directory, timeout):
        result = await super().exec(command, working_directory=working_directory, timeout=timeout)
        cwd = self._program_cwd(working_directory)
        for name, content in self.produces.items():
            self.contents[f"{cwd}/{name}"] = content
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


class _CallingSandbox(_ScriptedSandbox):
    """A sandbox whose "program" dispatches one host tool, prints the answer, and exits.

    The interleaving is what a real guest has and what a scripted ``exec`` cannot express: the
    request appears when the launcher starts, and the exit marker only once the supervisor's
    answer has landed. Every path is taken from :func:`~maf_sandbox.guest_run_layout` over the
    working directory the launcher was given, so a kind addressing the transport by any other
    name is not served here either.
    """

    def __init__(self, name: str, arguments: dict[str, Any] | None = None, **kwargs) -> None:
        super().__init__(**kwargs)
        self.answers: list[dict[str, Any]] = []
        self.layouts: list[GuestRunLayout] = []
        self._call = (name, arguments or {})
        self._outstanding: GuestRunLayout | None = None

    async def exec(self, command, *, working_directory, timeout):
        result = await super().exec(command, working_directory=working_directory, timeout=timeout)
        layout = guest_run_layout(working_directory, program=_PROGRAM_FILENAME)
        self.layouts.append(layout)
        self._outstanding = layout
        name, arguments = self._call
        self.contents[f"{layout.calls}/0001.request.json"] = json.dumps(
            {"id": "0001", "name": name, "arguments": arguments}
        ).encode()
        return result

    async def stat_file(self, path, *, working_directory):
        self._take_the_answer()
        return await super().stat_file(path, working_directory=working_directory)

    def _take_the_answer(self) -> None:
        """Read the response if it has landed, print what it said, and end the program."""
        layout = self._outstanding
        if layout is None:
            return
        answered = self.contents.get(f"{layout.calls}/0001.response.json")
        if answered is None:
            return
        self._outstanding = None
        self.answers.append(json.loads(answered))
        told = self.answers[-1].get("value", self.answers[-1].get("refusal"))
        self.contents[layout.output] = f"the host said {told}".encode()
        self.contents[layout.exit_code] = b"0"


class _FinishingSandbox(_ProducingSandbox):
    """A dispatch-served guest that produces its files and then leaves the exit marker.

    The supervisor polls for that marker, so a guest that never writes one is waited out. This
    one calls nothing: what it stands in for is a dispatched run that simply *succeeds*.
    """

    def _program_cwd(self, working_directory: str) -> str:
        """The launcher `cd`s into the work directory before starting the program, so that is
        where a relative filename lands — not the run directory `exec` was handed."""
        return guest_run_layout(working_directory, program=_PROGRAM_FILENAME).work

    async def exec(self, command, *, working_directory, timeout):
        result = await super().exec(command, working_directory=working_directory, timeout=timeout)
        layout = guest_run_layout(working_directory, program=_PROGRAM_FILENAME)
        self.contents[layout.output] = b"ran"
        self.contents[layout.exit_code] = b"0"
        return result


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


class _ShrinkingManifestSandbox(_ProducingSandbox):
    """Reports the manifest as tiny once it has been read — a guest still running after `exec`.

    The protocol says a stat is a promise about a file the guest may still rewrite, so a second
    stat of the manifest is worth exactly nothing. This is the only way to show that its cost is
    charged from the bytes that were actually read.
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.manifest_read = False

    async def stat_file(self, path, *, working_directory):
        entry = await super().stat_file(path, working_directory=working_directory)
        if entry is not None and self.manifest_read and path.endswith(_MANIFEST_FILENAME):
            return replace(entry, size_bytes=2)
        return entry

    async def read_file(self, path, *, working_directory, max_bytes):
        content = await super().read_file(
            path, working_directory=working_directory, max_bytes=max_bytes
        )
        if path.endswith(_MANIFEST_FILENAME):
            self.manifest_read = True
        return content


class _FailingSink(_RecordingSink):
    """A host store that accepts the first artifact and then breaks."""

    async def deliver(self, artifact: Artifact) -> LandedArtifact:
        if self.delivered:
            raise RuntimeError("the store went away")
        return await super().deliver(artifact)


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


def _context(*, thread_id: str | None = "thread-1") -> CallerContext:
    return CallerContext(
        current_scope=lambda: "scope-a",
        current_thread_id=lambda: thread_id,
        list_files=_listing,
    )


def _tool(backend: InProcessSandboxBackend, *, thread_id: str | None = "thread-1", **kw):
    tools = make_codeact_tools(
        # Below the default floor: this suite exercises the workload, not the floor check. Read
        # off the backend rather than named, so renaming the ladder's bottom rung is not a
        # change to this package.
        SandboxRouter([backend], min_isolation=backend.isolation),
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


# --- Host tools: one stamped function per leg a registry can carry ------------------------


@sandbox_tool(source=SourceIntegrity.TRUSTED, sink=None, identity=Identity.APP)
def _exchange_rate(pair: str) -> float:
    return 1.0


@sandbox_tool(source=None, sink="the-crm", identity=Identity.APP)
def _log_to_crm(note: str) -> None:
    return None


@sandbox_tool(source=None, sink=None, identity=None)
def _round_half_up(value: float) -> int:
    return int(value + 0.5)


@sandbox_tool(source=None, sink=None, identity=Identity.USER)
def _the_callers_calendar() -> list[str]:
    return []


def _unstamped_lookup(query: str) -> str:
    """No stamp at all, which the library's default gate registers without complaint."""
    return ""


def _registry(*tools: Callable[..., Any], **policy: Any) -> HostToolRegistry:
    """A registry serving `tools` under `policy`, with the registration notice filtered out.

    That notice is the host's to read once, and the filter below is the one it names itself.
    """
    registry = HostToolRegistry(**policy)
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", category=MafSandboxHostToolsWarning)
        for func in tools:
            registry.register(func)
    return registry


def _dispatching_tool(registry: HostToolRegistry, **kw: Any):
    """The tool a host gets for `registry`, on a backend that can serve what dispatch needs."""
    return _tool(_backend(capabilities=_DISPATCHES), host_tools=registry, **kw)


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
        assert codeact_sandbox_spec().work_dir == _WORK_DIR == "/maf-sandbox/work"

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

    def test_a_registry_holding_nothing_leaves_the_spec_where_it_was(self):
        """An empty registry is a dispatch surface that does not exist, and reads as one."""
        assert codeact_sandbox_spec(host_tools=_registry()) == codeact_sandbox_spec()

    def test_a_registry_adds_the_capability_and_the_surface_dispatch_travels_over(self):
        """`FILES_OUT` is not optional here, and not this kind's output channel either: the
        transport stats and reads the program's request files and its exit marker, so even a
        stdout-only program that can call a host function needs the pull surface."""
        spec = codeact_sandbox_spec(host_tools=_registry(_exchange_rate))
        assert spec.requires == frozenset(
            {Capability.EXEC, Capability.FILES_IN, Capability.FILES_OUT, Capability.HOST_TOOLS}
        )

    def test_a_registry_of_pure_computation_widens_it_just_the_same(self):
        """The capability follows from something being dispatchable at all, never from what
        the aggregate found in it: a tool that is no source, no sink and no authority is
        still a call crossing the boundary."""
        spec = codeact_sandbox_spec(host_tools=_registry(_round_half_up))
        assert Capability.HOST_TOOLS in spec.requires
        assert spec.identities == frozenset()

    def test_the_identities_a_registry_declares_reach_the_spec(self):
        """Which is what the router's `denied_identities` is matched against at attach."""
        spec = codeact_sandbox_spec(host_tools=_registry(_exchange_rate, _the_callers_calendar))
        assert spec.identities == frozenset({Identity.APP, Identity.USER})

    def test_reading_a_registry_seals_it(self):
        """A tool registered afterwards would be dispatchable from a spec that never saw it."""
        registry = _registry(_exchange_rate)
        codeact_sandbox_spec(host_tools=registry)
        with pytest.raises(ValueError, match="sealed"):
            registry.register(_round_half_up)


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

    def test_a_registry_on_a_backend_that_cannot_serve_host_tools_is_refused_at_attach(self):
        """No shipped backend declares `HOST_TOOLS`, so wiring a registry has to fail where
        the tool is *built* — not at the first call, and not silently. That is the promise the
        module docstring and the README both make, and this is where it is held.

        `_PULLS` is what a real backend offers today. The refusal has to come from the
        capability match rather than from the registry being empty, so the registry here has a
        tool in it.
        """
        with pytest.raises(SandboxCapabilityNotSupported):
            _tool(_backend(capabilities=_PULLS), host_tools=_registry(_round_half_up))


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

    def test_an_empty_registry_declares_nothing_either(self):
        """Nothing dispatchable is nothing carried, whatever cap the host holds."""
        tool = _dispatching_tool(_registry(), outbound_max_confidentiality="private")
        assert dict(tool.additional_properties or {}) == {}

    def test_a_registry_with_no_sink_tool_leaves_the_cap_unwritten(self):
        """A source brings data *in* and pure computation carries nothing at all, so the flow
        the cap gates still does not exist — and a cap on a flow that cannot happen gates
        calls for nothing."""
        registry = _registry(_exchange_rate, _round_half_up)
        tool = _dispatching_tool(registry, outbound_max_confidentiality="private")
        assert dict(tool.additional_properties or {}) == {}

    def test_a_sink_tool_makes_the_hosts_cap_apply_with_nothing_landing(self):
        """Egress is closed and no artifact lands, and the surface carries something out
        anyway — the one flow a derivation reading only the spec cannot see. What is written
        is the host's own cap, never the tool's sink value: the two are vocabularies this
        package refuses to order against each other."""
        tool = _dispatching_tool(_registry(_log_to_crm), outbound_max_confidentiality="private")
        assert dict(tool.additional_properties or {}) == {"max_allowed_confidentiality": "private"}

    def test_a_sink_tool_beside_an_output_sink_still_attaches(self):
        """`sandboxed_tool` refuses an explicit mapping together with a sink, and there is
        nothing to override anyway: a spec that lands already has the derivation writing the
        very same cap."""
        tool = _tool(
            _backend(capabilities=_DISPATCHES),
            host_tools=_registry(_log_to_crm),
            outbound_max_confidentiality="private",
            **_landing(CodeactOutputs.DECLARED),
        )
        assert dict(tool.additional_properties or {}) == {"max_allowed_confidentiality": "private"}

    def test_an_unstamped_tool_carries_the_hosts_cap_as_a_sink_tool_would(self):
        """Nobody answered the sink question, so the fold sees no sink to write a cap from —
        and the guest can still dispatch that function with conversation-derived arguments.
        Every other undeclared leg fails safe (untrusted source, APP identity); so does this
        one, or a confidential conversation reaches `execute_code` ungated."""
        tool = _dispatching_tool(
            _registry(_unstamped_lookup), outbound_max_confidentiality="private"
        )
        assert dict(tool.additional_properties or {}) == {"max_allowed_confidentiality": "private"}

    def test_no_source_integrity_is_declared_however_the_registry_is_stamped(self):
        """A registry of trusted lookups does not make a model-written `print(...)` trusted."""
        registry = _registry(_exchange_rate, _log_to_crm)
        tool = _dispatching_tool(registry, outbound_max_confidentiality="private")
        assert "source_integrity" not in dict(tool.additional_properties or {})


class TestWhatARegistryDoesBeyondTheSpec:
    """The two things a host's registry decides here that its own `requires` does not say."""

    def test_a_user_identity_tool_gates_every_call_on_approval(self):
        """A dispatch may exercise the caller's own delegated authority, and which call does
        is not knowable before the program runs — so one such tool raises the whole surface."""
        tool = _dispatching_tool(_registry(_the_callers_calendar))
        assert tool.approval_mode == "always_require"

    def test_the_applications_own_authority_does_not_gate_it(self):
        tool = _dispatching_tool(_registry(_exchange_rate, _log_to_crm))
        assert tool.approval_mode == "never_require"

    @pytest.mark.parametrize("tools", [(), (_exchange_rate,)])
    def test_the_registry_is_sealed_once_the_factory_has_run(self, tools):
        """The empty one too: sealing costs nothing there, and it turns "registered a tool
        after the tool was built" into a refusal at the host's own `register` rather than a
        registration that quietly reaches nothing."""
        registry = _registry(*tools)
        _dispatching_tool(registry)
        with pytest.raises(ValueError, match="sealed"):
            registry.register(_round_half_up)


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

#: `execute_code`'s `__doc__` with no file store, no output mode and no registry wired.
_UNWIRED_DESCRIPTION = """Run a short Python program inside a sandbox and return what it printed.

        Use this to compute rather than to reason: parse, transform, count, check, simulate —
        anything where running the code beats predicting what it would do.  The program runs
        as ``python3 program.py`` in a sandbox with **no network access**, so it can compute
        but cannot fetch.

        **Only what you print is read back as text.**  There is no REPL echo and the value of
        the last expression is not returned, so end the program with ``print(...)`` of
        everything you need to see.

        Write a complete, self-contained program every time.  Each call gets a fresh working
        directory: nothing you did not pass in to *this* call is in it.

        Args:
            code: The Python source to run.  The standard library, plus
                whatever the sandbox image ships.

        Returns:
            The program's stdout, its stderr when it wrote any, and its exit
            code when that was not zero.  If the sandbox is unavailable the tool returns an
            error message instead, so the run degrades rather than blocking.
        """


class TestToolDescription:
    def _description(self, **kw) -> str:
        return _callable(_tool(_backend(capabilities=_PULLS), **kw)).__doc__ or ""

    def test_a_host_that_wires_no_channel_gets_exactly_this_text(self):
        """A literal, not a comparison against another description this same code builds: a
        change that shifts every description shifts both sides of such a comparison and it
        stays green.  Every word and every line break below reaches the model as ``__doc__``,
        so an edit that reaches this leg has to be made here too, deliberately."""
        assert self._description() == _UNWIRED_DESCRIPTION

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
        described = self._description(file_store=InMemoryStore({}))
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


class TestToolDescriptionWithHostTools:
    """What a non-empty `host_tools` registry adds to the description the model reads."""

    def test_every_registered_tool_is_named(self):
        registry = _registry(_exchange_rate, _log_to_crm, _round_half_up)
        described = _callable(_dispatching_tool(registry)).__doc__ or ""
        assert "_exchange_rate" in described
        assert "_log_to_crm" in described
        assert "_round_half_up" in described

    def test_an_empty_registry_reads_exactly_like_no_registry_at_all(self):
        plain = _callable(_tool(_backend(capabilities=_PULLS))).__doc__ or ""
        with_empty_registry = _callable(_dispatching_tool(_registry())).__doc__ or ""
        assert with_empty_registry == plain
        assert "maf_host_tools" not in with_empty_registry

    def test_the_network_claim_is_qualified_only_once_a_tool_is_registered(self):
        without = _callable(_tool(_backend(capabilities=_PULLS))).__doc__ or ""
        described = _callable(_dispatching_tool(_registry(_exchange_rate))).__doc__ or ""
        assert "no network access" in without
        assert "no network access" not in described
        assert "no network of its own" in described
        assert "no network of its own" not in without

    def test_the_call_form_matches_what_the_shim_actually_generates(self):
        """The syntax the model is told to write must be the syntax the generated shim
        module accepts — checked against the real generated source, not a copy of it."""
        registry = _registry(_exchange_rate)
        described = _callable(_dispatching_tool(registry)).__doc__ or ""
        generated = host_tool_shim(registry.names())
        module = SHIM_MODULE.removesuffix(".py")

        assert f"import {module}" in described
        assert "def call(name, **arguments):" in generated
        assert f"{module}.call(" in described
        assert "keyword" in described
        assert "class HostToolError" in generated
        assert f"{module}.HostToolError" in described

    def test_the_returns_contract_says_where_a_traceback_actually_lands(self):
        """The launcher merges the program's stderr into its stdout, so the plain sentence
        would send a model looking for its traceback in a section that cannot hold one."""
        launcher = launcher_script(guest_run_layout("/w/run", program=_PROGRAM_FILENAME))
        plain = _callable(_tool(_backend(capabilities=_PULLS))).__doc__ or ""
        described = _callable(_dispatching_tool(_registry(_exchange_rate))).__doc__ or ""

        assert "2>&1" in launcher
        assert "its stderr when it wrote any" in plain
        assert "its stderr when it wrote any" not in described
        assert "traceback comes back under ``stdout``" in described
        assert "host's note about the run" in described


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
        tool = _tool(_backend(sandbox), file_store=store)

        _run(tool, "print('hi')", files=["data/sales.csv"])
        assert self._shared(sandbox) == {"data/sales.csv": "a,b\n1,2\n"}

    def test_a_name_outside_the_listing_is_refused_with_a_hint(self):
        """The listing is the injection-pinning boundary: a name the model invented, or read
        out of a file it was given, has nowhere to go."""
        sandbox = _ScriptedSandbox()
        store = InMemoryStore({"data/sales.csv": "x"})
        tool = _tool(_backend(sandbox), file_store=store)

        out = _run(tool, "print('hi')", files=["data/secrets.csv"])
        assert "not in this tool's file listing" in out
        assert "data/sales.csv" in out
        assert sandbox.files == {}

    @pytest.mark.parametrize("name", ["../../etc/passwd", "/etc/passwd", "a/../../b"])
    def test_a_traversing_name_is_refused_without_echoing_the_listing(self, name: str):
        """Echoing it would invite a retry with another spelling."""
        sandbox = _ScriptedSandbox()
        store = InMemoryStore({name: "x", "data/sales.csv": "y"})
        tool = _tool(_backend(sandbox), file_store=store)

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
        tool = _tool(_backend(sandbox), file_store=store)

        out = _run(tool, "print('hi')", files=[name])
        assert reason in out
        assert sandbox.files == {}

    def test_a_traversing_name_under_a_reserved_one_gets_the_validators_sentence(self):
        """`program.py/../x` climbs back out, so nothing is living inside anything and the
        nested-name sentence would be false. Only the validator running first keeps it true."""
        sandbox = _ScriptedSandbox()
        name = f"{_PROGRAM_FILENAME}/../x"
        tool = _tool(_backend(sandbox), file_store=InMemoryStore({name: "x"}))

        out = _run(tool, "print('hi')", files=[name])
        assert "cannot be shared" in out
        assert "nothing can live inside it" not in out, out
        assert sandbox.files == {}

    @pytest.mark.parametrize(
        "nested",
        [f"{_PROGRAM_FILENAME}/data.csv", f"{_PROGRAM_FILENAME}/a/b.csv"],
        ids=["a child of it", "deeper than that"],
    )
    def test_the_refusal_beneath_the_program_name_names_the_program_not_the_nested_name(
        self, nested: str
    ):
        """Backends create parent directories for a nested write, so this would turn
        `program.py` into a directory and the source write that follows would fail on every
        call — at any depth, which is why the rule is a prefix test and not a parent test.

        Asserted whole, because every word of it carries: which of the two names is refused,
        which is called reserved, and `reserves` rather than `writes` — the last being what
        keeps the same sentence true of the manifest, which the guest writes and this tool
        only reads.
        """
        sandbox = _ScriptedSandbox()
        tool = _tool(_backend(sandbox), file_store=InMemoryStore({nested: "x"}))

        out = _run(tool, "print('hi')", files=[nested])
        assert out == (
            f"Error: {nested!r} cannot be shared — {_PROGRAM_FILENAME!r} is a file name this "
            f"tool reserves in every run's directory, so nothing can live inside it."
        ), out
        assert sandbox.contents == {}

    @pytest.mark.parametrize(
        ("name", "sentence", "wrong"),
        [
            (_PROGRAM_FILENAME, "this tool writes a file of that name", "nothing can live inside"),
            (f"{_PROGRAM_FILENAME}/data.csv", "nothing can live inside it", "a file of that name"),
        ],
        ids=["the reserved name", "a name beneath it"],
    )
    def test_a_reserved_name_the_store_lacks_is_refused_as_reserved_not_as_a_listing_miss(
        self, name: str, sentence: str, wrong: str
    ):
        """Two reasons apply and the order decides which one the model reads. A listing miss
        invites a retry once the file is stored, which is a retry neither name can survive.

        The other name's sentence is asserted absent because the two are one branch apart, and
        a rule shared between them reads as plausible from either side.
        """
        sandbox = _ScriptedSandbox()
        tool = _tool(_backend(sandbox), file_store=InMemoryStore({"data/sales.csv": "x"}))

        out = _run(tool, "print('hi')", files=[name])
        assert sentence in out, out
        assert wrong not in out, out
        assert "not in this tool's file listing" not in out, out

    @pytest.mark.parametrize(
        "name",
        [_MANIFEST_FILENAME, f"{_MANIFEST_FILENAME}/r.csv"],
        ids=["the manifest name", "a name beneath it"],
    )
    def test_the_manifest_name_is_only_reserved_in_the_mode_that_reads_it(self, name: str):
        """Nothing reads `outputs.json` outside MANIFEST mode, so refusing it there is
        overreach. The reserved set is built per mode, and both checks have to honour that
        rather than carry their own idea of which names this kind owns."""
        sandbox = _ScriptedSandbox()
        tool = _tool(_backend(sandbox), file_store=InMemoryStore({name: "x"}))

        out = _run(tool, "print('hi')", files=[name])
        assert "cannot be shared" not in out, out
        assert f"{_run_dirs(sandbox)[0]}/{name}" in sandbox.files

    def test_a_name_that_merely_starts_with_the_program_name_is_fine(self):
        """`program.py.bak` shares no directory with it, so refusing it would be overreach."""
        sandbox = _ScriptedSandbox()
        store = InMemoryStore({f"{_PROGRAM_FILENAME}.bak": "x"})
        tool = _tool(_backend(sandbox), file_store=store)

        _run(tool, "print('hi')", files=[f"{_PROGRAM_FILENAME}.bak"])
        run_dir = _run_dirs(sandbox)[0]
        assert f"{run_dir}/{_PROGRAM_FILENAME}.bak" in sandbox.files

    def test_the_program_file_cannot_be_shadowed_by_a_shared_file(self):
        sandbox = _ScriptedSandbox()
        store = InMemoryStore({_PROGRAM_FILENAME: "print('theirs')"})
        tool = _tool(_backend(sandbox), file_store=store)

        out = _run(tool, "print('mine')", files=[_PROGRAM_FILENAME])
        assert "cannot be shared" in out
        assert sandbox.files == {}

    def test_a_file_deleted_between_rounds_does_not_survive_in_the_guest(self):
        """The reason each call gets its own directory: the sandbox is reused, so a stale
        input would otherwise be read by the next program as a live one."""
        sandbox = _ScriptedSandbox()
        store = InMemoryStore({"a.csv": "1", "b.csv": "2"})
        tool = _tool(_backend(sandbox), file_store=store)

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
        tool = _tool(_backend(sandbox), file_store=_ListedButGoneStore("gone.csv"))

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
        return _tool(_backend(sandbox), file_store=store, **kw)

    def test_more_files_than_the_count_allows_are_refused(self):
        sandbox = _ScriptedSandbox()
        store = InMemoryStore({"a": "1", "b": "2", "c": "3"})
        tool = self._tool(sandbox, store, files_in=replace(DEFAULT_TRANSFER_LIMITS, max_files=2))

        out = _run(tool, "print(1)", files=["a", "b", "c"])
        assert "your program and 3 shared" in out
        # Unqualified: with nothing dispatchable that list is everything that would cross.
        assert "writes at most 2 per call" in out
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
            file_store=store,
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

    @pytest.mark.parametrize("where", ["code", "file"])
    def test_content_that_is_not_encodable_is_a_refusal_not_a_dead_turn(self, where: str):
        """A lone surrogate survives JSON and arrives as a `str` that cannot be encoded. The
        tally runs outside the guarded write, so an unhandled `UnicodeEncodeError` here takes
        the caller's turn with it."""
        lone_surrogate = "x\ud800y"
        sandbox = _ScriptedSandbox()
        store = _CountingStore({"a.csv": lone_surrogate if where == "file" else "ok"})
        tool = self._tool(sandbox, store)

        out = _run(
            tool,
            lone_surrogate if where == "code" else "print(1)",
            files=["a.csv"],
        )
        assert "not valid UTF-8" in out
        assert sandbox.contents == {}

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

    @pytest.mark.parametrize(
        ("names", "sentence"),
        [
            (["Program.py", "program.py"], "this tool writes that file itself"),
            (["Program.py/x.csv", "program.py/x.csv"], "nothing can live inside it"),
        ],
        ids=["the reserved name", "a name beneath it"],
    )
    def test_a_case_variant_declared_first_does_not_turn_a_reserved_name_into_a_collision(
        self, names: list[str], sentence: str
    ):
        """The collision key is NFC-lowered, so a case variant is seen first and both reasons
        apply. "One file once saved" invites dropping one spelling, and dropping the wrong one
        re-declares a name that can never be saved — where the reserved refusal is final."""
        sandbox = _ProducingSandbox()
        tool = _pulling_tool(sandbox, CodeactOutputs.DECLARED, _RecordingSink())

        out = _run(tool, "print('hi')", outputs=names)
        assert sentence in out, out
        assert "one file once saved" not in out, out
        assert sandbox.raw_commands == []

    def test_a_traversing_output_under_a_reserved_name_gets_the_validators_sentence(self):
        """`program.py/../x` climbs back out, so nothing is living inside anything and the
        nested-name sentence would be false. Only the validator running first keeps it true."""
        sandbox = _ProducingSandbox()
        tool = _pulling_tool(sandbox, CodeactOutputs.DECLARED, _RecordingSink())

        out = _run(tool, "print('hi')", outputs=[f"{_PROGRAM_FILENAME}/../x"])
        assert "cannot be saved" in out
        assert "nothing can live inside it" not in out, out
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

    def test_the_work_subdirectory_counts_toward_the_ceiling_when_a_run_dispatches(self):
        """A dispatching run keeps the model's files one level deeper, so the prefix a declared
        name is judged against is `<run>/work/` — five bytes more than `<run>/`.

        242 is the longest name the flat layout accepts, and it is over the ceiling as soon as
        those five are counted. Both halves are asserted because only the pair discriminates:
        judging against the run id alone would let this through and have `collect_outputs`
        refuse the guest path a whole run later, which is the failure the up-front check exists
        to prevent.
        """
        name = "a" * (MAX_ARTIFACT_NAME_BYTES - 13)

        flat_sink = _RecordingSink()
        flat_tool, flat_sandbox = _neighbouring(
            False, **_landing(CodeactOutputs.DECLARED, flat_sink)
        )
        flat = _run_producing(flat_tool, flat_sandbox, {name: b"x"}, outputs=[name])

        armed_sink = _RecordingSink()
        armed_tool, armed_sandbox = _neighbouring(
            True, **_landing(CodeactOutputs.DECLARED, armed_sink)
        )
        armed = _run_producing(armed_tool, armed_sandbox, {name: b"x"}, outputs=[name])

        assert flat_sink.names == [name], f"the flat layout should still accept it: {flat}"
        assert "over the 255-byte ceiling" in armed, armed
        assert armed_sink.names == []
        assert armed_sandbox.raw_commands == [], "it was refused after the program ran"

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

        out = _run(tool, "print('hi')", outputs=[_PROGRAM_FILENAME])
        assert "cannot be saved" in out
        assert "this tool writes that file itself" in out, out

    @pytest.mark.parametrize(
        "name",
        [_MANIFEST_FILENAME, f"{_MANIFEST_FILENAME}/r.csv"],
        ids=["the manifest name", "a name beneath it"],
    )
    def test_the_manifest_name_may_be_declared_in_the_mode_that_never_reads_it(self, name: str):
        """DECLARED mode writes no manifest and reads none, so `outputs.json` is an ordinary
        name here — the per-mode set says so and both checks have to read it from there."""
        sandbox = _ProducingSandbox()
        sink = _RecordingSink()
        tool = _pulling_tool(sandbox, CodeactOutputs.DECLARED, sink)

        out = _run_producing(tool, sandbox, {name: b"a,b\n"}, outputs=[name])
        assert "cannot be saved" not in out, out
        assert sink.names == [name]

    @pytest.mark.parametrize(
        "nested",
        [f"{_PROGRAM_FILENAME}/report.csv", f"{_PROGRAM_FILENAME}/a/report.csv"],
        ids=["a child of it", "deeper than that"],
    )
    def test_a_declared_output_beneath_the_program_name_names_the_program(self, nested: str):
        """This tool writes `program.py`, so telling a model it writes `program.py/report.csv`
        names a file it does not write. The verb is asserted too: this refusal is a save, and
        the sentence is built from an argument a call site can hand the wrong word."""
        sandbox = _ProducingSandbox()
        sink = _RecordingSink()
        tool = _pulling_tool(sandbox, CodeactOutputs.DECLARED, sink)

        out = _run(tool, "print('hi')", outputs=[nested])
        assert f"Error: {nested!r} cannot be saved" in out, out
        assert f"{_PROGRAM_FILENAME!r} is a file name this tool reserves" in out, out
        assert "writes that file itself" not in out, out
        assert "nothing can live inside it" in out, out
        assert sandbox.raw_commands == []
        assert sink.names == []

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

    def test_a_sink_that_breaks_part_way_says_some_files_may_already_be_saved(self):
        """`collect_outputs` cannot un-deliver, so "could not be saved" alone invites a retry
        on the assumption that nothing landed."""
        sandbox = _ProducingSandbox()
        sink = _FailingSink()
        tool = _pulling_tool(sandbox, CodeactOutputs.DECLARED, sink)

        out = _run_producing(
            tool, sandbox, {"a.csv": b"1", "b.csv": b"2"}, outputs=["a.csv", "b.csv"]
        )
        assert "may already have been saved" in out
        assert sink.names == ["a.csv"]

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

    def test_the_manifest_is_read_from_the_work_directory_when_a_run_dispatches(self):
        """`_read_manifest` stats a path built from the same prefix everything else uses, so a
        dispatching run must look in `work/` rather than in the run directory.

        MANIFEST is the one output mode whose names arrive after the program has run, and the
        stat that fetches them is the only place the prefix is used for a read. Get it wrong
        and the run answers "no outputs.json was written" — an empty collection reported as
        success, after a program that produced everything it promised.
        """
        sandbox = _FinishingSandbox()
        sink = _RecordingSink()
        tool = _tool(
            _backend(sandbox, capabilities=_DISPATCHES),
            host_tools=_registry(_round_half_up),
            **_landing(CodeactOutputs.MANIFEST, sink),
        )

        out = _run_producing(
            tool,
            sandbox,
            {
                _MANIFEST_FILENAME: b'{"outputs": [{"path": "r.csv"}]}',
                "r.csv": b"1,2",
            },
        )

        (run_dir,) = _run_dirs(sandbox)
        assert f"{run_dir}/{WORK_DIRECTORY}/{_MANIFEST_FILENAME}" in sandbox.contents
        assert sink.names == ["r.csv"], out
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

    def test_a_manifest_path_beneath_the_manifest_name_names_the_manifest(self):
        """The manifest is a reserved name in this mode, and a path listed beneath it is
        refused for the same reason a nested input is — so the refusal has to name
        `outputs.json` rather than the path under it, which nothing writes."""
        sandbox = _ProducingSandbox()
        sink = _RecordingSink()
        tool = _pulling_tool(sandbox, CodeactOutputs.MANIFEST, sink)
        nested = f"{_MANIFEST_FILENAME}/r.csv"

        out = _run_producing(
            tool, sandbox, {_MANIFEST_FILENAME: f'{{"outputs": [{{"path": "{nested}"}}]}}'.encode()}
        )
        assert f"Error: {nested!r} cannot be saved" in out, out
        # `reserves`, not `writes`: the guest writes the manifest and this tool only reads it.
        assert f"{_MANIFEST_FILENAME!r} is a file name this tool reserves" in out, out
        assert "writes that file itself" not in out, out
        assert "nothing can live inside it" in out, out
        assert sink.names == []

    @pytest.mark.parametrize(
        ("name", "sentence"),
        [
            (_MANIFEST_FILENAME, "this tool writes a file of that name"),
            (f"{_MANIFEST_FILENAME}/r.csv", "nothing can live inside it"),
        ],
        ids=["the manifest name", "a name beneath it"],
    )
    def test_the_manifest_name_is_reserved_against_shared_files_too(self, name: str, sentence: str):
        """A store and this mode can be wired together, and then a shared `outputs.json` lands
        exactly where the manifest is read from — handing the collection to a file the guest
        never wrote. The name is reserved on the way in as well as on the way out."""
        sandbox = _ProducingSandbox()
        tool = _pulling_tool(
            sandbox,
            CodeactOutputs.MANIFEST,
            _RecordingSink(),
            file_store=InMemoryStore({name: "x"}),
        )

        out = _run(tool, "print('hi')", files=[name])
        assert "cannot be shared" in out, out
        assert sentence in out, out
        assert sandbox.contents == {}

    def test_a_manifest_over_the_file_cap_lands_nothing(self):
        """`max_files=2` leaves room for the manifest and one artifact, so listing two is over."""
        sandbox = _ProducingSandbox()
        sink = _RecordingSink()
        tool = _pulling_tool(
            sandbox,
            CodeactOutputs.MANIFEST,
            sink,
            files_out=replace(DEFAULT_TRANSFER_LIMITS, max_files=2),
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

    def test_the_manifest_is_counted_against_the_collection_it_describes(self):
        """It is a file this collection moved, so `files_out` counts it — `CONSUME`, because
        the kind read it itself and it must never reach the sink."""
        sandbox = _ProducingSandbox()
        sink = _RecordingSink()
        tool = _pulling_tool(
            sandbox,
            CodeactOutputs.MANIFEST,
            sink,
            files_out=replace(DEFAULT_TRANSFER_LIMITS, max_files=2),
        )

        out = _run_producing(
            tool,
            sandbox,
            {_MANIFEST_FILENAME: b'{"outputs": [{"path": "a"}]}', "a": b"1"},
        )
        assert sink.names == ["a"]
        assert _MANIFEST_FILENAME not in " ".join(sink.names)
        assert "saved a" in out

    @pytest.mark.parametrize(
        ("mode", "kwargs", "match"),
        [
            (CodeactOutputs.NONE, {"files_in": 0}, "no call could succeed"),
            (CodeactOutputs.DECLARED, {"files_out": 0}, "refuse every non-empty use"),
            (CodeactOutputs.MANIFEST, {"files_out": 1}, "at least 2"),
        ],
    )
    def test_a_cap_no_call_could_satisfy_is_refused_at_attach(self, mode, kwargs, match):
        """A tool the model can see and can never use successfully is worse than one that
        never attached: `program.py` is always one inbound file, and an `outputs` parameter
        with nowhere to put an output advertises a channel that refuses every use."""
        caps = {k: replace(DEFAULT_TRANSFER_LIMITS, max_files=v) for k, v in kwargs.items()}
        with pytest.raises(ValueError, match=match):
            _tool(
                _backend(capabilities=_PULLS),
                outputs=mode,
                output_sink=_RecordingSink().sink if mode is not CodeactOutputs.NONE else None,
                **caps,
            )

    def test_a_host_cap_with_no_room_for_an_artifact_is_refused_at_attach(self):
        """One slot means the manifest fills it and the channel could never deliver."""
        with pytest.raises(ValueError, match="at least 2"):
            _pulling_tool(
                _ProducingSandbox(),
                CodeactOutputs.MANIFEST,
                _RecordingSink(),
                files_out=replace(DEFAULT_TRANSFER_LIMITS, max_files=1),
            )

    def test_the_manifest_is_charged_the_bytes_that_were_read_not_a_second_stat(self):
        """A guest can still be running after `exec`. If the manifest is truncated between the
        read and a re-stat, an accounting that trusts the stat hands its cost back to the
        budget after its bytes have already crossed."""
        sandbox = _ShrinkingManifestSandbox()
        sink = _RecordingSink()
        manifest = b'{"outputs":[{"path":"a"}]}' + b" " * 40
        tool = _pulling_tool(
            sandbox,
            CodeactOutputs.MANIFEST,
            sink,
            files_out=replace(DEFAULT_TRANSFER_LIMITS, max_total_bytes=len(manifest) + 5),
        )

        out = _run_producing(tool, sandbox, {_MANIFEST_FILENAME: manifest, "a": b"0123456789"})
        assert "could not be saved" in out
        assert sink.names == []

    def test_an_artifact_that_fits_beside_the_manifest_still_lands(self):
        """The other side of the budget, so charging the manifest cannot refuse everything."""
        sandbox = _ProducingSandbox()
        sink = _RecordingSink()
        manifest = b'{"outputs":[{"path":"a"}]}'
        tool = _pulling_tool(
            sandbox,
            CodeactOutputs.MANIFEST,
            sink,
            files_out=replace(DEFAULT_TRANSFER_LIMITS, max_total_bytes=len(manifest) + 10),
        )

        _run_producing(tool, sandbox, {_MANIFEST_FILENAME: manifest, "a": b"12345"})
        assert sink.names == ["a"]

    @pytest.mark.parametrize("cap", ["max_bytes_per_file", "max_total_bytes"])
    def test_a_byte_cap_below_the_smallest_manifest_is_refused_at_attach(self, cap: str):
        """26 bytes is the shortest manifest naming one file, so a lower ceiling exposes a
        channel whose every call `_read_manifest` would refuse."""
        with pytest.raises(ValueError, match="bytes of files_out"):
            _pulling_tool(
                _ProducingSandbox(),
                CodeactOutputs.MANIFEST,
                _RecordingSink(),
                files_out=replace(DEFAULT_TRANSFER_LIMITS, **{cap: _SMALLEST_MANIFEST - 1}),
            )

    def test_exactly_the_smallest_manifest_is_a_usable_channel_and_attaches(self):
        """Equality leaves nothing for the artifact's *bytes*, and a zero-byte file is still a
        file — so refusing this configuration would refuse one that works."""
        sandbox = _ProducingSandbox()
        sink = _RecordingSink()
        tool = _pulling_tool(
            sandbox,
            CodeactOutputs.MANIFEST,
            sink,
            files_out=replace(DEFAULT_TRANSFER_LIMITS, max_total_bytes=_SMALLEST_MANIFEST),
        )

        _run_producing(tool, sandbox, {_MANIFEST_FILENAME: b'{"outputs":[{"path":"a"}]}', "a": b""})
        assert sink.names == ["a"]

    def test_the_manifest_read_is_bounded_by_the_collection_total_too(self):
        """A manifest bigger than the whole collection's budget cannot be part of a collection
        that fits, so the per-file ceiling alone is the wrong bound when the total is smaller."""
        sandbox = _StatOnlySandbox(size_bytes=2048)
        sink = _RecordingSink()
        tool = _pulling_tool(
            sandbox,
            CodeactOutputs.MANIFEST,
            sink,
            files_out=replace(DEFAULT_TRANSFER_LIMITS, max_total_bytes=1024, max_files=2),
        )

        out = _run(tool, "print('hi')")
        assert "reads at most 1024" in out
        assert sandbox.reads == []

    def test_the_manifest_read_is_bounded_by_the_hosts_own_ceiling(self):
        """`files_out` is what the router matched against the backend, so reading past it would
        transfer more than the spec declared and make that match untrue for this kind."""
        sandbox = _StatOnlySandbox(size_bytes=2048)
        sink = _RecordingSink()
        tool = _pulling_tool(
            sandbox,
            CodeactOutputs.MANIFEST,
            sink,
            files_out=replace(DEFAULT_TRANSFER_LIMITS, max_bytes_per_file=1024, max_files=2),
        )

        out = _run(tool, "print('hi')")
        assert "reads at most 1024" in out
        assert sandbox.reads == []

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
# Host tools — the program calls out, and the host answers over the run's own files
# ---------------------------------------------------------------------------


def _dispatching(sandbox: InProcessSandbox, *tools: Callable[..., Any], **kw: Any):
    """The tool for a registry serving `tools`, over a sandbox that can serve the transport."""
    return _tool(_backend(sandbox, capabilities=_DISPATCHES), host_tools=_registry(*tools), **kw)


class TestAProgramThatCallsOut:
    def test_the_registry_answers_and_the_program_reads_what_it_said(self):
        """End to end over the transport: the request the guest wrote reaches the registered
        function, its arguments arrive, and its return value is what the program prints."""
        sandbox = _CallingSandbox("_round_half_up", {"value": 3.6})
        out = _run(_dispatching(sandbox, _round_half_up), "print(_round_half_up(value=3.6))")

        assert sandbox.answers == [{"value": 4}]
        assert out == "stdout:\nthe host said 4"

    def test_each_call_is_served_under_its_own_run_directory(self):
        """`acquire` is get-or-create, so a second call must not find the first one's requests,
        its answers or its exit marker sitting where the supervisor polls."""
        sandbox = _CallingSandbox("_round_half_up", {"value": 0.5})
        tool = _dispatching(sandbox, _round_half_up)
        _run(tool, "print(1)")
        _run(tool, "print(2)")

        first, second = sandbox.layouts
        assert first.directory != second.directory
        assert [first.directory, second.directory] == _run_dirs(sandbox)
        assert len(sandbox.answers) == 2

    def test_the_dispatch_cap_bounds_one_call_rather_than_the_conversation(self):
        """The cap bounds what one program may cost, so a run that spends it all must leave the
        next call as much — not retire `execute_code` for the rest of the conversation."""
        sandbox = _CallingSandbox("_round_half_up", {"value": 3.6})
        registry = _registry(_round_half_up, max_dispatches_per_run=1)
        tool = _tool(_backend(sandbox, capabilities=_DISPATCHES), host_tools=registry)
        _run(tool, "print(1)")
        _run(tool, "print(2)")

        assert sandbox.answers == [{"value": 4}, {"value": 4}]

    def test_the_program_is_written_where_the_launcher_goes_looking_for_it(self):
        """Write the program only to ``layout.program``, where the launcher executes it.

        The two negative assertions are why this is `only`: a copy left in the run directory
        or in `work/` would satisfy the first assertion while still putting the program where
        a model's files are.
        """
        sandbox = _CallingSandbox("_round_half_up", {"value": 0.5})
        _run(_dispatching(sandbox, _round_half_up), "print('hi')")

        (layout,) = sandbox.layouts
        assert sandbox.files.get(layout.program) == "print('hi')", sorted(sandbox.files)
        assert f"{layout.directory}/{_PROGRAM_FILENAME}" not in sandbox.files
        assert f"{layout.work}/{_PROGRAM_FILENAME}" not in sandbox.files

    def test_the_shim_is_written_beside_the_program_with_the_runs_own_patience(self):
        """A guest that gives up before the supervisor does is wrong twice over: the dispatch
        it asked for goes on to act while the program has been told nobody answered."""
        sandbox = _CallingSandbox("_round_half_up", {"value": 0.5})
        _run(_dispatching(sandbox, _round_half_up, exec_timeout_seconds=97), "print(1)")

        (layout,) = sandbox.layouts
        assert sandbox.files[layout.shim] == host_tool_shim(
            frozenset({"_round_half_up"}), call_timeout=97
        )

    def test_both_paths_run_the_program_under_this_kinds_own_interpreter(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """The transport carries a default of its own, so a dispatching run that leaves it out
        is running under a constant this kind does not own and cannot change."""
        monkeypatch.setattr("maf_sandbox_codeact._tool._INTERPRETER", "pypy3")

        plain = _ScriptedSandbox()
        _run(_tool(_backend(plain)), "print(1)")
        assert plain.commands[0][0].startswith("pypy3 ")

        sandbox = _CallingSandbox("_round_half_up", {"value": 0.5})
        _run(_dispatching(sandbox, _round_half_up), "print(1)")

        (layout,) = sandbox.layouts
        assert "pypy3" in sandbox.files[layout.launcher]
        assert "python3" not in sandbox.files[layout.launcher]


class _StallingSandbox(_ScriptedSandbox):
    """A dispatch-served guest that prints and then never records an exit marker.

    What a wedged program looks like from the supervisor's side: the launcher returns, output
    accumulates, and the marker the run is waiting for never lands.
    """

    def __init__(self, printed: bytes = b"step 1 done", **kwargs) -> None:
        super().__init__(**kwargs)
        self.printed = printed

    async def exec(self, command, *, working_directory, timeout):
        result = await super().exec(command, working_directory=working_directory, timeout=timeout)
        layout = guest_run_layout(working_directory, program=_PROGRAM_FILENAME)
        self.contents[layout.output] = self.printed
        return result


class _SlowToTakeTheLauncherSandbox(_ScriptedSandbox):
    """A guest whose launcher upload outlives the run's whole bound.

    The transport gives up before `exec` is ever reached, so the program is never started —
    the one `SandboxProgramTimeout` that is not the program overrunning.
    """

    #: Longer than the one second the test gives the run, so the deadline `_within` holds the
    #: upload to is already gone when it returns. The factory refuses a non-positive
    #: `exec_timeout_seconds` on a dispatching tool, so one second is the shortest bound that
    #: reaches this path at all, and this has to outlast it.
    _SLOWER_THAN_THE_RUN = 1.2

    async def write_file(self, path: str, content: str | bytes) -> None:
        await super().write_file(path, content)
        if path.endswith(".sh"):
            await asyncio.sleep(self._SLOWER_THAN_THE_RUN)


class _StatTimingOutSandbox(_StallingSandbox):
    """A backend that bounds its own control-plane calls, as the shipped Docker one does.

    Its `stat_file` raises the client's own `TimeoutError` — not this run's, which has almost
    all of its time left. `_within` re-raises a backend's own untranslated on purpose.
    """

    async def stat_file(self, path, *, working_directory):
        raise TimeoutError("docker cp: context deadline exceeded")


class TestATimeoutSaysWhoseItWas:
    """`TimeoutError` means two unrelated things on the dispatch path, and only one of them is
    the program running out. Collapsing them tells the model to rewrite code that was fine."""

    def test_a_backends_own_timeout_is_not_blamed_on_the_program(self):
        sandbox = _StatTimingOutSandbox()
        out = _run(_dispatching(sandbox, _round_half_up, exec_timeout_seconds=600), "print('hi')")

        assert "timed out" not in out, "a stat that ran out was reported as the program's bound"
        assert out == "Error: could not run the program in the sandbox"
        assert "docker cp" not in out, "the backend's own sentence reached the transcript"

    def test_a_program_that_runs_out_is_quoted_as_far_as_it_got(self):
        """The transport's own sentence is surfaced rather than rebuilt, so the wording is
        `did not finish within` rather than this kind's older `timed out after`.

        Rebuilding it from `SandboxProgramTimeout.output` alone loses the case below and the
        host's reason for having read no output, both of which live only in the message.
        """
        sandbox = _StallingSandbox(printed=b"step 1 done\nstep 2 done")
        out = _run(_dispatching(sandbox, _round_half_up, exec_timeout_seconds=1), "print('x')")

        assert "did not finish within 1s" in out, out
        assert "step 2 done" in out, "the partial output the transport paid to read was dropped"

    def test_a_run_that_expires_before_the_program_starts_does_not_blame_the_program(self):
        """`SandboxProgramTimeout` covers the launcher upload too, where nothing ran.

        Telling a model its program timed out sends it rewriting code that never executed, and
        the distinction exists nowhere but the transport's message — the exception type is the
        same and `output` is empty either way.
        """
        sandbox = _SlowToTakeTheLauncherSandbox()
        out = _run(_dispatching(sandbox, _round_half_up, exec_timeout_seconds=1), "print('x')")

        assert "before the program was started" in out, out
        assert "did not finish" not in out, "a run that never started was reported as overrunning"

    def test_the_plain_path_still_reads_a_timeout_as_the_programs_own(self):
        """No dispatch, one `exec`, one bound: the equation this class complicates holds here."""
        sandbox = _ScriptedSandbox(raises=TimeoutError())
        out = _run(_tool(_backend(sandbox), exec_timeout_seconds=7), "print('hi')")

        assert out == "Error: the program timed out after 7s"


class TestOnlyAnAttachedToolSealsTheRegistry:
    def test_an_unconfigured_hosts_registry_can_still_be_widened(self):
        """Nothing is grounded on a host with no sandbox — no spec, no classification — so a
        later `register` has nothing to contradict and must not be refused as if it had."""
        registry = _registry(_round_half_up)
        assert make_codeact_tools(None, "data-analyst", _context(), host_tools=registry) == []

        registry.register(_exchange_rate)
        assert registry.names() == frozenset({"_round_half_up", "_exchange_rate"})


#: Every name the transport writes into a dispatching run, plus one nested beneath each of the
#: two that are files — the nested rule reads the same set as the exact one, so both have to
#: fall silent here. One list for both directions: two lists drift, and the half that stops
#: being checked is the half nobody looks at.
_TRANSPORT_NAMES = [
    SHIM_MODULE,
    f"{SHIM_MODULE.removesuffix('.py')}/__init__.py",
    f"{SHIM_MODULE.removesuffix('.py')}.so",
    f"{SHIM_MODULE}/part.csv",
    "program_output.txt",
    "program_exit_code",
    "run_program.sh",
    _PROGRAM_FILENAME,
    f"{_PROGRAM_FILENAME}/data.csv",
]


class TestWhatTheTwoDirectoriesMakeHarmless:
    """A run that dispatches puts the transport's files in `host_tools/` and the model's in
    `work/`, so a name that would collide is written instead of refused.

    Two directories are the guarantee, so what pins it is that these names land — not that
    some list still enumerates them.
    """

    @pytest.mark.parametrize("name", _TRANSPORT_NAMES)
    def test_a_shared_file_may_take_a_name_the_transport_uses(self, name: str):
        sandbox = _FinishingSandbox()
        tool = _tool(
            _backend(sandbox, capabilities=_DISPATCHES),
            host_tools=_registry(_round_half_up),
            file_store=InMemoryStore({name: "x"}),
            exec_timeout_seconds=5,
        )

        out = _run(tool, "print(1)", files=[name])

        assert "cannot be shared" not in out, out
        (run_dir,) = _run_dirs(sandbox)
        assert f"{run_dir}/{WORK_DIRECTORY}/{name}" in sandbox.files, sorted(sandbox.files)

    @pytest.mark.parametrize("name", _TRANSPORT_NAMES)
    def test_a_declared_output_may_take_a_name_the_transport_uses(self, name: str):
        """The outbound half of the same guarantee: outputs are collected from `work/`, and
        the transport's own copies are not in it."""
        sink = _RecordingSink()
        tool, sandbox = _neighbouring(True, **_landing(CodeactOutputs.DECLARED, sink))

        out = _run_producing(tool, sandbox, {name: b"a,b\n"}, outputs=[name])
        assert "cannot be saved" not in out, out
        assert sink.names == [name]

    def test_nothing_this_tool_writes_for_itself_lands_where_the_model_writes(self):
        """The guarantee behind the case above: what the transport owns and what a model can
        name are two directories, so there is no name to get wrong.

        Asserted against the paths the *tool* wrote, not against the layout — `sandbox.layouts`
        is built by the fake out of `guest_run_layout`, so asserting on it would restate a
        `maf_sandbox` property and hold whatever this kind did with it.
        """
        sandbox = _CallingSandbox("_round_half_up", {"value": 0.5})
        tool = _tool(
            _backend(sandbox, capabilities=_DISPATCHES),
            host_tools=_registry(_round_half_up),
            file_store=InMemoryStore({"data.csv": "a,b\n"}),
        )
        _run(tool, "print(1)", files=["data.csv"])

        (layout,) = sandbox.layouts
        written = set(sandbox.files) | set(sandbox.contents)
        under_work = {p for p in written if p.startswith(f"{layout.work}/")}

        assert under_work == {f"{layout.work}/data.csv"}, (
            f"the only thing in the model's directory should be the file it named: {under_work}"
        )
        assert {layout.program, layout.shim} <= written, "the transport's own files were written"
        assert not {layout.program, layout.shim} & under_work


class TestADegenerateRunBoundIsRefusedInThisKindsVoice:
    """The shim carries the run's bound as the guest's own patience, so a bound no run could
    have is settled at the factory — under the parameter the caller passed."""

    @pytest.mark.parametrize("seconds", [0, -1])
    def test_a_registry_makes_a_non_positive_bound_a_factory_refusal(self, seconds: int):
        with pytest.raises(ValueError) as refused:
            _dispatching_tool(_registry(_round_half_up), exec_timeout_seconds=seconds)

        assert str(refused.value).startswith(f"{EXECUTE_CODE_TOOL_NAME}: exec_timeout_seconds")
        # `call_timeout` is the shim generator's parameter, which this factory does not expose.
        assert "call_timeout" not in str(refused.value)

    def test_a_host_with_nothing_dispatchable_keeps_tolerating_one(self):
        """With no shim to generate the number only ever reaches `exec`, and this factory has
        never had an opinion about it."""
        _tool(_backend(), exec_timeout_seconds=0)
        _dispatching_tool(_registry(), exec_timeout_seconds=0)


def _neighbouring(dispatch: bool, **kw: Any):
    """The tool and its sandbox for the tests below, dispatching or not.

    Both halves run because the two put the model's files in different places — the run
    directory flat, or its `work` subdirectory — while the rule under test is the same one.
    """
    sandbox = _FinishingSandbox() if dispatch else _ProducingSandbox()
    tool = _tool(
        _backend(sandbox, capabilities=_DISPATCHES if dispatch else _PULLS),
        exec_timeout_seconds=1,
        **({"host_tools": _registry(_round_half_up)} if dispatch else {}),
        **kw,
    )
    return tool, sandbox


def _model_dir(sandbox: InProcessSandbox, dispatch: bool) -> str:
    """Where this run put the files a model named: the work subdirectory, or the run directory.

    Derived from `dispatch` rather than from what the sandbox happens to contain, so a
    regression that writes them to the wrong directory fails here instead of being read back
    from wherever it wrote them.
    """
    run_dir = _run_dirs(sandbox)[0]
    return f"{run_dir}/{WORK_DIRECTORY}" if dispatch else run_dir


@pytest.mark.parametrize("dispatch", [False, True], ids=["no registry", "dispatch armed"])
class TestANeighbourOfTheProgramsNameIsNotTheProgram:
    """`program.py` is exec'd by path, so neither a `program/` directory nor a `program.*`
    sibling displaces it, and both are names the `files` channel documents. The reserved-name
    rule is a prefix test, which is the shape that over-reaches onto these if written
    carelessly."""

    def test_a_sibling_of_the_programs_name_is_shared(self, dispatch: bool):
        tool, sandbox = _neighbouring(dispatch, file_store=InMemoryStore({"program.csv": "a,b\n"}))

        out = _run(tool, "print('hi')", files=["program.csv"])
        assert "cannot be shared" not in out
        assert f"{_model_dir(sandbox, dispatch)}/program.csv" in sandbox.files

    def test_a_nested_input_under_the_programs_name_is_shared(self, dispatch: bool):
        store = InMemoryStore({"program/train.py": "x = 1\n"})
        tool, sandbox = _neighbouring(dispatch, file_store=store)

        out = _run(tool, "print('hi')", files=["program/train.py"])
        assert "cannot be shared" not in out
        assert f"{_model_dir(sandbox, dispatch)}/program/train.py" in sandbox.files

    @pytest.mark.parametrize("name", ["Program.py", "Program.py/x.csv"])
    def test_a_case_variant_of_the_programs_name_is_shared(self, dispatch: bool, name: str):
        """The guest filesystem is POSIX, where `Program.py` and `program.py` are two files, so
        the rule matches exactly. Case-folding either comparison refuses a legal name."""
        tool, sandbox = _neighbouring(dispatch, file_store=InMemoryStore({name: "x"}))

        out = _run(tool, "print('hi')", files=[name])
        assert "cannot be shared" not in out, out
        assert f"{_model_dir(sandbox, dispatch)}/{name}" in sandbox.files

    @pytest.mark.parametrize("name", ["Program.py", "Program.py/x.csv"])
    def test_a_case_variant_of_the_programs_name_is_saved(self, dispatch: bool, name: str):
        sink = _RecordingSink()
        tool, sandbox = _neighbouring(dispatch, **_landing(CodeactOutputs.DECLARED, sink))

        out = _run_producing(tool, sandbox, {name: b"a,b\n"}, outputs=[name])
        assert sink.names == [name]
        assert "cannot be saved" not in out

    def test_the_programs_name_deeper_in_a_path_is_shared(self, dispatch: bool):
        """The rule is about the first segment. `data/program.py/notes.txt` displaces nothing,
        and a containment test written in place of the prefix test refuses it."""
        store = InMemoryStore({"data/program.py/notes.txt": "x"})
        tool, sandbox = _neighbouring(dispatch, file_store=store)

        out = _run(tool, "print('hi')", files=["data/program.py/notes.txt"])
        assert "cannot be shared" not in out, out
        assert f"{_model_dir(sandbox, dispatch)}/data/program.py/notes.txt" in sandbox.files

    def test_a_nested_output_under_it_is_saved(self, dispatch: bool):
        sink = _RecordingSink()
        tool, sandbox = _neighbouring(dispatch, **_landing(CodeactOutputs.DECLARED, sink))

        out = _run_producing(
            tool, sandbox, {"program/report.csv": b"a,b\n"}, outputs=["program/report.csv"]
        )
        assert sink.names == ["program/report.csv"]
        assert "cannot be saved" not in out


class TestTheShimIsAnInboundFileToo:
    """It crosses on every call a registry is wired for, so it is counted like the program."""

    def test_it_counts_against_the_inbound_file_count(self):
        limits = replace(DEFAULT_TRANSFER_LIMITS, max_files=2)
        store = InMemoryStore({"a.csv": "1"})

        plain = _ScriptedSandbox()
        _run(_tool(_backend(plain), file_store=store, files_in=limits), "print(1)", files=["a.csv"])
        assert f"{_run_dirs(plain)[0]}/a.csv" in plain.files

        sandbox = _ScriptedSandbox()
        tool = _tool(
            _backend(sandbox, capabilities=_DISPATCHES),
            host_tools=_registry(_round_half_up),
            file_store=store,
            files_in=limits,
        )
        out = _run(tool, "print(1)", files=["a.csv"])
        assert "3 files would be written" in out
        assert "your program, the host-tool module beside it, and 1 shared" in out
        # Qualified here and nowhere else: the launcher crosses too and is not in that list.
        assert "writes at most 2 of those per call" in out
        assert sandbox.contents == {}

    def test_it_counts_against_the_inbound_byte_ceilings(self):
        """Kilobytes of generated source, against a total with room for the module alone and
        not for the program beside it."""
        module = len(host_tool_shim(frozenset({"_round_half_up"}), call_timeout=97).encode())
        limits = replace(DEFAULT_TRANSFER_LIMITS, max_total_bytes=module + 5)

        plain = _ScriptedSandbox()
        _run(_tool(_backend(plain), files_in=limits), "print(1)")
        assert plain.files, "the same program did not fit without a registry"

        sandbox = _ScriptedSandbox()
        tool = _tool(
            _backend(sandbox, capabilities=_DISPATCHES),
            host_tools=_registry(_round_half_up),
            files_in=limits,
            exec_timeout_seconds=97,
        )
        assert f"at most {module + 5} per call" in _run(tool, "print(1)")
        assert sandbox.contents == {}

    def test_room_for_one_inbound_file_is_refused_at_the_factory(self):
        """Two files cross on every call, so a cap of one could never serve a single call — and
        a tool the model can see and never use successfully is worse than one that never
        attached."""
        with pytest.raises(ValueError, match="dispatch module"):
            _dispatching_tool(
                _registry(_round_half_up), files_in=replace(DEFAULT_TRANSFER_LIMITS, max_files=1)
            )

    @pytest.mark.parametrize("cap", ["max_bytes_per_file", "max_total_bytes"])
    def test_a_byte_cap_below_the_module_is_refused_at_the_factory_too(self, cap: str):
        """Its size is settled before anything attaches, so a ceiling under it is the same
        never-usable tool the count check refuses — reached by the other leg."""
        module = len(host_tool_shim(frozenset({"_round_half_up"}), call_timeout=97).encode())
        with pytest.raises(ValueError, match="dispatch module is"):
            _dispatching_tool(
                _registry(_round_half_up),
                files_in=replace(DEFAULT_TRANSFER_LIMITS, **{cap: module - 1}),
                exec_timeout_seconds=97,
            )

    def test_a_registry_holding_nothing_is_refused_nothing(self):
        """Nothing dispatchable is no shim, exactly as it is no capability in the spec."""
        _dispatching_tool(_registry(), files_in=replace(DEFAULT_TRANSFER_LIMITS, max_files=1))


class TestWithoutARegistry:
    def test_the_program_is_exec_ed_and_nothing_reaches_the_transport(self):
        """The stdout-only kind is what it always was: an argv sequence handed to `exec`, and
        no launcher, no shim and no directory of requests nobody would serve."""
        sandbox = _ScriptedSandbox()
        _run(_tool(_backend(sandbox)), "print('hi')")

        (run_dir,) = _run_dirs(sandbox)
        (argv,) = sandbox.raw_commands
        assert not isinstance(argv, str)
        assert list(argv) == ["python3", f"{run_dir}/{_PROGRAM_FILENAME}"]
        assert set(sandbox.contents) == {f"{run_dir}/{_PROGRAM_FILENAME}"}


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
