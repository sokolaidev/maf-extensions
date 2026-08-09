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
    "maf-sandbox-aca": "maf_sandbox_aca",
    "maf-sandbox-bicep": "maf_sandbox_bicep",
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
    router = SandboxRouter([backend])
    key = SandboxKey(scope="s", thread_id="t", agent_dir="a")
    sandbox = asyncio.run(router.acquire(key, SandboxSpec(kind="smoke")))
    result = asyncio.run(sandbox.exec("true", working_directory="/w", timeout=5))
    if result.stdout != "ok":
        raise SystemExit(f"FAIL: in-process sandbox returned {result.stdout!r}")

    # The rule that is a security property, not a convenience: a deployed host must refuse
    # anything weaker than a VM boundary. If this silently stopped raising, every consumer
    # relying on it would be quietly unprotected.
    from maf_sandbox import SandboxBackendNotPermitted

    try:
        SandboxRouter([backend], deployed=True)
    except SandboxBackendNotPermitted:
        pass
    else:
        raise SystemExit("FAIL: a deployed router accepted a process-isolated backend")

    _ = InMemoryStore({"a": "b"}), Isolation.VM
    return "router + in-process backend + deployed-isolation rule"


def _smoke_maf_sandbox_aca() -> str:
    from maf_sandbox import Isolation
    from maf_sandbox_aca import AcaSandboxBackend, AcaSandboxConfig

    # Constructed, not called: this asserts the package imports with its real preview SDK
    # resolved and still declares the boundary the router gates on. Reaching the service
    # would need credentials and would not test packaging.
    backend = AcaSandboxBackend(AcaSandboxConfig(endpoint="https://example.invalid"))
    if backend.isolation != Isolation.VM:
        raise SystemExit(
            f"FAIL: aca backend declares {backend.isolation!r}, expected vm"
        )
    return "backend constructs and declares vm isolation"


def _smoke_maf_sandbox_bicep() -> str:
    from maf_sandbox import SandboxRouter, WorkspaceContext
    from maf_sandbox.testing import (
        InMemoryStore,
        InProcessSandbox,
        InProcessSandboxBackend,
    )
    from maf_sandbox_bicep import BICEP_VALIDATE_TOOL_NAME, make_bicep_tools

    def _bicep_tool(store: InMemoryStore, backend: InProcessSandboxBackend):
        context = WorkspaceContext(
            current_scope=lambda: "smoke",
            current_thread_id=lambda: "thread",
            list_files=InMemoryStore.list,
        )
        tools = make_bicep_tools(
            SandboxRouter([backend]),
            store,
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


_SMOKES = {
    "maf-sandbox": _smoke_maf_sandbox,
    "maf-sandbox-aca": _smoke_maf_sandbox_aca,
    "maf-sandbox-bicep": _smoke_maf_sandbox_bicep,
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
