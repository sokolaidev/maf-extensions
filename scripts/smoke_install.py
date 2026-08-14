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
import json
import pathlib
import sys

_PACKAGES = {
    "maf-sandbox": "maf_sandbox",
    "maf-sandbox-acas": "maf_sandbox_acas",
    "maf-sandbox-bicep": "maf_sandbox_bicep",
    "maf-sandbox-codeact": "maf_sandbox_codeact",
    "maf-sandbox-docker": "maf_sandbox_docker",
    "maf-sandbox-wslc": "maf_sandbox_wslc",
}

_SARIF = json.dumps(
    {
        "version": "2.1.0",
        "runs": [
            {
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
                ]
            }
        ],
    }
)
_EMPTY_SARIF = json.dumps({"version": "2.1.0", "runs": []})


def _check_typing_marker(module) -> None:
    """`py.typed` is invisible to every test in this repository and breaks consumers."""
    marker = pathlib.Path(module.__file__).parent / "py.typed"
    if not marker.is_file():
        raise SystemExit(
            f"FAIL: {module.__name__} installed without py.typed at {marker}"
        )


def _smoke_maf_sandbox() -> str:
    from maf_sandbox import Isolation, SandboxKey, SandboxRouter, SandboxSpec
    from maf_sandbox.testing import (
        InMemoryStore,
        InProcessSandbox,
        InProcessSandboxBackend,
    )

    backend = InProcessSandboxBackend(InProcessSandbox(default_stdout="ok"))
    # Below the default microvm floor: this part proves acquire/exec, not the floor.
    router = SandboxRouter([backend], min_isolation=Isolation.PROCESS)
    key = SandboxKey(scope="s", thread_id="t", agent_dir="a")
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
    from maf_sandbox import Isolation, meets_floor
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
    return "backend constructs and meets the default minimum-isolation floor"


def _smoke_maf_sandbox_bicep() -> str:
    from maf_sandbox import Isolation, SandboxRouter, CallerContext
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
            SandboxRouter([backend], min_isolation=Isolation.PROCESS),
            # InMemoryStore provides the AgentFileStore subset this smoke test exercises.
            store,  # type: ignore[arg-type]
            "devops-engineer",
            context,
            image="registry.invalid/bicep:1",
        )
        if (
            len(tools) != 1
            or getattr(tools[0], "name", None) != BICEP_VALIDATE_TOOL_NAME
        ):
            raise SystemExit(
                f"FAIL: expected one {BICEP_VALIDATE_TOOL_NAME} tool, got {tools}"
            )
        tool = tools[0]
        return getattr(tool, "func", None) or getattr(tool, "__wrapped__", None) or tool

    # The happy path: the file reaches the sandbox, both phases run, diagnostics render.
    store = InMemoryStore(
        {"main.bicep": "param location string = resourceGroup().location"}
    )
    backend = InProcessSandboxBackend(
        InProcessSandbox(outputs={"bicep build": _SARIF}, default_stdout=_EMPTY_SARIF)
    )
    out = asyncio.run(_bicep_tool(store, backend)(files=["main.bicep"]))
    if "BCP035" not in out:
        raise SystemExit(f"FAIL: diagnostics missing from tool output: {out!r}")
    if not backend.sandbox.files:
        raise SystemExit("FAIL: the workload never wrote the file into the sandbox")
    if len(backend.keys) != 1:
        raise SystemExit(
            f"FAIL: the happy path acquired {len(backend.keys)} sandbox(es), not 1"
        )

    # The failure paths (#22, #33): the message an agent receives is the whole product here,
    # and both return before a sandbox is acquired — the free half of "verify the published
    # package" that a workspace test cannot make, because in the workspace the wheel resolves
    # whether or not its build included these modules.
    miss_store = InMemoryStore({"main.bicep": "x"})
    miss_backend = InProcessSandboxBackend(
        InProcessSandbox(default_stdout=_EMPTY_SARIF)
    )
    miss = asyncio.run(_bicep_tool(miss_store, miss_backend)(files=["absent.bicep"]))
    if "not in this tool's file listing" not in miss:
        raise SystemExit(f"FAIL: a listing miss did not say so: {miss!r}")
    if miss_backend.keys:
        raise SystemExit("FAIL: a listing miss acquired a sandbox before refusing")

    # A hostile name that is genuinely in the listing — being present is not evidence it is
    # safe to interpolate into a shell command.
    hostile = "a;$(id).bicep"
    unsafe_store = InMemoryStore({hostile: "x"})
    unsafe_backend = InProcessSandboxBackend(
        InProcessSandbox(default_stdout=_EMPTY_SARIF)
    )
    unsafe = asyncio.run(_bicep_tool(unsafe_store, unsafe_backend)(files=[hostile]))
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
        Capability,
        Isolation,
        LandedArtifact,
        OutputSink,
        SandboxCapabilityNotSupported,
        SandboxRouter,
        CallerContext,
    )
    from maf_sandbox.testing import (
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
        # Process isolation, opted below the default microvm floor: the floor itself is
        # _smoke_maf_sandbox's subject, and a bare SandboxRouter([backend]) is refused there.
        return SandboxRouter([backend], min_isolation=Isolation.PROCESS)

    def _body(tools):
        if len(tools) != 1 or getattr(tools[0], "name", None) != EXECUTE_CODE_TOOL_NAME:
            raise SystemExit(
                f"FAIL: expected one {EXECUTE_CODE_TOOL_NAME} tool, got {tools}"
            )
        tool = tools[0]
        return getattr(tool, "func", None) or getattr(tool, "__wrapped__", None) or tool

    backend = InProcessSandboxBackend(InProcessSandbox(default_stdout="7\n"))
    body = _body(
        make_codeact_tools(
            _router(backend), "data-analyst", context, image="registry.invalid/python:3"
        )
    )

    out = asyncio.run(body(code="print(3 + 4)"))
    if out != "stdout:\n7":
        raise SystemExit(f"FAIL: the tool rendered {out!r}")
    # Each call gets a directory of its own under the work dir, so the path is not fixed.
    written = list(backend.sandbox.files.items())
    if len(written) != 1 or not written[0][0].startswith("/maf-sandbox/work/"):
        raise SystemExit(
            f"FAIL: the program never reached the sandbox: {backend.sandbox.files}"
        )
    program_path, source = written[0]
    if not program_path.endswith("/program.py") or source != "print(3 + 4)":
        raise SystemExit(f"FAIL: the program landed at {program_path!r} as {source!r}")
    if backend.sandbox.commands[0][0] != f"python3 {program_path}":
        raise SystemExit(f"FAIL: unexpected command {backend.sandbox.commands[0]!r}")

    # Files in: the caller's listing is the authority, and it has to travel in the wheel.
    store = InMemoryStore({"data.csv": "a,b\n"})
    shared = InProcessSandboxBackend(InProcessSandbox(default_stdout="ok\n"))
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
    if not any(path.endswith("/data.csv") for path in shared.sandbox.files):
        raise SystemExit(
            f"FAIL: the listed file was not shared: {shared.sandbox.files}"
        )
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
            self.contents[f"{working_directory}/report.csv"] = b"1,2\n"
            return result

    producing = InProcessSandboxBackend(
        _Producing(default_stdout="done\n"),
        capabilities=DEFAULT_CAPABILITIES | {Capability.FILES_OUT},
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
        raise SystemExit(
            f"FAIL: the declared output did not land: {landed} / {saved!r}"
        )

    # The spec's `requires` has to travel in the wheel: a backend that cannot run a command
    # is refused as the tool attaches, not when the model first calls it.
    weak = InProcessSandboxBackend(capabilities=frozenset({Capability.FILES_IN}))
    try:
        make_codeact_tools(_router(weak), "data-analyst", context)
    except SandboxCapabilityNotSupported:
        pass
    else:
        raise SystemExit(
            "FAIL: a backend that cannot exec was allowed to serve execute_code"
        )

    return (
        "execute_code wrote the program into a directory of its own and ran the interpreter "
        "as argv; it shared a listed file and refused an unlisted one; it landed a declared "
        "output through the host's sink; and it refused a backend that cannot exec"
    )


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
        raise SystemExit(
            f"FAIL: wslc backend declares {backend.isolation!r}, expected container"
        )
    allowlisting = WslcSandboxBackend(WslcSandboxConfig(egress_proxy_image="x:1"))
    if backend.egress != Egress.CLOSED or allowlisting.egress != Egress.ALLOWLIST:
        raise SystemExit(f"FAIL: egress {backend.egress!r}/{allowlisting.egress!r}")
    # The proxy recipe is data, not code: a wheel that drops it breaks allowlist mode only here.
    dockerfile = proxy_build_context() / "Dockerfile"
    if not dockerfile.is_file():
        raise SystemExit(
            f"FAIL: the proxy build context is missing its Dockerfile ({dockerfile})"
        )
    return "backend constructs, declares its egress, and ships the proxy recipe"


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
        raise SystemExit(
            f"FAIL: docker backend declares {backend.isolation!r}, expected container"
        )
    if backend.capabilities != frozenset(
        {Capability.EXEC, Capability.FILES_IN, Capability.FILES_OUT}
    ):
        raise SystemExit(
            f"FAIL: docker backend declares {sorted(backend.capabilities)!r}"
        )
    allowlisting = DockerSandboxBackend(DockerSandboxConfig(egress_proxy_image="x:1"))
    if backend.egress != Egress.CLOSED or allowlisting.egress != Egress.ALLOWLIST:
        raise SystemExit(f"FAIL: egress {backend.egress!r}/{allowlisting.egress!r}")
    # The proxy recipe is data, not code: a wheel that drops it breaks allowlist mode only here.
    dockerfile = proxy_build_context() / "Dockerfile"
    if not dockerfile.is_file():
        raise SystemExit(
            f"FAIL: the proxy build context is missing its Dockerfile ({dockerfile})"
        )
    return "backend constructs, declares FILES_OUT and its egress, and ships the proxy recipe"


_SMOKES = {
    "maf-sandbox": _smoke_maf_sandbox,
    "maf-sandbox-acas": _smoke_maf_sandbox_acas,
    "maf-sandbox-bicep": _smoke_maf_sandbox_bicep,
    "maf-sandbox-codeact": _smoke_maf_sandbox_codeact,
    "maf-sandbox-docker": _smoke_maf_sandbox_docker,
    "maf-sandbox-wslc": _smoke_maf_sandbox_wslc,
}


def main(argv: list[str]) -> int:
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
        raise SystemExit(
            f"FAIL: {module_name} imported from {location}, not an installation"
        )

    _check_typing_marker(module)
    detail = _SMOKES[name]()
    print(f"SMOKE OK  {name}  ({location.parent})")
    print(f"          {detail}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
