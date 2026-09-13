"""CodeAct programs evaluated without exec, including their actual file operations."""

from __future__ import annotations

import asyncio
import builtins
import io
import posixpath
import subprocess
import sys
from dataclasses import replace
from types import SimpleNamespace

import pytest
from maf_sandbox import (
    DEFAULT_CAPABILITIES,
    DEFAULT_TRANSFER_LIMITS,
    Artifact,
    CallerContext,
    Capability,
    Cleanup,
    EgressRule,
    ExecResult,
    HostToolRegistry,
    LandedArtifact,
    OutputSink,
    SandboxCapabilityDenied,
    SandboxCapabilityNotSupported,
    SandboxQueuedTimeout,
    SandboxRouter,
    Selection,
    SourceIntegrity,
)
from maf_sandbox.testing import (
    FAKE_BACKEND_DECLARATIONS,
    InMemoryStore,
    InProcessSandbox,
    InProcessSandboxBackend,
)

from maf_sandbox_codeact import (
    CodeactOutputs,
    CodeactRuntime,
    codeact_sandbox_spec,
    make_codeact_tools,
)
from maf_sandbox_codeact._runtime import runtime_program

_RUNTIME = CodeactRuntime("Python statements; json is available. No subprocess or network modules.")
_FILES_RUNTIME = replace(_RUNTIME, guest_work_dir="/runtime")


class _WrittenBuffer(io.BytesIO):
    def __init__(self, contents, path, mode):
        super().__init__(b"" if "w" in mode else contents.get(path, b""))
        self.contents, self.path, self.open_mode = contents, path, mode
        if "a" in mode:
            self.seek(0, 2)

    def close(self):
        if not self.closed and any(letter in self.open_mode for letter in "wa+"):
            self.contents[self.path] = self.getvalue()
        super().close()


class _PythonSandbox(InProcessSandbox):
    """Evaluate only these tests' authored programs against an in-memory guest filesystem."""

    def __init__(self, *, failure=None):
        super().__init__()
        self.failure = failure
        self.writes = []
        self.guest_directories = []

    async def exec(self, *args, **kwargs):
        raise AssertionError("the runtime has no exec")

    async def write_file(self, path, content, *, working_directory):
        self.writes.append((path, content, working_directory))
        await super().write_file(path, content, working_directory=working_directory)

    def _mkdir(self, path, *, exist_ok=False):
        self.guest_directories.append(path)
        self.directories.add(path)

    def _open(self, path, mode="r", *, encoding="utf-8"):
        assert path.startswith("/runtime/"), "program must use its explicit guest_call_path"
        path = posixpath.normpath(path)
        if "r" in mode and path not in self.contents:
            raise FileNotFoundError(path)
        buffer = _WrittenBuffer(self.contents, path, mode)
        return buffer if "b" in mode else io.TextIOWrapper(buffer, encoding=encoding)

    async def run_code(self, code, *, timeout):
        self.programs.append((code, timeout))
        if self.failure is not None:
            raise self.failure
        stdout, stderr = io.StringIO(), io.StringIO()

        def printing(*args, **kwargs):
            kwargs.setdefault("file", stdout)
            builtins.print(*args, **kwargs)

        def importing(name, *args, **kwargs):
            if name == "os":
                return SimpleNamespace(makedirs=self._mkdir)
            if name == "sys":
                return SimpleNamespace(stdout=stdout, stderr=stderr)
            if name in {"json", "__future__"}:
                return builtins.__import__(name, *args, **kwargs)
            raise ImportError(name)

        namespace = {
            "__name__": "__main__",
            "__builtins__": dict(
                vars(builtins), print=printing, open=self._open, __import__=importing
            ),
        }
        try:
            exec(code, namespace)
        except Exception as error:
            return ExecResult(stdout=stdout.getvalue(), stderr=str(error), exit_code=1)
        return ExecResult(stdout=stdout.getvalue(), stderr=stderr.getvalue())


async def _listing(store):
    return await store.list()


def _context():
    return CallerContext(
        current_scope=lambda: "test-scope",
        current_thread_id=lambda: "test-thread",
        list_files=_listing,
    )


def _backend(sandbox, *, capabilities=None, name="runtime"):
    return InProcessSandboxBackend(
        sandbox,
        name=name,
        declarations=replace(
            FAKE_BACKEND_DECLARATIONS,
            egress_method_tokens=None,
            capabilities=frozenset(
                {
                    Capability.RUN_CODE,
                    Capability.FILES_IN,
                    Capability.FILES_OUT,
                    Capability.SNAPSHOT,
                }
                if capabilities is None
                else capabilities
            ),
        ),
    )


def _make(sandbox=None, *, selection=Selection.FIXED, capabilities=None, **kwargs):
    sandbox = sandbox or _PythonSandbox()
    backend = _backend(sandbox, capabilities=capabilities)
    router = SandboxRouter([backend], min_isolation=backend.isolation, selection=selection)
    tool = make_codeact_tools(
        router, "analyst", _context(), runtime=kwargs.pop("runtime", _RUNTIME), **kwargs
    )[0]
    return tool, sandbox, backend


def _function(tool):
    return getattr(tool, "func", None) or getattr(tool, "__wrapped__", None) or tool


def _run(tool, code="print(2 + 2)", **kwargs):
    return asyncio.run(_function(tool)(code=code, **kwargs))


@pytest.mark.parametrize("selection", list(Selection))
def test_plain_runtime_needs_no_file_or_exec_capability(selection):
    tool, sandbox, backend = _make(selection=selection, capabilities={Capability.RUN_CODE})
    code = "from __future__ import annotations\nprint(2 + 2)"
    assert "4" in _run(tool, code)
    assert sandbox.programs == [(code, 120)]
    assert sandbox.writes == sandbox.commands == []
    assert backend.specs[-1] == codeact_sandbox_spec(runtime=_RUNTIME)
    assert backend.specs[-1].requires == frozenset({Capability.RUN_CODE})
    assert backend.specs[-1].kind == "codeact"
    assert DEFAULT_CAPABILITIES == {Capability.EXEC, Capability.FILES_IN}


@pytest.mark.parametrize("selection", list(Selection))
def test_routing_selects_a_backend_for_the_explicit_variant(selection):
    runtime = _backend(_PythonSandbox())
    shell = InProcessSandboxBackend(name="shell")
    router = SandboxRouter(
        [shell, runtime] if selection is Selection.PER_SPEC else [runtime],
        min_isolation=runtime.isolation,
        selection=selection,
    )
    tool = make_codeact_tools(router, "analyst", _context(), runtime=_RUNTIME)[0]
    assert "4" in _run(tool)
    assert runtime.specs and not shell.specs
    if selection is Selection.FIXED:
        with pytest.raises(SandboxCapabilityNotSupported, match="exec"):
            make_codeact_tools(router, "analyst", _context())


def test_runtime_refuses_an_exec_only_backend_at_attach():
    with pytest.raises(SandboxCapabilityNotSupported, match="run_code"):
        _make(capabilities=DEFAULT_CAPABILITIES)


def test_the_hosts_capability_denial_is_not_bypassed():
    backend = _backend(_PythonSandbox())
    router = SandboxRouter(
        [backend], min_isolation=backend.isolation, denied_capabilities={Capability.RUN_CODE}
    )
    with pytest.raises(SandboxCapabilityDenied, match="run_code"):
        make_codeact_tools(router, "analyst", _context(), runtime=_RUNTIME)
    assert not backend.specs


@pytest.mark.parametrize("mode", list(CodeactOutputs))
@pytest.mark.parametrize("takes_files", [False, True])
def test_file_capabilities_follow_the_wired_channels(mode, takes_files):
    expected = {Capability.RUN_CODE}
    if takes_files:
        expected.add(Capability.FILES_IN)
    if mode is not CodeactOutputs.NONE:
        expected.add(Capability.FILES_OUT)
    spec = codeact_sandbox_spec(runtime=_FILES_RUNTIME, outputs=mode, takes_files=takes_files)
    assert spec.requires == expected
    assert spec.work_dir == "/runtime"
    assert spec.exclusive_admission and not spec.confined_to_guest_call_path


@pytest.mark.parametrize(
    "kwargs",
    [
        {"takes_files": True},
        {"outputs": CodeactOutputs.DECLARED},
        {"outputs": CodeactOutputs.MANIFEST},
    ],
)
def test_a_runtime_without_a_file_contract_refuses_file_modes(kwargs):
    with pytest.raises(ValueError, match="guest_work_dir"):
        codeact_sandbox_spec(runtime=_RUNTIME, **kwargs)


def test_the_tool_also_refuses_a_file_store_without_a_file_contract():
    with pytest.raises(ValueError, match="guest_work_dir"):
        _make(file_store=InMemoryStore({}))


def test_native_host_tools_refuse_before_a_shim_is_generated():
    registry = HostToolRegistry(require_declared=False)
    registry.register(lambda: None, name="example")
    with pytest.raises(ValueError, match="native channel"):
        codeact_sandbox_spec(runtime=_RUNTIME, host_tools=registry)
    with pytest.raises(ValueError, match="native channel"):
        _make(host_tools=registry)


def test_an_empty_registry_adds_no_capability():
    assert codeact_sandbox_spec(runtime=_RUNTIME, host_tools=HostToolRegistry()).host_tools is None


@pytest.mark.parametrize("timeout", [0, -1, float("inf"), float("nan")])
def test_runtime_requires_a_real_deadline(timeout):
    with pytest.raises(ValueError, match="finite positive"):
        _make(exec_timeout_seconds=timeout)


@pytest.mark.parametrize(
    "failure, phrase",
    [
        (SandboxQueuedTimeout("provider detail"), "never started"),
        (TimeoutError("provider detail"), "timed out after 7s"),
        (RuntimeError("provider detail"), "could not run"),
    ],
)
def test_timeout_and_failure_messages_do_not_quote_backend_detail(failure, phrase):
    tool, sandbox, _ = _make(_PythonSandbox(failure=failure), exec_timeout_seconds=7)
    answer = _run(tool)
    assert phrase in answer
    assert "provider detail" not in answer
    assert sandbox.programs[0][1] == 7


def test_plain_source_keeps_byte_limits_without_spending_a_file_slot():
    limits = replace(DEFAULT_TRANSFER_LIMITS, max_files=0, max_bytes_per_file=10)
    tool, sandbox, backend = _make(files_in=limits)
    assert "4" in _run(tool, "print(4)")
    acquires = len(backend.specs)
    assert "bytes per program" in _run(tool, "print('too long')")
    assert len(backend.specs) == acquires
    assert len(sandbox.programs) == 1 and not sandbox.writes


@pytest.mark.parametrize("code", ["\ud800", "x = '\udfff'"])
def test_unencodable_code_refuses_before_acquire(code):
    tool, sandbox, backend = _make()
    assert "not valid UTF-8" in _run(tool, code)
    assert not backend.specs and not sandbox.programs


def test_file_bootstrap_bytes_are_also_bounded():
    tool, sandbox, backend = _make(
        runtime=_FILES_RUNTIME,
        files_in=replace(DEFAULT_TRANSFER_LIMITS, max_bytes_per_file=32),
    )
    assert "bytes per program" in _run(tool, "print(4)")
    assert not sandbox.programs and not backend.specs


@pytest.mark.parametrize("runtime", [_RUNTIME, _FILES_RUNTIME], ids=["plain", "files"])
@pytest.mark.parametrize(
    "code",
    [
        "x = 1\nfrom __main__ import x\nassert x == 1",
        "class Result:\n    value = 42\nimport pickle\nassert pickle.loads(pickle.dumps(Result())).value == 42",
        "def result():\n    return 42\nimport pickle\nassert pickle.loads(pickle.dumps(result))() == 42",
    ],
    ids=["self_import", "pickle_instance", "pickle_function"],
)
def test_runtime_preserves_the_active_module_namespace(runtime, code):
    program = runtime_program(runtime, code, "call")
    # A separate interpreter provides a real __main__ without replacing pytest's module.
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import os, sys\nos.makedirs = lambda *args, **kwargs: None\nexec(sys.stdin.read(), globals())",
        ],
        input=program,
        text=True,
        capture_output=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("mode", [CodeactOutputs.DECLARED, CodeactOutputs.MANIFEST])
@pytest.mark.parametrize("cleanup", [Cleanup.RESET, Cleanup.DISPOSE])
def test_real_file_operations_collect_before_cleanup_and_do_not_change_cwd(mode, cleanup):
    sandbox = _PythonSandbox()
    backend = _backend(sandbox)
    router = SandboxRouter([backend], min_isolation=backend.isolation, min_cleanup=cleanup)
    landed = []

    async def deliver(artifact: Artifact):
        assert artifact.content == b"42"
        assert any(path.endswith("/answer.txt") for path in sandbox.contents)
        landed.append(artifact)
        return LandedArtifact(name=artifact.name, display="saved")

    tool = make_codeact_tools(
        router,
        "analyst",
        _context(),
        runtime=_FILES_RUNTIME,
        file_store=InMemoryStore({"program.py": "41"}),
        output_sink=OutputSink(deliver=deliver),
        outputs=mode,
        files_in=replace(DEFAULT_TRANSFER_LIMITS, max_files=1),
    )[0]
    code = '''"""The user's module docstring."""
from __future__ import annotations
assert __doc__ == "The user's module docstring."
with open(guest_call_path + '/program.py') as source:
    answer = str(int(source.read()) + 1)
with open(guest_call_path + '/answer.txt', 'w') as output:
    output.write(answer)
with open(guest_call_path + '/outputs.json', 'w') as manifest:
    manifest.write('{"outputs": [{"path": "answer.txt"}]}')
print(answer)
'''
    args = {"outputs": ["answer.txt"]} if mode is CodeactOutputs.DECLARED else {}
    result = _run(tool, code, files=["program.py"], **args)
    assert "42" in result and landed[0].name == "answer.txt"
    assert len(sandbox.writes) == 1
    assert sandbox.writes[0][0] == "program.py"
    assert not sandbox.commands
    assert "chdir" not in sandbox.programs[0][0]
    assert backend.specs[-1] == codeact_sandbox_spec(
        runtime=_FILES_RUNTIME,
        takes_files=True,
        outputs=mode,
        files_in=replace(DEFAULT_TRANSFER_LIMITS, max_files=1),
    )
    if cleanup is Cleanup.RESET:
        assert not sandbox.contents
    else:
        assert backend.disposed_kinds[-1] == "codeact"


def test_withholding_collects_after_guest_failure_without_returning_streams():
    landed = []

    async def deliver(artifact: Artifact):
        landed.append(artifact)
        return LandedArtifact(name=artifact.name, display="must not be rendered")

    tool, _, _ = _make(
        runtime=_FILES_RUNTIME,
        outputs=CodeactOutputs.DECLARED,
        output_sink=OutputSink(deliver=deliver),
        withhold_guest_output=True,
    )
    result = _run(
        tool,
        "with open(guest_call_path + '/answer.txt', 'w') as f:\n    f.write('secret')\nprint('secret')\nraise ValueError('secret')",
        outputs=["answer.txt"],
    )
    assert not isinstance(result, str)
    assert "secret" not in str(result) and "must not be rendered" not in str(result)
    assert "non-zero" in result[0].text
    assert "declared output" in result[-1].text
    assert landed[0].content == b"secret"
    assert tool.additional_properties["source_integrity"] == SourceIntegrity.UNTRUSTED


def test_runtime_instructions_do_not_promise_an_exec_image_or_implicit_working_directory():
    tool, _, _ = _make(
        runtime=_FILES_RUNTIME,
        egress_allow=(EgressRule("example.com", ("GET",)),),
        capabilities={Capability.RUN_CODE, Capability.SNAPSHOT, Capability.EGRESS_METHODS},
    )
    description = tool.description
    assert _RUNTIME.instructions in description
    assert "python3 program.py" not in description
    assert "whatever the sandbox image ships" not in description
    assert "Each call gets a fresh working" not in description
    assert "guest_call_path" in description and "not changed" in description


def test_unconfigured_hosts_still_receive_no_tools():
    assert (
        make_codeact_tools(
            None, "analyst", _context(), runtime="bad", outputs=CodeactOutputs.MANIFEST
        )
        == []
    )


@pytest.mark.parametrize(
    "path",
    [
        "relative",
        "/",
        "/runtime/../other",
        "/runtime/",
        "//runtime",
        "C:\\runtime",
        "/runtime\0bad",
    ],
)
def test_bad_runtime_bases_refuse(path):
    with pytest.raises(ValueError, match="absolute POSIX"):
        CodeactRuntime("Python", guest_work_dir=path)


@pytest.mark.parametrize("instructions", ["", "   ", None, 2])
def test_the_host_must_state_the_runtime_facilities(instructions):
    with pytest.raises(ValueError, match="instructions"):
        CodeactRuntime(instructions)


def test_submitted_program_and_shared_files_spend_one_byte_budget():
    tool, sandbox, backend = _make(
        runtime=_FILES_RUNTIME,
        file_store=InMemoryStore({"input.txt": "x" * 550}),
        files_in=replace(
            DEFAULT_TRANSFER_LIMITS, max_files=1, max_bytes_per_file=1000, max_total_bytes=600
        ),
    )
    assert "per call" in _run(tool, files=["input.txt"])
    assert not backend.specs and not sandbox.programs and not sandbox.writes


@pytest.mark.parametrize("channel", ["files", "outputs"])
def test_runtime_file_names_still_refuse_traversal_before_acquire(channel):
    async def deliver(artifact: Artifact):
        raise AssertionError("a refused name must not reach the sink")

    tool, sandbox, backend = _make(
        runtime=_FILES_RUNTIME,
        file_store=InMemoryStore({"../escape": "data"}),
        outputs=CodeactOutputs.DECLARED,
        output_sink=OutputSink(deliver=deliver),
    )
    assert "Error:" in _run(tool, **{channel: ["../escape"]})
    assert not backend.specs and not sandbox.programs and not sandbox.writes


def test_runtime_calls_use_distinct_directories_after_reset():
    sandbox = _PythonSandbox()
    backend = _backend(sandbox)
    router = SandboxRouter([backend], min_isolation=backend.isolation, min_cleanup=Cleanup.RESET)
    tool = make_codeact_tools(router, "analyst", _context(), runtime=_FILES_RUNTIME)[0]
    first = _run(tool, "print(guest_call_path)")
    second = _run(tool, "print(guest_call_path)")
    assert "/runtime/" in first and "/runtime/" in second and first != second
    assert len(set(sandbox.guest_directories)) == 2


@pytest.mark.parametrize(
    "changed",
    [replace(_RUNTIME, instructions="Python with different modules"), _FILES_RUNTIME, None],
)
def test_attached_variants_refuse_to_change_a_live_runtime_contract(changed):
    sandbox = _PythonSandbox()
    backend = _backend(sandbox, capabilities=set(Capability))
    router = SandboxRouter([backend], min_isolation=backend.isolation, min_cleanup=Cleanup.RESET)
    original = make_codeact_tools(router, "analyst", _context(), runtime=_RUNTIME)[0]
    different = make_codeact_tools(router, "analyst", _context(), runtime=changed)[0]
    assert "4" in _run(original)
    refusal = (
        "cannot change its storage base"
        if changed is _FILES_RUNTIME
        else "different execution contract"
    )
    assert refusal in _run(different)
    assert len(sandbox.programs) == 1 and not sandbox.writes
    assert "4" in _run(original)


def test_cancellation_propagates_and_cleans_the_runtime():
    tool, sandbox, backend = _make(_PythonSandbox(failure=asyncio.CancelledError()))
    with pytest.raises(asyncio.CancelledError):
        _run(tool)
    assert len(sandbox.programs) == 1
    assert backend.disposed_kinds[-1] == "codeact"


def test_runtime_calls_wait_for_exclusive_admission():
    async def scenario():
        entered, release, second_started = asyncio.Event(), asyncio.Event(), asyncio.Event()

        class WaitingPython(_PythonSandbox):
            async def run_code(self, code, *, timeout):
                entered.set()
                await release.wait()
                return await super().run_code(code, timeout=timeout)

        sandbox = WaitingPython()
        tool, _, backend = _make(sandbox)
        first = asyncio.create_task(_function(tool)(code="print('first')"))
        await entered.wait()
        acquires = len(backend.specs)

        async def another():
            second_started.set()
            return await _function(tool)(code="print('second')")

        second = asyncio.create_task(another())
        await second_started.wait()
        await asyncio.sleep(0)
        assert len(backend.specs) == acquires and not second.done()
        release.set()
        answers = await asyncio.gather(first, second)
        assert "first" in answers[0] and "second" in answers[1]
        assert len(sandbox.programs) == 2

    asyncio.run(scenario())


def test_runtime_can_produce_files_without_files_in():
    landed = []

    async def deliver(artifact: Artifact):
        landed.append(artifact)
        return LandedArtifact(name=artifact.name, display="saved")

    tool, sandbox, _ = _make(
        runtime=_FILES_RUNTIME,
        capabilities={Capability.RUN_CODE, Capability.FILES_OUT},
        outputs=CodeactOutputs.DECLARED,
        output_sink=OutputSink(deliver=deliver),
        files_in=replace(DEFAULT_TRANSFER_LIMITS, max_files=0),
    )
    _run(
        tool,
        "with open(guest_call_path + '/answer.txt', 'w') as f:\n    f.write('42')",
        outputs=["answer.txt"],
    )
    assert landed[0].content == b"42"
    assert not sandbox.writes


@pytest.mark.parametrize("channel", ["input", "output"])
def test_runtime_file_channels_refuse_missing_capabilities_at_attach(channel):
    async def deliver(artifact: Artifact):
        raise AssertionError("a refused attachment must not reach the sink")

    kwargs = (
        {"file_store": InMemoryStore({})}
        if channel == "input"
        else {"outputs": CodeactOutputs.DECLARED, "output_sink": OutputSink(deliver=deliver)}
    )
    with pytest.raises(SandboxCapabilityNotSupported, match="files_in|files_out"):
        _make(runtime=_FILES_RUNTIME, capabilities={Capability.RUN_CODE}, **kwargs)
