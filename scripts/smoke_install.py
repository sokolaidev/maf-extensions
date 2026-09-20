"""Prove a built wheel is installable and usable, from outside this repository.

Run against an installed distribution, never against the source tree — that is the entire
point. Inside the workspace every package resolves whether or not its manifest says so,
every file is present whether or not the build backend included it, and every module
imports whether or not it declared its dependency. None of that is true for the person
who runs ``pip install``.

    python scripts/smoke_install.py <package-name>

Exits non-zero with a specific message on the first failure. Deliberately importable-free
of this repository: it is executed inside a throwaway virtual environment where only the
built wheel and its dependencies exist.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import pathlib
import subprocess
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # never imported at run time: only the named distribution is installed
    from maf_sandbox.testing import InProcessSandbox

_PACKAGES = {
    "maf-sandbox": "maf_sandbox",
    "maf-sandbox-acas": "maf_sandbox_acas",
    "maf-sandbox-bicep": "maf_sandbox_bicep",
    "maf-sandbox-codeact": "maf_sandbox_codeact",
    "maf-sandbox-deepagents": "maf_sandbox_deepagents",
    "maf-sandbox-docker": "maf_sandbox_docker",
    "maf-sandbox-hyperlight": "maf_sandbox_hyperlight",
    "maf-sandbox-drawio": "maf_sandbox_drawio",
    "maf-sandbox-otel": "maf_sandbox_otel",
    "maf-sandbox-terraform": "maf_sandbox_terraform",
    "maf-sandbox-tui": "maf_sandbox_tui",
    "maf-sandbox-wslc": "maf_sandbox_wslc",
}

_SARIF = json.dumps(
    {
        "version": "2.1.0",
        "runs": [
            {
                "tool": {"driver": {"name": "bicep"}},
                "results": [
                    {
                        "ruleId": "BCP035",
                        "level": "error",
                        "message": {"text": "Missing required property 'properties'."},
                        "locations": [
                            {
                                "physicalLocation": {
                                    "artifactLocation": {"uri": "main.bicep"},
                                    "region": {"startLine": 7, "startColumn": 3},
                                }
                            }
                        ],
                    }
                ],
            }
        ],
    }
)
_EMPTY_SARIF = json.dumps(
    {"version": "2.1.0", "runs": [{"tool": {"driver": {"name": "bicep"}}, "results": []}]}
)


class _RecordingContents(dict[str, bytes]):
    """A sandbox's ``contents``, copying every write out where a removal cannot reach it."""

    def __init__(self, written: dict[str, str], seeded: dict[str, bytes]) -> None:
        super().__init__()
        self._written = written
        for path, content in seeded.items():
            self[path] = content

    def __setitem__(self, path: str, content: bytes) -> None:
        super().__setitem__(path, content)
        self._written[path] = content.decode("utf-8")


def _recording(sandbox: InProcessSandbox) -> dict[str, str]:
    """A decoded ledger of every write into ``sandbox``, kept where the reclaim cannot reach it."""
    written: dict[str, str] = {}
    sandbox.contents = _RecordingContents(written, sandbox.contents)
    return written


def _check_typing_marker(module) -> None:
    """`py.typed` is invisible to every test in this repository and breaks consumers."""
    marker = pathlib.Path(module.__file__).parent / "py.typed"
    if not marker.is_file():
        raise SystemExit(f"FAIL: {module.__name__} installed without py.typed at {marker}")


def _smoke_maf_sandbox() -> str:
    from maf_sandbox import Isolation, SandboxKey, SandboxRouter, SandboxSpec
    from maf_sandbox.testing import (
        InMemoryStore,
        InProcessSandbox,
        InProcessSandboxBackend,
    )

    backend = InProcessSandboxBackend(InProcessSandbox(default_stdout="ok"))
    # Below the default microvm floor: this part proves acquire/exec, not the floor.
    router = SandboxRouter([backend], min_isolation=Isolation.NONE)
    key = SandboxKey(scope="s", thread_id="t", agent_id="a")
    sandbox = asyncio.run(router.acquire(key, SandboxSpec(kind="smoke")))
    result = asyncio.run(sandbox.exec("true", working_directory="/w", timeout=5))
    if result.stdout != "ok":
        raise SystemExit(f"FAIL: in-process sandbox returned {result.stdout!r}")

    from maf_sandbox import SandboxBackendNotPermitted

    try:
        SandboxRouter([backend])
    except SandboxBackendNotPermitted:
        pass
    else:
        raise SystemExit(
            "FAIL: the default minimum-isolation floor accepted a process-isolated backend"
        )

    _ = InMemoryStore({"a": "b"})
    return "router + in-process backend + the default minimum-isolation floor"


def _smoke_maf_sandbox_acas() -> str:
    from maf_sandbox import Capability, Isolation, meets_floor
    from maf_sandbox_acas import AcasSandboxBackend, AcasSandboxConfig

    # Constructed, not called: this asserts the package imports with its real preview SDK
    # resolved and still declares the boundary the router gates on. Reaching the service
    # would need credentials and would not test packaging.
    backend = AcasSandboxBackend(AcasSandboxConfig(endpoint="https://example.invalid"))
    if not meets_floor(backend.isolation, Isolation.MICROVM):
        raise SystemExit(
            f"FAIL: acas backend declares {backend.isolation!r}, "
            "which does not meet the default microvm floor"
        )
    # HOST_TOOLS specifically, because a sample resolves this package from PyPI rather than from
    # the workspace: a codeact sample wiring a host-tool registry against this backend is refused
    # at attach unless the *published* wheel carries the declaration.
    if backend.declarations.capabilities != frozenset(
        {
            Capability.EXEC,
            Capability.FILES_IN,
            Capability.FILES_OUT,
            Capability.FILES_LIST,
            Capability.FILES_DELETE,
            Capability.HOST_TOOLS,
        }
    ):
        raise SystemExit(
            f"FAIL: acas backend declares {sorted(backend.declarations.capabilities)!r}"
        )
    return (
        "backend constructs, meets the default minimum-isolation floor, and declares the pull "
        "surface and HOST_TOOLS"
    )


def _rendered(result: object) -> str:
    """A tool result as text, whether the tool answered with one string or with items.

    A kind that labels part of what it returns answers with a list of items, and `str` of a
    list renders their reprs rather than their text.
    """
    if isinstance(result, list):
        items: list[object] = result
        return "\n".join(str(getattr(item, "text", None) or "") for item in items)
    return str(result)


def _integrities(result: object) -> list[object]:
    """Each item's own declared integrity, or ``None`` where it carries no label."""
    if not isinstance(result, list):
        return []
    items: list[object] = result
    return [
        (getattr(item, "additional_properties", None) or {})
        .get("security_label", {})
        .get("integrity")
        for item in items
    ]


def _smoke_maf_sandbox_bicep() -> str:
    from maf_sandbox import CallerContext, Isolation, SandboxRouter
    from maf_sandbox.testing import (
        InMemoryStore,
        InProcessSandbox,
        InProcessSandboxBackend,
    )
    from maf_sandbox_bicep import BICEP_VALIDATE_TOOL_NAME, make_bicep_tools

    def _bicep_tool(store: InMemoryStore, backend: InProcessSandboxBackend):
        context = CallerContext(
            current_scope=lambda: "smoke",
            current_thread_id=lambda: "thread",
            list_files=InMemoryStore.list,
        )
        tools = make_bicep_tools(
            # Below the default floor, as in _smoke_maf_sandbox: exercises the workload.
            SandboxRouter([backend], min_isolation=Isolation.NONE),
            # InMemoryStore provides the AgentFileStore subset this smoke test exercises.
            store,  # type: ignore[arg-type]
            "devops-engineer",
            context,
            image="registry.invalid/bicep:1",
        )
        if len(tools) != 1 or getattr(tools[0], "name", None) != BICEP_VALIDATE_TOOL_NAME:
            raise SystemExit(f"FAIL: expected one {BICEP_VALIDATE_TOOL_NAME} tool, got {tools}")
        tool = tools[0]
        return getattr(tool, "func", None) or getattr(tool, "__wrapped__", None) or tool

    # The happy path: the file reaches the sandbox, both phases run, diagnostics render.
    store = InMemoryStore({"main.bicep": "param location string = resourceGroup().location"})
    sandbox = InProcessSandbox(outputs={"bicep build": _SARIF}, default_stdout=_EMPTY_SARIF)
    written = _recording(sandbox)
    backend = InProcessSandboxBackend(sandbox)
    answer = asyncio.run(_bicep_tool(store, backend)(files=["main.bicep"]))
    out = _rendered(answer)
    if "BCP035" not in out:
        raise SystemExit(f"FAIL: diagnostics missing from tool output: {out!r}")
    # The split a FIDES host reads, under the result contract: the parts the model may act
    # on carry no label of their own and inherit the tool's raised declaration, the compiler's
    # own text says untrusted for itself, and the standing sentence closes it as trusted. So
    # hiding leaves the completion line, the verdict and the sentence readable.
    integrities = _integrities(answer)
    if (
        len(integrities) < 3
        or integrities[0] is not None
        or integrities[-1] != "trusted"
        or "untrusted" not in integrities
        or set(integrities[1:-1]) - {None, "untrusted"}
    ):
        raise SystemExit(
            f"FAIL: the result is not unlabelled parts, untrusted output, one trusted sentence: "
            f"{integrities!r}"
        )
    if not any(path.endswith("/main.bicep") for path in written):
        raise SystemExit(f"FAIL: the workload never wrote the file into the sandbox: {written}")
    # Adoption can acquire the same key again before serving the call.
    if len(set(backend.keys)) != 1:
        raise SystemExit(
            f"FAIL: the happy path acquired {len(set(backend.keys))} sandbox keys, not 1"
        )

    # The failure paths (#22, #33): the message an agent receives is the whole product here,
    # and both return before a sandbox is acquired — the free half of "verify the published
    # package" that a workspace test cannot make, because in the workspace the wheel resolves
    # whether or not its build included these modules.
    miss_store = InMemoryStore({"main.bicep": "x"})
    miss_backend = InProcessSandboxBackend(InProcessSandbox(default_stdout=_EMPTY_SARIF))
    miss = _rendered(asyncio.run(_bicep_tool(miss_store, miss_backend)(files=["absent.bicep"])))
    if "not in this tool's file listing" not in miss:
        raise SystemExit(f"FAIL: a listing miss did not say so: {miss!r}")
    if miss_backend.keys:
        raise SystemExit("FAIL: a listing miss acquired a sandbox before refusing")

    # A hostile name that is genuinely in the listing — being present is not evidence it is
    # safe to interpolate into a shell command.
    hostile = "a;$(id).bicep"
    unsafe_store = InMemoryStore({hostile: "x"})
    unsafe_backend = InProcessSandboxBackend(InProcessSandbox(default_stdout=_EMPTY_SARIF))
    unsafe = _rendered(asyncio.run(_bicep_tool(unsafe_store, unsafe_backend)(files=[hostile])))
    if "[A-Za-z0-9._/-]" not in unsafe:
        raise SystemExit(f"FAIL: an unsafe name was not named as such: {unsafe!r}")
    if unsafe_backend.keys or unsafe_backend.sandbox.commands:
        raise SystemExit("FAIL: an unsafe name reached the sandbox")
    if miss == unsafe:
        raise SystemExit("FAIL: a listing miss and an unsafe name share one message")

    return (
        "bicep_validate rendered diagnostics on the happy path, and refused a listing miss "
        "and an unsafe name — with distinct messages, before acquiring a sandbox"
    )


def _smoke_maf_sandbox_codeact() -> str:
    from maf_sandbox import (
        DEFAULT_CAPABILITIES,
        CallerContext,
        Capability,
        Isolation,
        LandedArtifact,
        OutputSink,
        SandboxCapabilityNotSupported,
        SandboxRouter,
    )
    from maf_sandbox.testing import (
        FAKE_BACKEND_DECLARATIONS,
        InMemoryStore,
        InProcessSandbox,
        InProcessSandboxBackend,
    )
    from maf_sandbox_codeact import (
        EXECUTE_CODE_TOOL_NAME,
        CodeactOutputs,
        make_codeact_tools,
    )

    async def _listing(store):
        return [] if store is None else await store.list()

    context = CallerContext(
        current_scope=lambda: "smoke",
        current_thread_id=lambda: "thread",
        list_files=_listing,
    )

    def _router(backend):
        # The bottom rung, opted below the default microvm floor: the floor itself is
        # _smoke_maf_sandbox's subject, and a bare SandboxRouter([backend]) is refused there.
        return SandboxRouter([backend], min_isolation=Isolation.NONE)

    def _body(tools):
        if len(tools) != 1 or getattr(tools[0], "name", None) != EXECUTE_CODE_TOOL_NAME:
            raise SystemExit(f"FAIL: expected one {EXECUTE_CODE_TOOL_NAME} tool, got {tools}")
        tool = tools[0]
        return getattr(tool, "func", None) or getattr(tool, "__wrapped__", None) or tool

    sandbox = InProcessSandbox(default_stdout="7\n")
    written = _recording(sandbox)
    backend = InProcessSandboxBackend(sandbox)
    body = _body(
        make_codeact_tools(
            _router(backend), "data-analyst", context, image="registry.invalid/python:3"
        )
    )

    answer = asyncio.run(body(code="print(3 + 4)"))
    out = _rendered(answer)
    # Under the result contract the parts are separate items: a completion line the model
    # can read, a verdict from the tool's declared set, then the program's own text.
    integrities = _integrities(answer)
    if integrities[:2] != [None, None] or integrities[-1] != "untrusted":
        raise SystemExit(f"FAIL: the parts are not unlabelled then untrusted: {integrities}")
    if "Result: ok" not in out or not out.endswith("stdout:\n7"):
        raise SystemExit(f"FAIL: the tool rendered {out!r}")
    # Each call gets a directory of its own under the work dir, so the path is not fixed.
    landed_program = list(written.items())
    if len(landed_program) != 1 or not landed_program[0][0].startswith("/maf-sandbox/work/"):
        raise SystemExit(f"FAIL: the program never reached the sandbox: {written}")
    program_path, source = landed_program[0]
    if not program_path.endswith("/program.py") or source != "print(3 + 4)":
        raise SystemExit(f"FAIL: the program landed at {program_path!r} as {source!r}")
    command, working_directory, _ = backend.sandbox.commands[0]
    if command != "python3 program.py" or working_directory != program_path.rsplit("/", 1)[0]:
        raise SystemExit(f"FAIL: unexpected command {backend.sandbox.commands[0]!r}")

    # Files in: the caller's listing is the authority, and it has to travel in the wheel.
    store = InMemoryStore({"data.csv": "a,b\n"})
    shared_sandbox = InProcessSandbox(default_stdout="ok\n")
    shared_written = _recording(shared_sandbox)
    shared = InProcessSandboxBackend(shared_sandbox)
    with_files = _body(
        make_codeact_tools(
            _router(shared),
            "data-analyst",
            context,
            # Same InMemoryStore→AgentFileStore duck-type as _bicep_tool.
            file_store=store,  # type: ignore[arg-type]
            image="registry.invalid/python:3",
        )
    )
    asyncio.run(with_files(code="print(1)", files=["data.csv"]))
    if not any(path.endswith("/data.csv") for path in shared_written):
        raise SystemExit(f"FAIL: the listed file was not shared: {shared_written}")
    refused = asyncio.run(with_files(code="print(1)", files=["absent.csv"]))
    if "not in this tool's file listing" not in refused:
        raise SystemExit(f"FAIL: an unlisted file was not refused: {refused!r}")

    # Files out: a declared name lands through the host's sink, under its own name rather
    # than the run directory's.
    landed: list[str] = []

    async def _deliver(artifact):
        landed.append(artifact.name)
        return LandedArtifact(name=artifact.name, display=f"saved {artifact.name}")

    class _Producing(InProcessSandbox):
        async def exec(self, command, *, working_directory, timeout):
            result = await super().exec(
                command, working_directory=working_directory, timeout=timeout
            )
            await self.write_file("report.csv", b"1,2\n", working_directory=working_directory)
            return result

    producing = InProcessSandboxBackend(
        _Producing(default_stdout="done\n"),
        declarations=dataclasses.replace(
            FAKE_BACKEND_DECLARATIONS, capabilities=DEFAULT_CAPABILITIES | {Capability.FILES_OUT}
        ),
    )
    with_outputs = _body(
        make_codeact_tools(
            _router(producing),
            "data-analyst",
            context,
            output_sink=OutputSink(deliver=_deliver),
            outputs=CodeactOutputs.DECLARED,
            image="registry.invalid/python:3",
        )
    )
    saved = asyncio.run(with_outputs(code="print(1)", outputs=["report.csv"]))
    if landed != ["report.csv"] or "saved report.csv" not in saved:
        raise SystemExit(f"FAIL: the declared output did not land: {landed} / {saved!r}")

    # The spec's `requires` has to travel in the wheel: a backend that cannot run a command
    # is refused as the tool attaches, not when the model first calls it.
    weak = InProcessSandboxBackend(
        declarations=dataclasses.replace(
            FAKE_BACKEND_DECLARATIONS, capabilities=frozenset({Capability.FILES_IN})
        )
    )
    try:
        make_codeact_tools(_router(weak), "data-analyst", context)
    except SandboxCapabilityNotSupported:
        pass
    else:
        raise SystemExit("FAIL: a backend that cannot exec was allowed to serve execute_code")

    return (
        "execute_code wrote the program into a directory of its own and ran the interpreter "
        "as argv; it shared a listed file and refused an unlisted one; it landed a declared "
        "output through the host's sink; and it refused a backend that cannot exec"
    )


def _smoke_maf_sandbox_otel() -> str:
    from maf_sandbox import (
        Egress,
        IsolationScope,
        SandboxAcquired,
        SandboxKey,
        SandboxObserver,
        SandboxSpec,
    )
    from maf_sandbox_otel import NAMESPACE, OpenTelemetrySandboxObserver, hashed_key

    if not issubclass(OpenTelemetrySandboxObserver, SandboxObserver):
        raise SystemExit(
            "FAIL: the recorder is not a SandboxObserver, so nothing would register it"
        )

    # No SDK is installed here, which is the case a host without telemetry configured is in: the
    # API's no-op providers answer, and recording must still cost nothing and raise nothing.
    observer = OpenTelemetrySandboxObserver()
    key = SandboxKey(scope="s", thread_id="t", agent_id="a")
    observer.sandbox_acquired(
        SandboxAcquired(
            key=key,
            spec=SandboxSpec(kind="smoke", egress=Egress.CLOSED),
            isolation_scope=IsolationScope.CONVERSATION,
            backend="none",
            isolation=None,
            declarations=None,
            seconds=0.0,
        )
    )
    if hashed_key(key) != hashed_key(key) or NAMESPACE != "maf_sandbox":
        raise SystemExit(f"FAIL: the join column is unstable or renamed ({NAMESPACE})")
    return "the recorder registers as an observer and records against no-op providers"


def _smoke_maf_sandbox_wslc() -> str:
    from maf_sandbox import Egress, Isolation
    from maf_sandbox_wslc import (
        WslcSandboxBackend,
        WslcSandboxConfig,
        proxy_build_context,
    )

    # Constructed, not called: CI runners have no `wslc`, and reaching it would not test packaging.
    backend = WslcSandboxBackend(WslcSandboxConfig())
    if backend.isolation != Isolation.CONTAINER:
        raise SystemExit(f"FAIL: wslc backend declares {backend.isolation!r}, expected container")
    allowlisting = WslcSandboxBackend(WslcSandboxConfig(egress_proxy_image="x:1"))
    if backend.declarations.egress_modes != frozenset(
        {Egress.CLOSED}
    ) or allowlisting.declarations.egress_modes != frozenset({Egress.ALLOWLIST, Egress.CLOSED}):
        raise SystemExit(
            f"FAIL: egress {backend.declarations.egress_modes!r}/{allowlisting.declarations.egress_modes!r}"
        )
    # The proxy recipe is data, not code: a wheel that drops it breaks allowlist mode only here.
    dockerfile = proxy_build_context() / "Dockerfile"
    if not dockerfile.is_file():
        raise SystemExit(f"FAIL: the proxy build context is missing its Dockerfile ({dockerfile})")
    return "backend constructs, declares its egress, and ships the proxy recipe"


def _smoke_maf_sandbox_deepagents() -> str:
    from deepagents.backends.protocol import SandboxBackendProtocol, execute_accepts_timeout
    from maf_sandbox import (
        Capability,
        Isolation,
        SandboxCapabilityNotSupported,
        SandboxKey,
        SandboxRouter,
    )
    from maf_sandbox.testing import InProcessSandboxBackend
    from maf_sandbox_deepagents import REQUIRED_CAPABILITIES, MafSandbox, deepagents_spec

    if not issubclass(MafSandbox, SandboxBackendProtocol):
        raise SystemExit("FAIL: MafSandbox is not a SandboxBackendProtocol, so no execute tool")
    if not execute_accepts_timeout(MafSandbox):
        raise SystemExit("FAIL: execute() does not take the per-command timeout Deep Agents passes")
    spec = deepagents_spec("smoke:image")
    if not REQUIRED_CAPABILITIES <= spec.requires or Capability.FILES_OUT not in spec.requires:
        raise SystemExit(f"FAIL: the spec requires {sorted(spec.requires)}")
    router = SandboxRouter([InProcessSandboxBackend()], min_isolation=Isolation.NONE)
    try:
        MafSandbox(router, SandboxKey(scope="s", thread_id="t", agent_id="a"), spec)
    except SandboxCapabilityNotSupported:  # the fake declares no FILES_OUT
        pass
    else:
        raise SystemExit("FAIL: a backend without FILES_OUT was admitted")
    return "adapter constructs, carries a timeout, and a backend lacking FILES_OUT is refused"


def _smoke_maf_sandbox_docker() -> str:
    from maf_sandbox import Capability, Egress, Isolation
    from maf_sandbox_docker import (
        DockerSandboxBackend,
        DockerSandboxConfig,
        proxy_build_context,
    )

    # Constructed, not called: CI runners running this smoke have no engine reachable in a clean
    # venv, and reaching one would not test packaging.
    backend = DockerSandboxBackend(DockerSandboxConfig())
    if backend.isolation != Isolation.CONTAINER:
        raise SystemExit(f"FAIL: docker backend declares {backend.isolation!r}, expected container")
    if backend.declarations.capabilities != frozenset(
        {
            Capability.EXEC,
            Capability.FILES_IN,
            Capability.FILES_OUT,
            Capability.FILES_DELETE,
            Capability.HOST_TOOLS,
            Capability.RECLAIM,
        }
    ):
        raise SystemExit(
            f"FAIL: docker backend declares {sorted(backend.declarations.capabilities)!r}"
        )
    allowlisting = DockerSandboxBackend(DockerSandboxConfig(egress_proxy_image="x:1"))
    if backend.declarations.egress_modes != frozenset(
        {Egress.CLOSED}
    ) or allowlisting.declarations.egress_modes != frozenset({Egress.ALLOWLIST, Egress.CLOSED}):
        raise SystemExit(
            f"FAIL: egress {backend.declarations.egress_modes!r}/{allowlisting.declarations.egress_modes!r}"
        )
    # The proxy recipe is data, not code: a wheel that drops it breaks allowlist mode only here.
    dockerfile = proxy_build_context() / "Dockerfile"
    if not dockerfile.is_file():
        raise SystemExit(f"FAIL: the proxy build context is missing its Dockerfile ({dockerfile})")
    return (
        "backend constructs, declares FILES_OUT, HOST_TOOLS, RECLAIM and its egress, and ships the "
        "proxy recipe"
    )


def _smoke_maf_sandbox_hyperlight() -> str:
    from maf_sandbox import (
        Capability,
        Egress,
        Isolation,
        SandboxBackend,
        SandboxCapabilityNotSupported,
        SandboxKey,
        SandboxSpec,
    )
    from maf_sandbox_hyperlight import RUNTIME_INSTRUCTIONS, HyperlightSandboxBackend

    backend = HyperlightSandboxBackend()
    if not isinstance(backend, SandboxBackend) or backend.isolation is not Isolation.MICROVM:
        raise SystemExit("FAIL: Hyperlight does not implement the microVM backend protocol")
    if backend.declarations.capabilities != {Capability.RUN_CODE, Capability.SNAPSHOT}:
        raise SystemExit("FAIL: Hyperlight advertises an unvalidated channel")
    if backend.declarations.egress_modes != {Egress.CLOSED, Egress.ALLOWLIST}:
        raise SystemExit("FAIL: Hyperlight does not declare its enforced network policies")
    if not RUNTIME_INSTRUCTIONS:
        raise SystemExit("FAIL: Hyperlight supplies no runtime instructions")
    try:
        asyncio.run(
            backend.acquire(SandboxKey("smoke", "thread", "agent"), SandboxSpec(kind="shell"))
        )
    except SandboxCapabilityNotSupported:
        pass
    else:
        raise SystemExit("FAIL: Hyperlight admitted EXEC/FILES_IN")
    return "constructs without WHP, declares runtime/reset and refuses shell/file workloads before starting a worker"


def _smoke_maf_sandbox_terraform() -> str:
    """Exercise both public engine options without requiring guest binaries on the host."""
    from maf_sandbox import CallerContext, Cleanup, Egress, IsolationScope
    from maf_sandbox.testing import InMemoryStore
    from maf_sandbox_terraform import make_terraform_tools, terraform_sandbox_spec

    for engine in ("terraform", "opentofu"):
        spec = terraform_sandbox_spec(engine=engine)
        assert spec.kind == engine
        assert spec.isolation_scope is IsolationScope.CALL
        assert spec.min_cleanup is Cleanup.DISPOSE and spec.egress is Egress.CLOSED
    context = CallerContext(
        current_scope=lambda: "smoke",
        current_thread_id=lambda: "test",
        list_files=InMemoryStore.list,
    )
    # This deliberately loose store exercises the same protocol as the other wheel smokes.
    assert make_terraform_tools(None, InMemoryStore({}), "smoke", context) == []  # pyright: ignore[reportArgumentType]
    return "both engines require closed call isolation and disposal; no guest binaries imported"


def _smoke_maf_sandbox_drawio() -> str:
    import tempfile
    from importlib.resources import files

    from maf_sandbox import (
        DEFAULT_CAPABILITIES,
        Artifact,
        Capability,
        Isolation,
        LandedArtifact,
        OsFamily,
        OutputSink,
        SandboxRouter,
        make_file_system_sink,
    )
    from maf_sandbox.maf import list_no_files, make_caller_context
    from maf_sandbox.testing import (
        FAKE_BACKEND_DECLARATIONS,
        InProcessSandbox,
        InProcessSandboxBackend,
    )
    from maf_sandbox_drawio import make_drawio_tools

    program = files("maf_sandbox_drawio").joinpath("_renderer.py").read_text(encoding="utf-8")
    if "def convert(" not in program:
        raise SystemExit("FAIL: draw.io guest converter is missing")
    source = (
        '<mxGraphModel><root><mxCell id="0"/><mxCell id="1" parent="0"/>'
        '<mxCell id="a" parent="1" vertex="1" value="Smoke &amp; check">'
        '<mxGeometry as="geometry" x="10" y="20" width="160" height="80"/>'
        "</mxCell></root></mxGraphModel>"
    )
    output = f'<mxfile><diagram name="Page-1">{source}</diagram></mxfile>'

    class _Producing(InProcessSandbox):
        async def exec(self, command, *, working_directory, timeout):
            result = await super().exec(
                command, working_directory=working_directory, timeout=timeout
            )
            await self.write_file("diagram.drawio", output, working_directory=working_directory)
            return result

    sandbox = _Producing()
    written = _recording(sandbox)
    backend = InProcessSandboxBackend(
        sandbox,
        declarations=dataclasses.replace(
            FAKE_BACKEND_DECLARATIONS,
            capabilities=DEFAULT_CAPABILITIES | {Capability.FILES_OUT},
            os_families=frozenset({OsFamily.POSIX}),
        ),
    )
    router = SandboxRouter([backend], min_isolation=Isolation.NONE)
    with tempfile.TemporaryDirectory(prefix="drawio-smoke-") as directory:
        output_directory = pathlib.Path(directory)

        async def deliver(artifact: Artifact) -> LandedArtifact:
            if artifact.call_id is None:
                raise SystemExit("FAIL: draw.io delivered an artifact without a call ID")
            sink = make_file_system_sink(output_directory / artifact.call_id)
            return await sink.deliver(artifact)

        [tool] = make_drawio_tools(
            router,
            "smoke",
            make_caller_context(list_no_files, lambda: "s", lambda: "t"),
            OutputSink(deliver, per_call=True),
        )
        if tool.name != "create_drawio":
            raise SystemExit(f"FAIL: expected create_drawio, got {tool.name!r}")
        refused = asyncio.run(tool.func(xml="x" * (1024 * 1024 + 1)))
        if "1 MiB" not in _rendered(refused) or backend.keys or any(output_directory.iterdir()):
            raise SystemExit("FAIL: draw.io did not reject oversized input before acquiring")
        result = asyncio.run(tool.func(xml=source))
        destinations = list(output_directory.glob("*/diagram.drawio"))
        rendered = _rendered(result)
        # Separate items now: a completion line, the verdict, then the sink reference.
        if "Result: created" not in rendered or len(destinations) != 1:
            raise SystemExit(f"FAIL: draw.io did not deliver an artifact: {result!r}")
        [destination] = destinations
        landed_call_id = destination.parent.name
        if destination.read_text(encoding="utf-8") != output:
            raise SystemExit("FAIL: draw.io did not preserve the converter's output")
    if len(sandbox.commands) != 1:
        raise SystemExit(f"FAIL: expected one draw.io converter execution: {sandbox.commands!r}")
    command, guest_directory, _ = sandbox.commands[0]
    if landed_call_id != guest_directory.rsplit("/", 1)[-1]:
        raise SystemExit("FAIL: draw.io landed the output under a different call ID")
    expected = "python3 -I renderer.py --preserve-layout true --direction TB --timeout 54.0"
    if command != expected:
        raise SystemExit(f"FAIL: unexpected draw.io converter command: {command!r}")
    if written.get(f"{guest_directory}/input.xml") != source:
        raise SystemExit("FAIL: draw.io did not upload the model's XML")
    if written.get(f"{guest_directory}/renderer.py") != program:
        raise SystemExit("FAIL: draw.io did not upload its packaged converter")
    if len(set(backend.keys)) != 1 or not backend.disposed:
        raise SystemExit("FAIL: draw.io did not acquire and dispose one sandbox key")
    return (
        "create_drawio uploads XML and its packaged converter, executes fixed argv, "
        "delivers diagram.drawio under its call ID and disposes; oversized input is refused "
        "before acquiring"
    )


def _smoke_maf_sandbox_tui() -> str:
    from maf_sandbox_tui import MemoryControl, SandboxConsole

    records = asyncio.run(MemoryControl.demo().list_sandboxes())
    if len(records) != 3 or not all(record.instance_id for record in records):
        raise SystemExit("FAIL: MST demo does not expose physical sandbox identities")
    if not SandboxConsole.TITLE:
        raise SystemExit("FAIL: MST console has no title")
    executable = pathlib.Path(sys.executable).parent / (
        "mst.exe" if sys.platform == "win32" else "mst"
    )
    if not executable.is_file():
        raise SystemExit(f"FAIL: the wheel installed no MST entry point at {executable}")
    version_result = subprocess.run(
        [str(executable), "version", "--json"],
        capture_output=True,
        check=False,
        text=True,
        timeout=10,
    )
    if version_result.returncode != 0:
        raise SystemExit(f"FAIL: installed 'mst version' failed: {version_result.stderr}")
    reported = json.loads(version_result.stdout)
    if reported.get("name") != "maf-sandbox-tui" or not reported.get("version"):
        raise SystemExit(f"FAIL: installed 'mst version' returned {reported!r}")
    demo_result = subprocess.run(
        [str(executable), "--demo", "--json"],
        capture_output=True,
        check=False,
        text=True,
        timeout=10,
    )
    if demo_result.returncode != 0 or len(json.loads(demo_result.stdout)) != 3:
        raise SystemExit(
            f"FAIL: installed 'mst --demo --json' failed: "
            f"{demo_result.stdout!r} / {demo_result.stderr!r}"
        )
    return "runs its installed entry point, reports its version and exposes a three-instance demo"


_SMOKES = {
    "maf-sandbox": _smoke_maf_sandbox,
    "maf-sandbox-acas": _smoke_maf_sandbox_acas,
    "maf-sandbox-bicep": _smoke_maf_sandbox_bicep,
    "maf-sandbox-codeact": _smoke_maf_sandbox_codeact,
    "maf-sandbox-deepagents": _smoke_maf_sandbox_deepagents,
    "maf-sandbox-docker": _smoke_maf_sandbox_docker,
    "maf-sandbox-drawio": _smoke_maf_sandbox_drawio,
    "maf-sandbox-hyperlight": _smoke_maf_sandbox_hyperlight,
    "maf-sandbox-otel": _smoke_maf_sandbox_otel,
    "maf-sandbox-terraform": _smoke_maf_sandbox_terraform,
    "maf-sandbox-tui": _smoke_maf_sandbox_tui,
    "maf-sandbox-wslc": _smoke_maf_sandbox_wslc,
}


def main(argv: list[str]) -> int:
    """CLI entry: import the named distribution, assert it resolved under ``site-packages``, check ``py.typed``, run the per-package smoke, and print SMOKE OK."""
    if len(argv) != 2 or argv[1] not in _PACKAGES:
        print(f"usage: {argv[0]} <{'|'.join(_PACKAGES)}>", file=sys.stderr)
        return 2

    name = argv[1]
    module_name = _PACKAGES[name]
    module = __import__(module_name)

    # An installed package must not resolve to a checkout: that would mean this proved
    # nothing about the artifact.
    location = pathlib.Path(module.__file__).resolve()
    if "site-packages" not in location.parts:
        raise SystemExit(f"FAIL: {module_name} imported from {location}, not an installation")

    _check_typing_marker(module)
    detail = _SMOKES[name]()
    print(f"SMOKE OK  {name}  ({location.parent})")
    print(f"          {detail}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
