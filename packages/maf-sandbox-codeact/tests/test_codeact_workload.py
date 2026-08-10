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
from collections.abc import Sequence

import pytest
from maf_sandbox import (
    Capability,
    ExecResult,
    Isolation,
    SandboxCapabilityNotSupported,
    SandboxRouter,
    WorkspaceContext,
)
from maf_sandbox.testing import InProcessSandbox, InProcessSandboxBackend

from maf_sandbox_codeact import (
    CODEACT_KIND,
    EXECUTE_CODE_TOOL_NAME,
    codeact_sandbox_spec,
    make_codeact_tools,
)
from maf_sandbox_codeact._tool import _PROGRAM_FILENAME, _WORK_DIR

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


async def _no_listing(_store: object) -> list[str]:
    """This kind shares no workspace files, so nothing ever enumerates one."""
    return []


def _context(*, thread_id: str | None = "thread-1") -> WorkspaceContext:
    return WorkspaceContext(
        current_scope=lambda: "scope-a",
        current_thread_id=lambda: thread_id,
        list_files=_no_listing,
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


def _callable(tool):
    """The tool body, off whichever attribute the MAF decorator carries it on."""
    return getattr(tool, "func", None) or getattr(tool, "__wrapped__", None) or tool


def _run(tool, code: str) -> str:
    return asyncio.run(_callable(tool)(code=code))


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


class TestTheProgramIsWrittenThenRun:
    def test_the_program_is_written_to_the_work_dir(self):
        sandbox = _ScriptedSandbox()
        _run(_tool(_backend(sandbox)), "print('hi')")

        assert sandbox.files == {f"{_WORK_DIR}/{_PROGRAM_FILENAME}": "print('hi')"}

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
        assert list(argv) == ["python3", f"{_WORK_DIR}/{_PROGRAM_FILENAME}"]

    def test_the_command_never_carries_the_model_written_source(self):
        code = "import os; os.system('id'); print('$(whoami)`id`; rm -rf /')"
        sandbox = _ScriptedSandbox()
        _run(_tool(_backend(sandbox)), code)

        argv = sandbox.raw_commands[0]
        assert all(part == "python3" or part.endswith(_PROGRAM_FILENAME) for part in argv)
        assert sandbox.files[f"{_WORK_DIR}/{_PROGRAM_FILENAME}"] == code

    def test_the_program_runs_in_the_work_dir(self):
        sandbox = _ScriptedSandbox()
        _run(_tool(_backend(sandbox)), "print('hi')")

        _, working_directory, _ = sandbox.commands[0]
        assert working_directory == _WORK_DIR

    def test_the_exec_timeout_is_passed_through(self):
        sandbox = _ScriptedSandbox()
        _run(_tool(_backend(sandbox), exec_timeout_seconds=7), "print('hi')")

        _, _, timeout = sandbox.commands[0]
        assert timeout == 7

    def test_each_call_replaces_the_previous_program(self):
        """One fixed path, so a stale program cannot be run in place of the current one."""
        sandbox = _ScriptedSandbox()
        tool = _tool(_backend(sandbox))
        _run(tool, "print(1)")
        _run(tool, "print(2)")

        assert sandbox.files == {f"{_WORK_DIR}/{_PROGRAM_FILENAME}": "print(2)"}

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
    def _description(self) -> str:
        return _callable(_tool(_backend())).__doc__ or ""

    def test_it_says_only_printed_output_comes_back(self):
        assert "print" in self._description()

    def test_it_says_the_sandbox_has_no_network(self):
        assert "no network" in self._description().lower()


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
