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
import dataclasses
import inspect
import json
import logging
import warnings
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from types import MappingProxyType
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
    SandboxLimits,
    SandboxOutputError,
    SandboxOutputSinkRequired,
    SandboxProgramTimeout,
    SandboxRouter,
    SandboxTransferLimitsNotPermitted,
    SourceIntegrity,
    TransferLimits,
    guest_run_layout,
    host_tool_shim,
    launcher_script,
    sandbox_tool,
)
from maf_sandbox.testing import (
    FAKE_BACKEND_DECLARATIONS,
    InMemoryStore,
    InProcessSandbox,
    InProcessSandboxBackend,
)

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
    _WITHHELD_ROUTE,
    _WORK_DIR,
    _format_withheld,
)

#: What a backend must declare before this kind may collect anything.
_PULLS = DEFAULT_CAPABILITIES | {Capability.FILES_OUT}

#: And before a program may reach a host tool: a host-tool call carries its requests over the
#: same pull surface, so it needs everything a collection needs and the capability besides.
_CALLS = _PULLS | {Capability.HOST_TOOLS}

# ---------------------------------------------------------------------------
# Fakes: a sandbox that keeps the command it was handed, unjoined, and what it was written
# ---------------------------------------------------------------------------


def _is_core_removal(command: str | Sequence[str]) -> bool:
    """Whether ``command`` is core removing a call's directory rather than the program running.

    Core spells `rm -rf` today and dispatches `Sandbox.reclaim` once that ships, which is no
    command at all. This suite asserts on what the kind ran, so it keeps either out of the
    ledger below.
    """
    return isinstance(command, str) and command.startswith("rm -rf ")


class _RecordingContents(dict[str, bytes]):
    """A sandbox's ``contents``, copying every write where the reclaim cannot reach it."""

    def __init__(self, written: dict[str, bytes], seeded: Mapping[str, bytes]) -> None:
        super().__init__()
        self._written = written
        for path, content in seeded.items():
            self[path] = content

    def __setitem__(self, path: str, content: bytes) -> None:
        super().__setitem__(path, content)
        self._written[path] = content


class _ScriptedSandbox(InProcessSandbox):
    """Records the raw ``command``, answers with a whole :class:`ExecResult`, and its writes.

    :class:`~maf_sandbox.testing.InProcessSandbox` joins an argv sequence with
    :func:`shlex.join` before recording it, and scripts stdout alone. This kind's tests need
    the command *unjoined* — that it is a sequence at all is the property under test, and a
    joined string cannot be told apart from a shell line — and need stderr and exit codes
    alongside stdout to exercise the result format.

    :attr:`written` is what they read in place of ``contents``: a call's own directory is
    genuinely gone once the call returns, and what the call wrote into it is the property
    under test. Every write lands there, whether it arrives through ``write_file`` or is
    put straight into ``contents`` the way the fakes below stand in for a program's output.
    """

    def __init__(self, result: ExecResult | None = None, **kwargs) -> None:
        super().__init__(**kwargs)
        self.result = result
        self.raw_commands: list[str | Sequence[str]] = []
        #: Every path ever written, in bytes. :attr:`written_files` is the decoded view.
        self.written: dict[str, bytes] = {}
        self.contents = _RecordingContents(self.written, self.contents)

    @property
    def written_files(self) -> Mapping[str, str]:
        """:attr:`written`, UTF-8 decoded — what ``files`` is to ``contents``."""
        return MappingProxyType({p: c.decode("utf-8") for p, c in self.written.items()})

    async def exec(self, command, *, working_directory, timeout):
        if not _is_core_removal(command):
            self.raw_commands.append(command)
        answer = await super().exec(command, working_directory=working_directory, timeout=timeout)
        return self.result if self.result is not None else answer


class _WriteFailingSandbox(_ScriptedSandbox):
    async def write_file(self, path: str, content: str | bytes, *, working_directory: str) -> None:
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
    """A sandbox whose "program" calls one host tool, prints the answer, and exits.

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
        if str(command).startswith("kill") or _is_core_removal(command):
            # Neither starts a program, so neither is a run this fake should record.
            return result
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
    """A guest served over the host-tool-call transport that produces its files and then leaves
    the exit marker.

    The supervisor polls for that marker, so a guest that never writes one is waited out. This
    one calls nothing: what it stands in for is a run over that transport that simply *succeeds*.
    """

    def _program_cwd(self, working_directory: str) -> str:
        """The launcher `cd`s into the work directory before starting the program, so that is
        where a relative filename lands — not the run directory `exec` was handed."""
        return guest_run_layout(working_directory, program=_PROGRAM_FILENAME).work

    async def exec(self, command, *, working_directory, timeout):
        result = await super().exec(command, working_directory=working_directory, timeout=timeout)
        if str(command).startswith("kill") or _is_core_removal(command):
            # Neither starts a program, so neither is a run this fake should record.
            return result
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


class _LeakyDisplaySink(_RecordingSink):
    """A host sink whose ``display`` quotes the artifact's own bytes.

    Permitted: ``deliver`` is handed the :class:`~maf_sandbox.Artifact`, and no protocol rule
    says ``display`` may not be derived from its ``content``. So a kind that renders ``display``
    renders whatever the guest wrote, however the kind labels its own result.
    """

    async def deliver(self, artifact: Artifact) -> LandedArtifact:
        await super().deliver(artifact)
        return LandedArtifact(
            name=artifact.name,
            display=f"{artifact.name}: {artifact.content.decode()}",
            handle=f"blob://{artifact.name}?sig=secret",
        )


class _RefusingSink(_RecordingSink):
    """A host sink that refuses by raising, quoting the artifact's bytes in the reason.

    Permitted the same way `_LeakyDisplaySink`'s `display` is: `deliver` is handed the
    `Artifact`, and nothing constrains the text it raises with.
    """

    async def deliver(self, artifact: Artifact) -> LandedArtifact:
        raise SandboxOutputError(f"{artifact.name} rejected: {artifact.content.decode()}")


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


#: Transfer limits the router accepts for a *folded* spec (#393). Generous on purpose, so these
#: tests exercise mechanics rather than the limit match; `TestTheSpecCarriesItsHostToolSurface`
#: covers the match. A test that wants a specific ceiling passes `limits=` explicitly.
_MiB = 1024 * 1024
_CALL_LIMITS = SandboxLimits(
    files_in=TransferLimits(
        max_bytes_per_file=64 * _MiB, max_total_bytes=256 * _MiB, max_files=1024
    ),
    files_out=TransferLimits(
        max_bytes_per_file=64 * _MiB, max_total_bytes=256 * _MiB, max_files=1024
    ),
)


def _backend(
    sandbox: InProcessSandbox | None = None,
    *,
    acquire_error: BaseException | None = None,
    capabilities: frozenset[Capability] | None = None,
    limits: SandboxLimits | None = None,
) -> InProcessSandboxBackend:
    # A backend that arms host tool calls declares limits that can serve it, unless a test pins
    # its own.
    if limits is None and capabilities is not None and Capability.HOST_TOOLS in capabilities:
        limits = _CALL_LIMITS
    declarations = FAKE_BACKEND_DECLARATIONS
    if capabilities is not None:
        declarations = dataclasses.replace(declarations, capabilities=capabilities)
    if limits is not None:
        declarations = dataclasses.replace(declarations, limits=limits)
    return InProcessSandboxBackend(
        sandbox if sandbox is not None else _ScriptedSandbox(),
        acquire_error=acquire_error,
        declarations=declarations,
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


def _host_tool_calling_tool(registry: HostToolRegistry, **kw: Any):
    """The tool a host gets for `registry`, on a backend that can serve what host tool calls
    need."""
    return _tool(_backend(capabilities=_CALLS), host_tools=registry, **kw)


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
# The spec — what containment is fixed, and what a deployment may open
# ---------------------------------------------------------------------------


class TestCodeactSandboxSpec:
    def test_kind_is_codeact(self):
        assert codeact_sandbox_spec().kind == CODEACT_KIND == "codeact"

    def test_work_dir_is_the_programs_own_root(self):
        assert codeact_sandbox_spec().work_dir == _WORK_DIR == "/maf-sandbox/work"

    def test_egress_is_closed_by_default(self):
        """A spec that names no host denies every host, so the program can compute but cannot
        fetch — and with nothing callable as a host tool from inside either, nothing external
        can enter the sandbox and nothing leaves it but what the program printed.

        The default, not the only option: a deployment may name hosts, and the tests below
        cover what that opens. What stays fixed is the kind's own half.
        """
        assert codeact_sandbox_spec().egress_allow == ()

    def test_a_deployments_hosts_reach_the_spec(self):
        """The second list: endpoints a published kind cannot know, supplied by whoever wires
        it. The spec is what the router matches, so this is where they have to arrive."""
        spec = codeact_sandbox_spec(egress_allow=("index.example", "artifacts.example"))

        assert spec.egress_allow == ("index.example", "artifacts.example")

    def test_the_spec_carries_the_union_of_both_lists(self, monkeypatch: pytest.MonkeyPatch):
        """`_KIND_EGRESS` is empty today, which makes the union indistinguishable from the
        deployment's half alone — so it is patched here rather than asserted around.

        Without this the kind's half could be dropped entirely and nothing would notice until
        a release added something to it.
        """
        monkeypatch.setattr("maf_sandbox_codeact._tool._KIND_EGRESS", ("modules.example",))

        spec = codeact_sandbox_spec(egress_allow=("index.example",))

        assert spec.egress_allow == ("modules.example", "index.example")

    def test_the_kinds_half_is_there_even_when_the_deployment_adds_nothing(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr("maf_sandbox_codeact._tool._KIND_EGRESS", ("modules.example",))

        assert codeact_sandbox_spec().egress_allow == ("modules.example",)

    def test_a_host_named_by_both_lists_is_carried_once(self, monkeypatch: pytest.MonkeyPatch):
        """Two callers naming the same host is agreement, not a mistake — and a duplicate in an
        allowlist is a second rule that can drift from the first."""
        monkeypatch.setattr("maf_sandbox_codeact._tool._KIND_EGRESS", ("shared.example",))

        spec = codeact_sandbox_spec(egress_allow=("shared.example", "other.example"))

        assert spec.egress_allow == ("shared.example", "other.example")

    def test_a_host_the_deployment_names_twice_is_carried_once(self):
        """Cross-list dedup does not exercise this: a repeated host inside one list is a
        second rule that can drift from the first."""
        spec = codeact_sandbox_spec(egress_allow=("a.example", "a.example", "b.example"))

        assert spec.egress_allow == ("a.example", "b.example")

    def test_a_bare_string_is_refused_rather_than_read_one_character_at_a_time(self):
        """`Sequence[str]` admits `str`, so this type-checks and would otherwise become seven
        single-character hosts — the real endpoint unreachable, with no refusal anywhere."""
        with pytest.raises(TypeError, match="not a single string"):
            codeact_sandbox_spec(egress_allow="pypi.org")

    @pytest.mark.parametrize("router", [None, SandboxRouter([])], ids=["no router", "no backend"])
    def test_an_unconfigured_host_still_gets_no_tools_rather_than_an_exception(self, router):
        """This factory's contract, and a malformed allowlist is exactly how a host that
        develops with sandboxing off would trip it — a trailing comma in configuration.

        Both spellings of unconfigured, because `configured` is a conjunction and a gate that
        reads only `router is not None` refuses the second while satisfying the first.
        """
        assert make_codeact_tools(router, "agent", _context(), egress_allow=("",)) == []
        assert make_codeact_tools(router, "agent", _context(), egress_allow="pypi.org") == []

    def test_a_bare_string_is_refused_on_the_tool_factory_too(self):
        """The spec factory refuses it, but `make_codeact_tools` validates on its own path and
        promises the same `TypeError` where a sandbox is configured — so it needs its own pin,
        or that promise rests on a check nothing exercises."""
        with pytest.raises(TypeError, match="not a single string"):
            _tool(_backend(capabilities=_PULLS), egress_allow="pypi.org")

    @pytest.mark.parametrize("blank", ["", "   ", "\t"])
    def test_a_blank_entry_is_refused(self, blank: str):
        """An allowlist entry matching nothing, which reads like one matching everything."""
        with pytest.raises(ValueError, match="non-empty hostnames"):
            codeact_sandbox_spec(egress_allow=(blank,))

    @pytest.mark.parametrize(
        "entry",
        [" files.example", "files.example ", "a b.example", "a,b.example"],
        ids=["leading space", "trailing space", "inner space", "comma"],
    )
    def test_an_entry_that_is_not_one_hostname_is_refused(self, entry: str):
        """No hostname holds a space or a comma, and each is a way for the spec, the model's
        description, and a backend's allowlist to disagree silently: a comma-joined `"a,b"` is
        one spec entry the wslc proxy splits back into two, and a padded `" a"` reaches a
        backend as a rule that matches nothing. Refused, not stripped."""
        with pytest.raises(ValueError, match="one hostname each"):
            codeact_sandbox_spec(egress_allow=(entry,))

    def test_a_one_shot_iterable_is_not_spent_before_it_reaches_the_spec(self):
        """`Sequence[str]` is the contract, but a host building the list dynamically hands over
        a generator — and validating one by iterating it leaves nothing to return, so the
        allowlist vanishes with no refusal and the model is told the network is closed."""
        named = ("index.example", "artifacts.example")

        assert codeact_sandbox_spec(egress_allow=(host for host in named)).egress_allow == named

    def test_a_wildcard_host_is_accepted(self):
        """The refusal is spaces and commas, not punctuation — `*.data.mcr.microsoft.com` is a
        legitimate allowlist entry (Bicep's own uses it) and must survive."""
        spec = codeact_sandbox_spec(egress_allow=("*.data.mcr.microsoft.com",))
        assert spec.egress_allow == ("*.data.mcr.microsoft.com",)

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
        """An empty registry is a host-tool surface that does not exist, and reads as one."""
        assert codeact_sandbox_spec(host_tools=_registry()) == codeact_sandbox_spec()

    def test_a_registry_adds_the_capability_and_the_surface_host_tool_calls_travel_over(self):
        """`FILES_OUT` is not optional here, and not this kind's output channel either: the
        transport stats and reads the program's request files and its exit marker, so even a
        stdout-only program that can call a host function needs the pull surface."""
        spec = codeact_sandbox_spec(host_tools=_registry(_exchange_rate))
        assert spec.requires == frozenset(
            {Capability.EXEC, Capability.FILES_IN, Capability.FILES_OUT, Capability.HOST_TOOLS}
        )

    def test_a_registry_of_pure_computation_widens_it_just_the_same(self):
        """The capability follows from something being callable at all, never from what
        the aggregate found in it: a tool that is no source, no sink and no authority is
        still a call crossing the boundary."""
        spec = codeact_sandbox_spec(host_tools=_registry(_round_half_up))
        assert Capability.HOST_TOOLS in spec.requires
        assert spec.identities == frozenset()

    def test_the_identities_a_registry_declares_reach_the_spec(self):
        """Which is what the router's `denied_identities` is matched against at attach."""
        spec = codeact_sandbox_spec(
            host_tools=_registry(
                _exchange_rate,
                _the_callers_calendar,
                allowed_identities=frozenset({Identity.APP, Identity.USER}),
            )
        )
        assert spec.identities == frozenset({Identity.APP, Identity.USER})

    def test_reading_a_registry_seals_it(self):
        """A tool registered afterwards would be callable from a spec that never saw it."""
        registry = _registry(_exchange_rate)
        codeact_sandbox_spec(host_tools=registry)
        with pytest.raises(ValueError, match="sealed"):
            registry.register(_round_half_up)


# ---------------------------------------------------------------------------
# The program reaches the interpreter as a file, never as a command line
# ---------------------------------------------------------------------------


def _run_dirs(sandbox: _ScriptedSandbox) -> list[str]:
    """The distinct run directories this sandbox was written into, in first-seen order."""
    seen: list[str] = []
    for path in sandbox.written:
        if not path.startswith(f"{_WORK_DIR}/"):
            continue
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
        assert sandbox.written_files == {f"{run_dir}/{_PROGRAM_FILENAME}": "print('hi')"}

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
        assert list(sandbox.written_files.values()) == [code]

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
        assert sandbox.written_files == {
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
# Withheld guest output — the result a host can classify, and what replaces the streams
# ---------------------------------------------------------------------------


def _withholding_tool(sandbox: InProcessSandbox, sink: _RecordingSink | None = None, **kw: Any):
    """The pairing `withhold_guest_output` requires: declared outputs, and somewhere to land."""
    return _tool(
        _backend(sandbox, capabilities=_PULLS),
        **_landing(CodeactOutputs.DECLARED, sink),
        withhold_guest_output=True,
        **kw,
    )


class TestWithheldResultFormat:
    """With the streams withheld, every line of the result is something the host observed.

    The shape is fixed rather than elided the way `_format_result`'s is: a model reading this
    has nothing else to go on, and a section that vanishes when it is zero is one it cannot
    rely on.
    """

    def _out(self, result: ExecResult) -> str:
        return _run(_withholding_tool(_ScriptedSandbox(result)), "print('hi')")

    def test_what_the_program_printed_does_not_come_back(self):
        out = self._out(ExecResult(stdout="the secret is 42\n"))
        assert "the secret is 42" not in out
        assert "42" not in out

    def test_stderr_does_not_come_back_either(self):
        """A traceback is guest-authored text like any other, however useful it would be."""
        out = self._out(
            ExecResult(stdout="", stderr="Traceback ...\nNameError: undefined\n", exit_code=1)
        )
        assert "NameError" not in out
        assert "Traceback" not in out

    def test_the_streams_are_reported_as_byte_counts(self):
        out = self._out(ExecResult(stdout="42\n", stderr="warning\n"))
        assert "stdout: 3 bytes" in out
        assert "stderr: 8 bytes" in out

    def test_bytes_are_counted_as_utf_8_not_as_characters(self):
        """A count that said 2 for four bytes would be a claim about the wrong thing."""
        out = self._out(ExecResult(stdout="é☃"))
        assert "stdout: 5 bytes" in out

    def test_a_zero_exit_code_is_named_here_unlike_the_shown_format(self):
        """With the streams gone it is the only thing that says the program worked at all."""
        assert "exit code: 0" in self._out(ExecResult(stdout="42\n"))

    def test_a_non_zero_exit_code_is_named(self):
        assert "exit code: 1" in self._out(ExecResult(stdout="", stderr="boom\n", exit_code=1))

    def test_every_result_names_the_route_that_still_carries_content(self):
        """An exit code on its own leaves a model nothing it can act on."""
        out = self._out(ExecResult(stdout="", stderr="boom\n", exit_code=1))
        assert "declared output" in out
        assert "not read back as text" in out

    def test_a_silent_program_gets_the_same_shape_rather_than_the_print_advice(self):
        """`_NO_OUTPUT` tells a model to print what it needs, which is the opposite of the
        route here."""
        out = self._out(ExecResult(stdout=""))
        assert "printed nothing" not in out
        assert "stdout: 0 bytes" in out

    def test_the_whole_result_is_fixed_and_in_one_order(self):
        out = self._out(ExecResult(stdout="42\n", stderr="warning\n", exit_code=3))
        assert out.splitlines()[:3] == ["exit code: 3", "stdout: 3 bytes", "stderr: 8 bytes"]


class TestWithholdingStillLandsFiles:
    """Withholding closes the printing road; the declared-output road is what replaces it, so
    it has to keep working and keep being reported."""

    def test_a_landed_file_is_named_by_the_spelling_the_model_declared(self):
        sink = _RecordingSink()
        sandbox = _ProducingSandbox()
        tool = _withholding_tool(sandbox, sink)
        out = _run_producing(tool, sandbox, {"answer.txt": b"12 resources"}, outputs=["answer.txt"])

        assert sink.names == ["answer.txt"]
        assert "- answer.txt" in out

    def test_the_sinks_display_does_not_reach_a_withheld_result(self):
        """`display` is composed from an `Artifact` whose `content` is the guest's bytes, and
        no protocol rule keeps the two apart — so a sink that derives one from the other would
        put guest-authored text inside a result declaring trusted integrity."""
        sink = _LeakyDisplaySink()
        sandbox = _ProducingSandbox()
        tool = _withholding_tool(sandbox, sink)
        out = _run_producing(tool, sandbox, {"answer.txt": b"THE-SECRET"}, outputs=["answer.txt"])

        assert sink.names == ["answer.txt"], "the artifact still landed"
        assert "THE-SECRET" not in out, "the sink's content-derived display reached the result"
        assert "- answer.txt" in out

    def test_a_sinks_refusal_text_does_not_reach_a_withheld_result(self):
        """`deliver` refuses by raising and nothing constrains what it puts in the message —
        the same opening `display` was, one branch further out."""
        sandbox = _ProducingSandbox()
        tool = _withholding_tool(sandbox, _RefusingSink())
        out = _run_producing(tool, sandbox, {"answer.txt": b"THE-SECRET"}, outputs=["answer.txt"])

        assert "THE-SECRET" not in out, "the sink's refusal text reached a trusted result"
        assert "could not be saved" in out

    def test_the_shown_path_still_quotes_a_sinks_refusal(self):
        """Unchanged where the result is not claiming to hold no guest text."""
        sandbox = _ProducingSandbox()
        tool = _pulling_tool(sandbox, CodeactOutputs.DECLARED, _RefusingSink())
        out = _run_producing(tool, sandbox, {"answer.txt": b"THE-SECRET"}, outputs=["answer.txt"])

        assert "THE-SECRET" in out

    def test_the_shown_path_still_renders_the_sinks_display(self):
        """The host's own reference is the better string wherever the claim does not depend on
        it, so withholding is the only thing that gives it up."""
        sink = _RecordingSink()
        sandbox = _ProducingSandbox()
        tool = _pulling_tool(sandbox, CodeactOutputs.DECLARED, sink)
        out = _run_producing(tool, sandbox, {"answer.txt": b"12 resources"}, outputs=["answer.txt"])

        assert "saved answer.txt" in out

    def test_a_landed_files_contents_still_do_not_reach_the_result(self):
        """The sink's reference is the host's; what the program wrote into the file is not."""
        sink = _RecordingSink()
        sandbox = _ProducingSandbox()
        tool = _withholding_tool(sandbox, sink)
        out = _run_producing(tool, sandbox, {"answer.txt": b"12 resources"}, outputs=["answer.txt"])

        assert "12 resources" not in out

    def test_the_sinks_handle_is_still_never_rendered(self):
        sink = _RecordingSink()
        sandbox = _ProducingSandbox()
        tool = _withholding_tool(sandbox, sink)
        out = _run_producing(tool, sandbox, {"answer.txt": b"x"}, outputs=["answer.txt"])

        assert "sig=secret" not in out

    def test_a_declared_name_never_written_is_still_reported(self):
        """The model's own name, echoed back — nothing the program authored."""
        sandbox = _ProducingSandbox()
        tool = _withholding_tool(sandbox)
        out = _run_producing(tool, sandbox, {}, outputs=["answer.txt"])

        assert "answer.txt" in out

    def test_a_failed_program_still_gets_its_files_landed(self):
        """Showing the streams, a non-zero exit skips collection so a missing-file report does
        not bury the traceback. Withheld there is no traceback, and this is the only channel
        left — including for a program that caught its own error and wrote the diagnosis out.
        """
        sink = _RecordingSink()
        sandbox = _ProducingSandbox(result=ExecResult(stdout="", stderr="boom\n", exit_code=1))
        tool = _withholding_tool(sandbox, sink)
        out = _run_producing(tool, sandbox, {"why.txt": b"ValueError"}, outputs=["why.txt"])

        assert sink.names == ["why.txt"], "the one channel left was skipped on a failed run"
        assert "- why.txt" in out
        assert "exit code: 1" in out

    def test_a_failed_program_showing_its_streams_still_skips_collection(self):
        """The pre-existing rule is unchanged where the traceback does come back."""
        sink = _RecordingSink()
        sandbox = _ProducingSandbox(result=ExecResult(stdout="", stderr="boom\n", exit_code=1))
        tool = _pulling_tool(sandbox, CodeactOutputs.DECLARED, sink)
        _run_producing(tool, sandbox, {"why.txt": b"ValueError"}, outputs=["why.txt"])

        assert sink.names == []


class TestAWithheldStreamIsSizedNotDecoded:
    """`_stream_bytes` runs outside every guarded block in `_execute`, so anything it raises
    escapes the tool body and kills the caller's turn rather than answering the model."""

    def test_a_lone_surrogate_is_counted_rather_than_raising(self):
        """A backend's JSON carries one through as a `str` a plain encode refuses — the trap
        `_InboundTally.add` already guards on the way in."""
        out = _run(
            _withholding_tool(_ScriptedSandbox(ExecResult(stdout="ok\udcff"))), "print('hi')"
        )

        assert "stdout: 5 bytes" in out
        assert "exit code: 0" in out


class TestWithholdingIsRefusedWhereItCouldNotBeHonest:
    """Both refusals are at construction, like every other impossible pairing here: a tool the
    model can see and cannot use, or one making a claim it cannot keep, is worse than none."""

    def test_no_output_mode_is_refused_because_nothing_could_come_back(self):
        with pytest.raises(ValueError, match="no way to read anything back"):
            _tool(_backend(), withhold_guest_output=True)

    def test_the_manifest_mode_is_refused_because_the_guest_names_the_files(self):
        """Under `MANIFEST` the program writes `outputs.json`, so a name it chose is rendered
        back into the result — guest-authored text under a result claiming to have none."""
        with pytest.raises(ValueError, match="guest-authored text"):
            _tool(
                _backend(capabilities=_PULLS),
                **_landing(CodeactOutputs.MANIFEST),
                withhold_guest_output=True,
            )

    def test_the_refusal_names_the_mode_that_works(self):
        with pytest.raises(ValueError, match=str(CodeactOutputs.DECLARED)):
            _tool(_backend(), withhold_guest_output=True)

    def test_declared_outputs_are_accepted(self):
        assert _withholding_tool(_ScriptedSandbox()) is not None

    def test_an_unconfigured_host_still_gets_no_tool_rather_than_the_refusal(self):
        """Every check in this factory waits for the attach gate: a host with no sandbox keeps
        its ungrounded behaviour instead of learning about a pairing it never reached."""
        assert (
            make_codeact_tools(None, "data-analyst", _context(), withhold_guest_output=True) == []
        )


class TestWithholdingDeclaresTrustedIntegrity:
    """The declaration is the point of the feature. Withholding the text and still declaring
    nothing would leave the tracker's untrusted default in place — the result would go on
    tainting the conversation, which is the problem the option exists to solve."""

    def test_a_withholding_tool_declares_trusted(self):
        tool = _withholding_tool(_ScriptedSandbox())
        assert dict(tool.additional_properties or {})["source_integrity"] == "trusted"

    def test_the_declared_value_is_the_enums(self):
        tool = _withholding_tool(_ScriptedSandbox())
        assert dict(tool.additional_properties or {})["source_integrity"] == SourceIntegrity.TRUSTED

    def test_a_showing_tool_still_declares_nothing(self):
        """The default is unchanged, and that is the whole compatibility claim."""
        tool = _tool(_backend(capabilities=_PULLS), **_landing(CodeactOutputs.DECLARED))
        assert "source_integrity" not in dict(tool.additional_properties or {})

    def _with_registry(self, *tools: Callable[..., Any]):
        return _tool(
            _backend(capabilities=_CALLS),
            host_tools=_registry(*tools),
            **_landing(CodeactOutputs.DECLARED),
            withhold_guest_output=True,
        )

    def test_a_registry_of_trusted_sources_leaves_it_trusted(self):
        assert (
            dict(self._with_registry(_exchange_rate).additional_properties or {})[
                "source_integrity"
            ]
            == "trusted"
        )

    def test_an_unstamped_registry_takes_the_declaration_away(self):
        """`result_integrity` folds an unstamped tool to untrusted, and that fold is core's to
        make: withholding is about this kind's rendering, not about where a host tool's data
        came from."""
        registry = _registry(_unstamped_lookup)
        assert registry.aggregate().result_integrity is SourceIntegrity.UNTRUSTED
        tool = self._with_registry(_unstamped_lookup)
        assert "source_integrity" not in dict(tool.additional_properties or {})

    def test_a_sink_only_registry_keeps_it(self):
        """A tool that carries something out has no opinion about integrity coming in."""
        assert _registry(_log_to_crm).aggregate().result_integrity is None
        assert (
            dict(self._with_registry(_log_to_crm).additional_properties or {})["source_integrity"]
            == "trusted"
        )


class TestTheModelIsToldUpFront:
    """The description has to say printing does not come back, or a model writes its answer to
    a channel that discards it."""

    def test_the_description_says_the_printed_output_does_not_come_back(self):
        description = _callable(_withholding_tool(_ScriptedSandbox())).__doc__ or ""
        assert "does not come back" in description

    def test_the_description_does_not_promise_stdout(self):
        """The shown format's `Returns:` promises the program's stdout, which would be a lie
        here — and the lie a model would act on by printing its answer."""
        description = _callable(_withholding_tool(_ScriptedSandbox())).__doc__ or ""
        assert "The program's stdout, its stderr" not in description

    def test_the_shown_format_still_promises_it(self):
        description = _callable(_tool(_backend())).__doc__ or ""
        assert "The program's stdout" in description

    def test_it_does_not_promise_a_reference_to_where_a_file_landed(self):
        """Withheld, the result names the file and not its landing place, so the declared-output
        paragraph may not offer one."""
        description = _callable(_withholding_tool(_ScriptedSandbox())).__doc__ or ""

        assert "reference to where each one landed" not in description
        assert "names where each one landed" not in description

    def test_it_tells_the_model_a_failed_program_still_saves(self):
        """The recovery route this mode rests on. Told the shown rule — that a failed program
        saves nothing — a model would not write its diagnosis out and then fail, which is
        exactly the move withholding leaves it."""
        description = _callable(_withholding_tool(_ScriptedSandbox())).__doc__ or ""

        assert "still saves what it wrote" in description
        assert "A program that fails saves nothing at all" not in description

    def test_the_shown_format_still_states_the_opposite(self):
        tool = _tool(_backend(capabilities=_PULLS), **_landing(CodeactOutputs.DECLARED))
        description = _callable(tool).__doc__ or ""

        assert "A program that fails saves nothing at all" in description


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
        """Wiring a registry a backend cannot serve has to fail where the tool is *built* —
        not at the first call, and not silently. That is the promise the module docstring and
        the README both make, and this is where it is held.

        `_PULLS` is `{EXEC, FILES_IN, FILES_OUT}` — every file channel a host-tool call rides on,
        and `HOST_TOOLS` withheld. No shipped backend has that shape: docker and acas declare the
        capability, and wslc lacks `FILES_OUT` as well. Built rather than borrowed on purpose,
        so the refusal can only come from the one capability under test. It has to come from
        the capability match rather than from the registry being empty, so the registry here
        has a tool in it.
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
        """Nothing callable is nothing carried, whatever cap the host holds."""
        tool = _host_tool_calling_tool(_registry(), outbound_max_confidentiality="private")
        assert dict(tool.additional_properties or {}) == {}

    def test_an_opened_allowlist_makes_the_hosts_cap_apply(self):
        """A named host is a way out, so the flow the cap gates exists — and unlike the
        host-tool case this one the shared derivation *can* see, off `spec.egress_allow`.

        Opening egress changes what a host's policy engine reads from this tool, so the cap
        has to start applying without anyone wiring a sink or a registry.
        """
        tool = _tool(
            _backend(capabilities=_PULLS),
            egress_allow=("index.example",),
            outbound_max_confidentiality="private",
        )
        assert dict(tool.additional_properties or {}) == {"max_allowed_confidentiality": "private"}

    def test_a_closed_allowlist_leaves_it_unwritten(self):
        """The other side of the same bound: the default must not start declaring a flow."""
        tool = _tool(_backend(capabilities=_PULLS), outbound_max_confidentiality="private")
        assert dict(tool.additional_properties or {}) == {}

    def test_a_registry_with_no_sink_tool_leaves_the_cap_unwritten(self):
        """A source brings data *in* and pure computation carries nothing at all, so the flow
        the cap gates still does not exist — and a cap on a flow that cannot happen gates
        calls for nothing."""
        registry = _registry(_exchange_rate, _round_half_up)
        tool = _host_tool_calling_tool(registry, outbound_max_confidentiality="private")
        assert dict(tool.additional_properties or {}) == {}

    def test_a_sink_tool_makes_the_hosts_cap_apply_with_nothing_landing(self):
        """Egress is closed and no artifact lands, and the surface carries something out
        anyway — the one flow a derivation reading only the spec cannot see. What is written
        is the host's own cap, never the tool's sink value: the two are vocabularies this
        package refuses to order against each other."""
        tool = _host_tool_calling_tool(
            _registry(_log_to_crm), outbound_max_confidentiality="private"
        )
        assert dict(tool.additional_properties or {}) == {"max_allowed_confidentiality": "private"}

    def test_a_sink_tool_beside_an_output_sink_still_attaches(self):
        """`sandboxed_tool` refuses an explicit mapping together with a sink, and there is
        nothing to override anyway: a spec that lands already has the derivation writing the
        very same cap."""
        tool = _tool(
            _backend(capabilities=_CALLS),
            host_tools=_registry(_log_to_crm),
            outbound_max_confidentiality="private",
            **_landing(CodeactOutputs.DECLARED),
        )
        assert dict(tool.additional_properties or {}) == {"max_allowed_confidentiality": "private"}

    def test_an_unstamped_tool_carries_the_hosts_cap_as_a_sink_tool_would(self):
        """Nobody answered the sink question, so the fold sees no sink to write a cap from —
        and the guest can still call that function as a host tool with conversation-derived
        arguments.
        Every other undeclared leg fails safe (untrusted source, APP identity); so does this
        one, or a confidential conversation reaches `execute_code` ungated."""
        tool = _host_tool_calling_tool(
            _registry(_unstamped_lookup), outbound_max_confidentiality="private"
        )
        assert dict(tool.additional_properties or {}) == {"max_allowed_confidentiality": "private"}

    def test_no_source_integrity_is_declared_however_the_registry_is_stamped(self):
        """A registry of trusted lookups does not make a model-written `print(...)` trusted."""
        registry = _registry(_exchange_rate, _log_to_crm)
        tool = _host_tool_calling_tool(registry, outbound_max_confidentiality="private")
        assert "source_integrity" not in dict(tool.additional_properties or {})


class TestWhatARegistryDoesBeyondTheSpec:
    """The two things a host's registry decides here that its own `requires` does not say."""

    def test_a_user_identity_tool_gates_every_call_on_approval(self):
        """A host-tool call may exercise the caller's own delegated authority, and which call does
        is not knowable before the program runs — so one such tool raises the whole surface."""
        tool = _host_tool_calling_tool(
            _registry(
                _the_callers_calendar,
                allowed_identities=frozenset({Identity.APP, Identity.USER}),
            )
        )
        assert tool.approval_mode == "always_require"

    def test_the_applications_own_authority_does_not_gate_it(self):
        tool = _host_tool_calling_tool(_registry(_exchange_rate, _log_to_crm))
        assert tool.approval_mode == "never_require"

    @pytest.mark.parametrize("tools", [(), (_exchange_rate,)])
    def test_the_registry_is_sealed_once_the_factory_has_run(self, tools):
        """The empty one too: sealing costs nothing there, and it turns "registered a tool
        after the tool was built" into a refusal at the host's own `register` rather than a
        registration that quietly reaches nothing."""
        registry = _registry(*tools)
        _host_tool_calling_tool(registry)
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

    def test_an_opened_allowlist_replaces_the_no_network_claim_and_names_the_hosts(self):
        """Both halves matter. The model must not be told the network is absent when it is
        not, and it must be able to tell what it may reach without spending a call to find
        out — a program can enumerate the allowlist by trying it in any case."""
        described = self._description(egress_allow=("index.example",))

        assert "no network" not in described.lower(), described
        assert "index.example" in described, described
        # Nothing is listed below, so promising it sends the model looking for a channel that
        # does not exist. The pair is what discriminates: naming the hosts is not enough.
        assert "host tools" not in described.lower(), described

    def test_every_allowlisted_host_is_named_not_just_the_first(self):
        """A model told one of two reachable hosts wastes calls discovering the rest, and a
        rule that names only the first passes every single-host test — so this uses two."""
        described = self._description(egress_allow=("index.example", "artifacts.example"))

        assert "index.example" in described, described
        assert "artifacts.example" in described, described

    def test_the_description_follows_the_spec_rather_than_the_argument(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """It is read off the attached spec, so the kind's own half is described too — a model
        told only what the deployment added would understate what the sandbox can reach."""
        monkeypatch.setattr("maf_sandbox_codeact._tool._KIND_EGRESS", ("modules.example",))

        described = self._description()

        assert "modules.example" in described, described

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
        described = _callable(_host_tool_calling_tool(registry)).__doc__ or ""
        assert "_exchange_rate" in described
        assert "_log_to_crm" in described
        assert "_round_half_up" in described

    def test_host_tools_and_an_allowlist_are_both_named(self):
        """Either one alone reads as the only way out of the sandbox, and with both wired
        neither is — the claim that names only one understates what can leave."""
        described = (
            _callable(
                _host_tool_calling_tool(_registry(_round_half_up), egress_allow=("index.example",))
            ).__doc__
            or ""
        )

        assert "index.example" in described, described
        assert "host tools" in described.lower(), described
        assert "no network" not in described.lower(), described

    def test_an_empty_registry_reads_exactly_like_no_registry_at_all(self):
        plain = _callable(_tool(_backend(capabilities=_PULLS))).__doc__ or ""
        with_empty_registry = _callable(_host_tool_calling_tool(_registry())).__doc__ or ""
        assert with_empty_registry == plain
        assert "maf_host_tools" not in with_empty_registry

    def test_the_network_claim_is_qualified_only_once_a_tool_is_registered(self):
        without = _callable(_tool(_backend(capabilities=_PULLS))).__doc__ or ""
        described = _callable(_host_tool_calling_tool(_registry(_exchange_rate))).__doc__ or ""
        assert "no network access" in without
        assert "no network access" not in described
        assert "no network of its own" in described
        assert "no network of its own" not in without

    def test_the_call_form_matches_what_the_shim_actually_generates(self):
        """The syntax the model is told to write must be the syntax the generated shim
        module accepts — checked against the real generated source, not a copy of it."""
        registry = _registry(_exchange_rate)
        described = _callable(_host_tool_calling_tool(registry)).__doc__ or ""
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
        described = _callable(_host_tool_calling_tool(_registry(_exchange_rate))).__doc__ or ""

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
            for path, content in sandbox.written_files.items()
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
        assert sandbox.written_files == {}

    @pytest.mark.parametrize("name", ["../../etc/passwd", "/etc/passwd", "a/../../b"])
    def test_a_traversing_name_is_refused_without_echoing_the_listing(self, name: str):
        """Echoing it would invite a retry with another spelling."""
        sandbox = _ScriptedSandbox()
        store = InMemoryStore({name: "x", "data/sales.csv": "y"})
        tool = _tool(_backend(sandbox), file_store=store)

        out = _run(tool, "print('hi')", files=[name])
        assert "cannot be shared" in out
        assert "data/sales.csv" not in out
        assert sandbox.written_files == {}

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
        assert sandbox.written_files == {}

    def test_a_traversing_name_under_a_reserved_one_gets_the_validators_sentence(self):
        """`program.py/../x` climbs back out, so nothing is living inside anything and the
        nested-name sentence would be false. Only the validator running first keeps it true."""
        sandbox = _ScriptedSandbox()
        name = f"{_PROGRAM_FILENAME}/../x"
        tool = _tool(_backend(sandbox), file_store=InMemoryStore({name: "x"}))

        out = _run(tool, "print('hi')", files=[name])
        assert "cannot be shared" in out
        assert "nothing can live inside it" not in out, out
        assert sandbox.written_files == {}

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
        call — at any depth, which is why the rule is a prefix test and not a parent test."""
        sandbox = _ScriptedSandbox()
        tool = _tool(_backend(sandbox), file_store=InMemoryStore({nested: "x"}))

        out = _run(tool, "print('hi')", files=[nested])
        assert out == (
            f"Error: {nested!r} cannot be shared — {_PROGRAM_FILENAME!r} is a file name this "
            f"tool reserves in every run's directory, so nothing can live inside it."
        ), out
        assert sandbox.written == {}

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
        invites a retry once the file is stored, which is a retry neither name can survive."""
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
        assert f"{_run_dirs(sandbox)[0]}/{name}" in sandbox.written_files

    def test_the_two_reserved_names_are_refused_for_their_own_reasons(self):
        """One sentence for both would be false about one of them. This tool writes
        `program.py` into the run's directory and only *reads* `outputs.json` from it — the
        program writes that. Both refusals are asserted whole, so neither can drift onto the
        other's clause and tell a model this tool writes a file it never touches.
        """
        writes = _run(
            _tool(_backend(_ScriptedSandbox()), file_store=InMemoryStore({_PROGRAM_FILENAME: "x"})),
            "print('hi')",
            files=[_PROGRAM_FILENAME],
        )
        reads = _run(
            _pulling_tool(
                _ProducingSandbox(),
                CodeactOutputs.MANIFEST,
                _RecordingSink(),
                file_store=InMemoryStore({_MANIFEST_FILENAME: "x"}),
            ),
            "print('hi')",
            files=[_MANIFEST_FILENAME],
        )

        assert writes == (
            f"Error: {_PROGRAM_FILENAME!r} cannot be shared — this tool writes a file of that "
            f"name into every run's directory."
        ), writes
        assert reads == (
            f"Error: {_MANIFEST_FILENAME!r} cannot be shared — this tool reads a file of that "
            f"name from every run's directory as its manifest."
        ), reads

    def test_a_name_that_merely_starts_with_the_program_name_is_fine(self):
        """`program.py.bak` shares no directory with it, so refusing it would be overreach."""
        sandbox = _ScriptedSandbox()
        store = InMemoryStore({f"{_PROGRAM_FILENAME}.bak": "x"})
        tool = _tool(_backend(sandbox), file_store=store)

        _run(tool, "print('hi')", files=[f"{_PROGRAM_FILENAME}.bak"])
        run_dir = _run_dirs(sandbox)[0]
        assert f"{run_dir}/{_PROGRAM_FILENAME}.bak" in sandbox.written_files

    def test_the_program_file_cannot_be_shadowed_by_a_shared_file(self):
        sandbox = _ScriptedSandbox()
        store = InMemoryStore({_PROGRAM_FILENAME: "print('theirs')"})
        tool = _tool(_backend(sandbox), file_store=store)

        out = _run(tool, "print('mine')", files=[_PROGRAM_FILENAME])
        assert "cannot be shared" in out
        assert sandbox.written_files == {}

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
        assert f"{second}/a.csv" not in sandbox.written_files
        assert f"{second}/b.csv" in sandbox.written_files

    def test_a_listed_file_with_no_content_is_reported_rather_than_written_as_none(self):
        """A store read can miss without raising — the file was listed, then removed. Writing
        `None` through would put the string "None" into the sandbox for the program to parse."""
        sandbox = _ScriptedSandbox()
        tool = _tool(_backend(sandbox), file_store=_ListedButGoneStore("gone.csv"))

        out = _run(tool, "print('hi')", files=["gone.csv"])
        assert "no content" in out
        assert sandbox.written == {}

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
        # Unqualified: with nothing callable that list is everything that would cross.
        assert "writes at most 2 per call" in out
        assert sandbox.written == {}

    def test_a_file_over_the_per_file_ceiling_is_refused(self):
        sandbox = _ScriptedSandbox()
        store = InMemoryStore({"big.csv": "x" * 100})
        tool = self._tool(
            sandbox, store, files_in=replace(DEFAULT_TRANSFER_LIMITS, max_bytes_per_file=10)
        )

        out = _run(tool, "print(1)", files=["big.csv"])
        assert "at most 10 bytes per file" in out
        assert sandbox.written == {}

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
        assert sandbox.written == {}

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
        assert {f"{run_dir}/a.csv", f"{run_dir}/b.csv"} <= set(sandbox.written_files)

    def test_the_program_itself_counts_against_the_file_count(self):
        """The spec requires `FILES_IN` even with no store, because `program.py` crosses this
        boundary too — so a tally that skipped it let `max_files=1` write two files."""
        sandbox = _ScriptedSandbox()
        store = InMemoryStore({"a.csv": "1"})
        tool = self._tool(sandbox, store, files_in=replace(DEFAULT_TRANSFER_LIMITS, max_files=1))

        out = _run(tool, "print(1)", files=["a.csv"])
        assert "at most 1" in out
        assert sandbox.written == {}

    def test_an_over_count_call_reads_nothing_from_the_store(self):
        """A count cap that answers only once every requested file is in memory has already
        spent what it exists to bound."""
        sandbox = _ScriptedSandbox()
        store = _CountingStore({f"f{i}.csv": "x" for i in range(10)})
        tool = self._tool(sandbox, store, files_in=replace(DEFAULT_TRANSFER_LIMITS, max_files=3))

        out = _run(tool, "print(1)", files=sorted(store.files))
        assert "at most 3" in out
        assert store.reads == []
        assert sandbox.written == {}

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
        assert sandbox.written == {}

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
        assert sandbox.written == {}

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
            (["Program.py", "program.py"], "this tool writes a file of that name"),
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

    def test_the_work_subdirectory_counts_toward_the_ceiling_when_a_run_calls_a_host_tool(self):
        """A host-tool-calling run keeps the model's files one level deeper, so the prefix a
        declared name is judged against is `<run>/work/` — five bytes more than `<run>/`.

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
        assert "this tool writes a file of that name into every run's directory" in out, out

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
        assert "a file of that name" not in out, out
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

    def test_the_manifest_is_read_from_the_work_directory_when_a_run_calls_a_host_tool(self):
        """`_read_manifest` stats a path built from the same prefix everything else uses, so a
        host-tool-calling run must look in `work/` rather than in the run directory.

        MANIFEST is the one output mode whose names arrive after the program has run, and the
        stat that fetches them is the only place the prefix is used for a read. Get it wrong
        and the run answers "no outputs.json was written" — an empty collection reported as
        success, after a program that produced everything it promised.
        """
        sandbox = _FinishingSandbox()
        sink = _RecordingSink()
        tool = _tool(
            _backend(sandbox, capabilities=_CALLS),
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
        assert f"{run_dir}/{WORK_DIRECTORY}/{_MANIFEST_FILENAME}" in sandbox.written
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
        assert f"{_MANIFEST_FILENAME!r} is a file name this tool reserves" in out, out
        assert "a file of that name" not in out, out
        assert "nothing can live inside it" in out, out
        assert sink.names == []

    @pytest.mark.parametrize(
        ("name", "sentence"),
        [
            (_MANIFEST_FILENAME, "this tool reads a file of that name"),
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
        assert sandbox.written == {}

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


def _calling_tool(sandbox: InProcessSandbox, *tools: Callable[..., Any], **kw: Any):
    """The tool for a registry serving `tools`, over a sandbox that can serve the transport."""
    return _tool(_backend(sandbox, capabilities=_CALLS), host_tools=_registry(*tools), **kw)


class TestAProgramThatCallsOut:
    def test_the_registry_answers_and_the_program_reads_what_it_said(self):
        """End to end over the transport: the request the guest wrote reaches the registered
        function, its arguments arrive, and its return value is what the program prints."""
        sandbox = _CallingSandbox("_round_half_up", {"value": 3.6})
        out = _run(_calling_tool(sandbox, _round_half_up), "print(_round_half_up(value=3.6))")

        assert sandbox.answers == [{"value": 4}]
        assert out == "stdout:\nthe host said 4"

    def test_each_call_is_served_under_its_own_run_directory(self):
        """`acquire` is get-or-create, so a second call must not find the first one's requests,
        its answers or its exit marker sitting where the supervisor polls."""
        sandbox = _CallingSandbox("_round_half_up", {"value": 0.5})
        tool = _calling_tool(sandbox, _round_half_up)
        _run(tool, "print(1)")
        _run(tool, "print(2)")

        first, second = sandbox.layouts
        assert first.directory != second.directory
        assert [first.directory, second.directory] == _run_dirs(sandbox)
        assert len(sandbox.answers) == 2

    def test_the_host_tool_call_cap_bounds_one_call_rather_than_the_conversation(self):
        """The cap bounds what one program may cost, so a run that spends it all must leave the
        next call as much — not retire `execute_code` for the rest of the conversation."""
        sandbox = _CallingSandbox("_round_half_up", {"value": 3.6})
        registry = _registry(_round_half_up, max_host_tool_calls_per_run=1)
        tool = _tool(_backend(sandbox, capabilities=_CALLS), host_tools=registry)
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
        _run(_calling_tool(sandbox, _round_half_up), "print('hi')")

        (layout,) = sandbox.layouts
        assert sandbox.written_files.get(layout.program) == "print('hi')", sorted(
            sandbox.written_files
        )
        assert f"{layout.directory}/{_PROGRAM_FILENAME}" not in sandbox.written_files
        assert f"{layout.work}/{_PROGRAM_FILENAME}" not in sandbox.written_files

    def test_the_shim_is_written_beside_the_program_with_the_runs_own_patience(self):
        """A guest that gives up before the supervisor does is wrong twice over: the host-tool
        call it asked for goes on to act while the program has been told nobody answered."""
        sandbox = _CallingSandbox("_round_half_up", {"value": 0.5})
        _run(_calling_tool(sandbox, _round_half_up, exec_timeout_seconds=97), "print(1)")

        (layout,) = sandbox.layouts
        assert sandbox.written_files[layout.shim] == host_tool_shim(
            frozenset({"_round_half_up"}), call_timeout=97
        )

    def test_both_paths_run_the_program_under_this_kinds_own_interpreter(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """The transport carries a default of its own, so a host-tool-calling run that leaves it
        out is running under a constant this kind does not own and cannot change."""
        monkeypatch.setattr("maf_sandbox_codeact._tool._INTERPRETER", "pypy3")

        plain = _ScriptedSandbox()
        _run(_tool(_backend(plain)), "print(1)")
        assert plain.commands[0][0].startswith("pypy3 ")

        sandbox = _CallingSandbox("_round_half_up", {"value": 0.5})
        _run(_calling_tool(sandbox, _round_half_up), "print(1)")

        (layout,) = sandbox.layouts
        assert "pypy3" in sandbox.written_files[layout.launcher]
        assert "python3" not in sandbox.written_files[layout.launcher]


class _StallingSandbox(_ScriptedSandbox):
    """A guest served over the host-tool-call transport that prints and then never records an
    exit marker.

    What a wedged program looks like from the supervisor's side: the launcher returns, output
    accumulates, and the marker the run is waiting for never lands.
    """

    def __init__(self, printed: bytes = b"step 1 done", **kwargs) -> None:
        super().__init__(**kwargs)
        self.printed = printed

    async def exec(self, command, *, working_directory, timeout):
        result = await super().exec(command, working_directory=working_directory, timeout=timeout)
        if str(command).startswith("kill") or _is_core_removal(command):
            # Neither starts a program, so neither is a run this fake should record.
            return result
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
    #: `exec_timeout_seconds` on a host-tool-calling tool, so one second is the shortest bound that
    #: reaches this path at all, and this has to outlast it.
    _SLOWER_THAN_THE_RUN = 1.2

    async def write_file(self, path: str, content: str | bytes, *, working_directory: str) -> None:
        await super().write_file(path, content, working_directory=working_directory)
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
    """`TimeoutError` means two unrelated things on the host-tool-call path, and only one of them
    is the program running out. Collapsing them tells the model to rewrite code that was fine."""

    def test_a_backends_own_timeout_is_not_blamed_on_the_program(self):
        sandbox = _StatTimingOutSandbox()
        out = _run(_calling_tool(sandbox, _round_half_up, exec_timeout_seconds=600), "print('hi')")

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
        out = _run(_calling_tool(sandbox, _round_half_up, exec_timeout_seconds=1), "print('x')")

        assert "did not finish within 1s" in out, out
        assert "step 2 done" in out, "the partial output the transport paid to read was dropped"

    def test_a_run_that_expires_before_the_program_starts_does_not_blame_the_program(self):
        """`SandboxProgramTimeout` covers the launcher upload too, where nothing ran.

        Telling a model its program timed out sends it rewriting code that never executed, and
        the distinction exists nowhere but the transport's message — the exception type is the
        same and `output` is empty either way.
        """
        sandbox = _SlowToTakeTheLauncherSandbox()
        out = _run(_calling_tool(sandbox, _round_half_up, exec_timeout_seconds=1), "print('x')")

        assert "before the program was started" in out, out
        assert "did not finish" not in out, "a run that never started was reported as overrunning"

    def test_the_log_adds_no_claim_about_what_became_of_the_program(self, caplog):
        """The transport says which of three fates the program met; the kind must not overrule it.

        A blanket "it is still running" was false whenever the signal landed, and it sat on the
        same line as the sentence that said so.
        """
        sandbox = _StallingSandbox(printed=b"step 1 done")
        with caplog.at_level(logging.WARNING, logger="maf_sandbox_codeact._tool"):
            out = _run(_calling_tool(sandbox, _round_half_up, exec_timeout_seconds=1), "print('x')")

        # The kind's own records only: the framework logs what it did about the sandbox
        # through the same logger, and that is its claim to make, not this kind's.
        logged = [
            r.getMessage()
            for r in caplog.records
            if r.getMessage().startswith("execute_") and r.filename == "_tool.py"
        ]
        assert logged == [f"execute_code: {out.removeprefix('Error: ')}"], logged

    def test_the_plain_path_still_reads_a_timeout_as_the_programs_own(self):
        """No host-tool call, one `exec`, one bound: the equation this class complicates holds
        here."""
        sandbox = _ScriptedSandbox(raises=TimeoutError())
        out = _run(_tool(_backend(sandbox), exec_timeout_seconds=7), "print('hi')")

        assert out == "Error: the program timed out after 7s"


class TestOnTheTransportStderrIsTheHosts:
    """The launcher merges the guest's stderr into its output file, so on that transport
    `ExecResult.stderr` is the *host's* field and carries its note about the run.

    Reducing it to a byte count reports the one thing the note exists to prevent: a program
    whose output was dropped for its size reads back as one that printed nothing.
    """

    def test_the_hosts_note_is_surfaced_whole(self):
        note = "the program's output was larger than the host will read and was not returned"
        out = _format_withheld(ExecResult(stdout="", stderr=note), over_transport=True)

        assert f"note: {note}" in out
        assert "exit code: 0" in out

    def test_the_merged_stream_is_one_count_not_two(self):
        """Naming `stderr` there would tell a model its stderr write vanished."""
        out = _format_withheld(ExecResult(stdout="a\nb", stderr=""), over_transport=True)

        assert "output: 3 bytes" in out
        assert "stdout:" not in out
        assert "stderr:" not in out

    def test_a_run_with_no_note_says_nothing_in_its_place(self):
        out = _format_withheld(ExecResult(stdout="hi", stderr=""), over_transport=True)

        assert "note:" not in out

    def test_the_plain_path_keeps_both_counts(self):
        """Off the transport `stderr` is the program's own, and is withheld like `stdout`."""
        out = _format_withheld(ExecResult(stdout="a", stderr="boom"), over_transport=False)

        assert "stdout: 1 bytes" in out
        assert "stderr: 4 bytes" in out

    def test_a_wired_registry_selects_the_transport_rendering(self):
        """The wiring, not just the renderer: a tool built with host tools has to pass it."""
        sandbox = _CallingSandbox("_round_half_up", {"value": 3.6})
        out = _run(
            _calling_tool(
                sandbox,
                _round_half_up,
                **_landing(CodeactOutputs.DECLARED),
                withhold_guest_output=True,
            ),
            "print(_round_half_up(value=3.6))",
        )

        assert "output:" in out, out
        assert "the host said 4" not in out

    def test_the_description_names_one_stream_where_the_transport_merges_them(self):
        sandbox = _CallingSandbox("_round_half_up", {"value": 3.6})
        tool = _calling_tool(
            sandbox,
            _round_half_up,
            **_landing(CodeactOutputs.DECLARED),
            withhold_guest_output=True,
        )
        description = _callable(tool).__doc__ or ""

        assert "and stderr together" in description
        assert "``note`` line is the host's" in description
        assert "How many bytes of stdout and of stderr" not in description


class TestAWithheldTimeoutQuotesNothing:
    """A withheld timeout carries what the exception proves, and nothing the program printed.

    `SandboxProgramTimeout` holds that output in its message rather than only in `output`, so
    the sentence is rebuilt from the attributes — which is also why it names no bound: whose
    expired is not knowable from the public type.
    """

    def _withholding_calling_tool(self, sandbox: InProcessSandbox, **kw: Any):
        return _calling_tool(
            sandbox,
            _round_half_up,
            **_landing(CodeactOutputs.DECLARED),
            withhold_guest_output=True,
            **kw,
        )

    def test_the_partial_output_the_transport_read_is_not_quoted(self):
        sandbox = _StallingSandbox(printed=b"step 1 done\nstep 2 done")
        out = _run(self._withholding_calling_tool(sandbox, exec_timeout_seconds=1), "print('x')")

        assert "step 1 done" not in out, "the program's own output rode out on the timeout"
        assert "step 2 done" not in out

    def test_it_says_the_program_did_not_finish_without_naming_whose_bound(self):
        """A backend may raise the public `SandboxProgramTimeout` from a call of its own and the
        transport propagates it untranslated, so on the transport too the origin is unknown —
        the subtype that would settle it is core's private one."""
        sandbox = _StallingSandbox(printed=b"step 1 done")
        out = _run(self._withholding_calling_tool(sandbox, exec_timeout_seconds=1), "print('x')")

        assert "did not finish in the time it was given" in out, out
        assert "1s" not in out, "a bound this kind cannot attribute was named anyway"
        assert "the run's" not in out

    def test_it_still_names_the_route(self):
        sandbox = _StallingSandbox(printed=b"step 1 done")
        out = _run(self._withholding_calling_tool(sandbox, exec_timeout_seconds=1), "print('x')")

        assert "declared output" in out

    def test_a_run_that_expired_before_the_program_started_still_says_so(self):
        """Rebuilt from `signal`, which the transport documents as the thing to branch on —
        the prose that carries the same distinction is what may not be quoted here."""
        sandbox = _SlowToTakeTheLauncherSandbox()
        out = _run(self._withholding_calling_tool(sandbox, exec_timeout_seconds=1), "print('x')")

        assert "before the program was started" in out, out

    def test_a_run_that_may_have_started_claims_neither_way(self):
        """Only `"absent"` asserts a program never ran; every other fate is a degree of not
        knowing, so the sentence must not turn one into a claim that it did."""
        sandbox = _StallingSandbox(printed=b"step 1 done")
        out = _run(self._withholding_calling_tool(sandbox, exec_timeout_seconds=1), "print('x')")

        assert "before the program was started" not in out

    def test_off_the_transport_it_names_neither_a_run_nor_a_bound(self):
        """`SandboxProgramTimeout` is public and a backend may raise one from a call of its
        own, whose bound is not the number handed to `exec` — and a call with no host tool has
        no *run* to attribute it to either."""
        sandbox = _ScriptedSandbox(
            raises=SandboxProgramTimeout("the backend's own 5s call bound expired")
        )
        tool = _withholding_tool(sandbox, exec_timeout_seconds=90)
        out = _run(tool, "print('hi')")

        assert "90s" not in out, "this kind's bound was reported as the one that expired"
        assert "the run's" not in out, "a call with no host tool has no run"
        assert "did not finish" in out
        assert "declared output" in out

    def test_a_bare_timeout_names_the_bound_and_the_route(self):
        """The path every shipped backend takes: acas, docker and wslc all raise a bare
        `TimeoutError` from plain `exec`, so this is the withheld timeout a host actually
        meets. One `exec` and one bound here, so unlike the transport it may name it."""
        sandbox = _ScriptedSandbox(raises=TimeoutError())
        out = _run(_withholding_tool(sandbox, exec_timeout_seconds=7), "print('hi')")

        assert out == f"Error: the program timed out after 7s. {_WITHHELD_ROUTE}"

    def test_the_shown_bare_timeout_is_unchanged(self):
        sandbox = _ScriptedSandbox(raises=TimeoutError())
        out = _run(_tool(_backend(sandbox), exec_timeout_seconds=7), "print('hi')")

        assert out == "Error: the program timed out after 7s"

    def test_off_the_transport_it_quotes_no_part_of_the_message(self):
        sandbox = _ScriptedSandbox(
            raises=SandboxProgramTimeout("gave up — the program had printed: the secret is 42")
        )
        out = _run(_withholding_tool(sandbox), "print('hi')")

        assert "the secret is 42" not in out


class TestOnlyAnAttachedToolSealsTheRegistry:
    def test_an_unconfigured_hosts_registry_can_still_be_widened(self):
        """Nothing is grounded on a host with no sandbox — no spec, no classification — so a
        later `register` has nothing to contradict and must not be refused as if it had."""
        registry = _registry(_round_half_up)
        assert make_codeact_tools(None, "data-analyst", _context(), host_tools=registry) == []

        registry.register(_exchange_rate)
        assert registry.names() == frozenset({"_round_half_up", "_exchange_rate"})


#: Every name the transport writes into a host-tool-calling run, and one nested beneath each of the
#: two that are files: the nested rule reads the same per-mode set as the exact one, so both
#: have to fall silent for these.
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
    """A host-tool-calling run puts the transport's files in `host_tools/` and the model's in
    `work/`, so a name that would collide is written instead of refused.

    Two directories are the guarantee, so what pins it is that these names land — not that
    some list still enumerates them.
    """

    @pytest.mark.parametrize("name", _TRANSPORT_NAMES)
    def test_a_shared_file_may_take_a_name_the_transport_uses(self, name: str):
        sandbox = _FinishingSandbox()
        tool = _tool(
            _backend(sandbox, capabilities=_CALLS),
            host_tools=_registry(_round_half_up),
            file_store=InMemoryStore({name: "x"}),
            exec_timeout_seconds=5,
        )

        out = _run(tool, "print(1)", files=[name])

        assert "cannot be shared" not in out, out
        (run_dir,) = _run_dirs(sandbox)
        assert f"{run_dir}/{WORK_DIRECTORY}/{name}" in sandbox.written_files, sorted(
            sandbox.written_files
        )

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
            _backend(sandbox, capabilities=_CALLS),
            host_tools=_registry(_round_half_up),
            file_store=InMemoryStore({"data.csv": "a,b\n"}),
        )
        _run(tool, "print(1)", files=["data.csv"])

        (layout,) = sandbox.layouts
        written = set(sandbox.written)
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
            _host_tool_calling_tool(_registry(_round_half_up), exec_timeout_seconds=seconds)

        assert str(refused.value).startswith(f"{EXECUTE_CODE_TOOL_NAME}: exec_timeout_seconds")
        # `call_timeout` is the shim generator's parameter, which this factory does not expose.
        assert "call_timeout" not in str(refused.value)

    def test_a_host_with_nothing_callable_keeps_tolerating_one(self):
        """With no shim to generate the number only ever reaches `exec`, and this factory has
        never had an opinion about it."""
        _tool(_backend(), exec_timeout_seconds=0)
        _host_tool_calling_tool(_registry(), exec_timeout_seconds=0)


def _neighbouring(calls_a_host_tool: bool, **kw: Any):
    """The tool and its sandbox for the tests below, calling a host tool or not.

    Both halves run because the two put the model's files in different places — the run
    directory flat, or its `work` subdirectory — while the rule under test is the same one.
    """
    sandbox = _FinishingSandbox() if calls_a_host_tool else _ProducingSandbox()
    tool = _tool(
        _backend(sandbox, capabilities=_CALLS if calls_a_host_tool else _PULLS),
        exec_timeout_seconds=1,
        **({"host_tools": _registry(_round_half_up)} if calls_a_host_tool else {}),
        **kw,
    )
    return tool, sandbox


def _model_dir(sandbox: _ScriptedSandbox, calls_a_host_tool: bool) -> str:
    """Where this run put the files a model named: the work subdirectory, or the run directory.

    Derived from `calls_a_host_tool` rather than from what the sandbox happens to contain, so a
    regression that writes them to the wrong directory fails here instead of being read back
    from wherever it wrote them.
    """
    run_dir = _run_dirs(sandbox)[0]
    return f"{run_dir}/{WORK_DIRECTORY}" if calls_a_host_tool else run_dir


@pytest.mark.parametrize(
    "calls_a_host_tool", [False, True], ids=["no registry", "host tool call armed"]
)
class TestANeighbourOfTheProgramsNameIsNotTheProgram:
    """`program.py` is exec'd by path, so neither a `program/` directory nor a `program.*`
    sibling displaces it, and both are names the `files` channel documents. The reserved-name
    rule is a prefix test, which is the shape that over-reaches onto these if written
    carelessly."""

    def test_a_sibling_of_the_programs_name_is_shared(self, calls_a_host_tool: bool):
        tool, sandbox = _neighbouring(
            calls_a_host_tool, file_store=InMemoryStore({"program.csv": "a,b\n"})
        )

        out = _run(tool, "print('hi')", files=["program.csv"])
        assert "cannot be shared" not in out
        assert f"{_model_dir(sandbox, calls_a_host_tool)}/program.csv" in sandbox.written_files

    def test_a_nested_input_under_the_programs_name_is_shared(self, calls_a_host_tool: bool):
        store = InMemoryStore({"program/train.py": "x = 1\n"})
        tool, sandbox = _neighbouring(calls_a_host_tool, file_store=store)

        out = _run(tool, "print('hi')", files=["program/train.py"])
        assert "cannot be shared" not in out
        assert f"{_model_dir(sandbox, calls_a_host_tool)}/program/train.py" in sandbox.written_files

    @pytest.mark.parametrize("name", ["Program.py", "Program.py/x.csv"])
    def test_a_case_variant_of_the_programs_name_is_shared(
        self, calls_a_host_tool: bool, name: str
    ):
        """The guest filesystem is POSIX, where `Program.py` and `program.py` are two files, so
        the rule matches exactly. Case-folding either comparison refuses a legal name."""
        tool, sandbox = _neighbouring(calls_a_host_tool, file_store=InMemoryStore({name: "x"}))

        out = _run(tool, "print('hi')", files=[name])
        assert "cannot be shared" not in out, out
        assert f"{_model_dir(sandbox, calls_a_host_tool)}/{name}" in sandbox.written_files

    @pytest.mark.parametrize("name", ["Program.py", "Program.py/x.csv"])
    def test_a_case_variant_of_the_programs_name_is_saved(self, calls_a_host_tool: bool, name: str):
        sink = _RecordingSink()
        tool, sandbox = _neighbouring(calls_a_host_tool, **_landing(CodeactOutputs.DECLARED, sink))

        out = _run_producing(tool, sandbox, {name: b"a,b\n"}, outputs=[name])
        assert sink.names == [name]
        assert "cannot be saved" not in out

    def test_the_programs_name_deeper_in_a_path_is_shared(self, calls_a_host_tool: bool):
        """The rule is about the first segment. `data/program.py/notes.txt` displaces nothing,
        and a containment test written in place of the prefix test refuses it."""
        store = InMemoryStore({"data/program.py/notes.txt": "x"})
        tool, sandbox = _neighbouring(calls_a_host_tool, file_store=store)

        out = _run(tool, "print('hi')", files=["data/program.py/notes.txt"])
        assert "cannot be shared" not in out, out
        assert (
            f"{_model_dir(sandbox, calls_a_host_tool)}/data/program.py/notes.txt"
            in sandbox.written_files
        )

    def test_a_nested_output_under_it_is_saved(self, calls_a_host_tool: bool):
        sink = _RecordingSink()
        tool, sandbox = _neighbouring(calls_a_host_tool, **_landing(CodeactOutputs.DECLARED, sink))

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
        assert f"{_run_dirs(plain)[0]}/a.csv" in plain.written_files

        sandbox = _ScriptedSandbox()
        tool = _tool(
            _backend(sandbox, capabilities=_CALLS),
            host_tools=_registry(_round_half_up),
            file_store=store,
            files_in=limits,
        )
        out = _run(tool, "print(1)", files=["a.csv"])
        assert "3 files would be written" in out
        assert "your program, the host-tool module beside it, and 1 shared" in out
        # Qualified here and nowhere else: the launcher crosses too and is not in that list.
        assert "writes at most 2 of those per call" in out
        assert sandbox.written == {}

    def test_it_counts_against_the_inbound_byte_ceilings(self):
        """Kilobytes of generated source, against a total with room for the module alone and
        not for the program beside it.  The kind's runtime tally enforces against the workload's
        own files_in — the router's host-tool-call fold is transient and never reaches here
        (#393)."""
        module = len(host_tool_shim(frozenset({"_round_half_up"}), call_timeout=97).encode())
        limits = replace(DEFAULT_TRANSFER_LIMITS, max_total_bytes=module + 5)

        plain = _ScriptedSandbox()
        _run(_tool(_backend(plain), files_in=limits), "print(1)")
        assert plain.written_files, "the same program did not fit without a registry"

        sandbox = _ScriptedSandbox()
        tool = _tool(
            _backend(sandbox, capabilities=_CALLS),
            host_tools=_registry(_round_half_up),
            files_in=limits,
            exec_timeout_seconds=97,
        )
        assert f"at most {module + 5} per call" in _run(tool, "print(1)")
        assert sandbox.written == {}

    def test_room_for_one_inbound_file_is_refused_at_the_factory(self):
        """Two files cross on every call, so a cap of one could never serve a single call — and
        a tool the model can see and never use successfully is worse than one that never
        attached."""
        with pytest.raises(ValueError, match="host-tool module"):
            _host_tool_calling_tool(
                _registry(_round_half_up), files_in=replace(DEFAULT_TRANSFER_LIMITS, max_files=1)
            )

    @pytest.mark.parametrize("cap", ["max_bytes_per_file", "max_total_bytes"])
    def test_a_byte_cap_below_the_module_is_refused_at_the_factory_too(self, cap: str):
        """Its size is settled before anything attaches, so a ceiling under it is the same
        never-usable tool the count check refuses — reached by the other leg."""
        module = len(host_tool_shim(frozenset({"_round_half_up"}), call_timeout=97).encode())
        with pytest.raises(ValueError, match="host-tool module is"):
            _host_tool_calling_tool(
                _registry(_round_half_up),
                files_in=replace(DEFAULT_TRANSFER_LIMITS, **{cap: module - 1}),
                exec_timeout_seconds=97,
            )

    def test_a_registry_holding_nothing_is_refused_nothing(self):
        """Nothing callable is no shim, exactly as it is no capability in the spec."""
        _host_tool_calling_tool(_registry(), files_in=replace(DEFAULT_TRANSFER_LIMITS, max_files=1))


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
        assert set(sandbox.written) == {f"{run_dir}/{_PROGRAM_FILENAME}"}


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


class TestTheSpecCarriesItsHostToolSurface:
    """The router folds the transport's traffic only for a spec whose ``host_tools`` is set."""

    def test_the_spec_carries_the_surface_it_derived(self):
        spec = codeact_sandbox_spec(host_tools=_registry(_exchange_rate))
        assert spec.host_tools is not None
        assert spec.host_tools.identities == frozenset({Identity.APP})

    def test_a_spec_without_a_registry_carries_nothing(self):
        assert codeact_sandbox_spec().host_tools is None

    def test_a_backend_that_cannot_serve_the_transport_is_refused_at_attach(self):
        """The workload's own declaration fits this backend; only the folded one does not."""
        spec = codeact_sandbox_spec(host_tools=_registry(_exchange_rate))
        ceiling = replace(_CALL_LIMITS.files_in, max_files=spec.files_in.max_files)
        backend = _backend(capabilities=_CALLS, limits=replace(_CALL_LIMITS, files_in=ceiling))
        router = SandboxRouter([backend], min_isolation=backend.isolation)
        with pytest.raises(SandboxTransferLimitsNotPermitted) as refusal:
            router.ensure_can_serve(spec)
        # The note says the fold caused this, not the workload's own caps.
        assert "folded to include the wired host tools" in str(refusal.value)

    def test_a_backend_that_can_serve_the_transport_attaches(self):
        spec = codeact_sandbox_spec(host_tools=_registry(_exchange_rate))
        backend = _backend(capabilities=_CALLS)
        SandboxRouter([backend], min_isolation=backend.isolation).ensure_can_serve(spec)
