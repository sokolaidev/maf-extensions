"""Two backends behind one router: who serves, who cannot, and who still gets cleaned up.

Which backend serves is a deployment decision made at construction. A spec can raise the bar
and be refused by the one that is serving; it cannot pick a different one. Disposal is the
exception and goes to every backend registered.

Needs a Docker-compatible engine. Read this directory's README for why the pairing is Docker
beside the in-process backend, and what #328 would change.
"""

# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "maf-sandbox-docker",
#     "maf-sandbox>=0.14",
# ]
# ///

from __future__ import annotations

import asyncio

from maf_sandbox import (
    DEFAULT_CAPABILITIES,
    Capability,
    Isolation,
    SandboxBackendNotPermitted,
    SandboxCapabilityNotSupported,
    SandboxKey,
    SandboxRouter,
    SandboxSpec,
)
from maf_sandbox.testing import InProcessSandboxBackend
from maf_sandbox_docker import DockerSandboxBackend, DockerSandboxConfig

#: A tiny image, because nothing here compiles anything — the point is which backend runs the
#: command, not what the command is.
IMAGE = "mcr.microsoft.com/devcontainers/python:3.13-bookworm"

#: One request's key. A host reads scope and thread from its own request context; this program
#: serves one request, so they are constants, and `dispose_scope` uses them at the end.
KEY = SandboxKey(scope="samples", thread_id="11-two-backends", agent_dir="operator")

#: The floor this host is willing to go down to. The router floor-checks the backend it
#: resolves to, not the whole list, and refuses at construction — `NONE` is the bottom
#: rung, so either of these clears it.
FLOOR = Isolation.NONE


def backends() -> tuple[InProcessSandboxBackend, DockerSandboxBackend]:
    """The two, with the declarations they make for themselves.

    Neither is configured to be weaker than the other for the sake of the demonstration, which
    is the reason for this pairing rather than two in-process instances: `docker` really does
    declare `CONTAINER` and `FILES_OUT`, and the in-process backend really does declare
    `NONE` and only `EXEC | FILES_IN`. Every refusal below follows from what they are.
    """
    return InProcessSandboxBackend(name="in-process"), DockerSandboxBackend(DockerSandboxConfig())


def serving(router: SandboxRouter) -> str:
    """The name of the backend ``router`` resolved to.

    `router.backend` is `SandboxBackend | None`, and `None` means no backend was registered at
    all. A host reading a backend list out of configuration writes this same narrowing.
    """
    backend = router.backend
    if backend is None:
        raise RuntimeError("no backend was registered")
    return backend.name


def act_one_the_switch() -> None:
    """`selected=` names which backend serves. The workload does not know the difference."""
    print("== 1. Which backend serves is one argument ==\n")

    local, container = backends()
    spec = SandboxSpec(kind="operator", image=IMAGE)

    for chosen in ("in-process", "docker"):
        router = SandboxRouter([local, container], min_isolation=FLOOR, selected=chosen)
        router.ensure_can_serve(spec)
        print(f"  selected={chosen!r:14} -> router.backend.name == {serving(router)!r}")

    # Registration order is the default, and it is worth seeing beside the explicit form: a
    # host that omits `selected=` gets whichever it happened to list first.
    default = SandboxRouter([local, container], min_isolation=FLOOR)
    print(
        f"  selected omitted        -> router.backend.name == {serving(default)!r}"
        "  (the first registered)\n"
    )


def act_two_the_spec_cannot_pick() -> None:
    """Both refusals, with a backend that could serve sitting registered and unused."""
    print("== 2. A spec may raise the bar. It may not change who serves ==\n")

    local, container = backends()
    # `docker` is registered throughout this act and is never reached. That is the lesson.
    router = SandboxRouter([local, container], min_isolation=FLOOR, selected="in-process")

    raises_floor = SandboxSpec(kind="operator", image=IMAGE, min_isolation=Isolation.CONTAINER)
    try:
        router.ensure_can_serve(raises_floor)
    except SandboxBackendNotPermitted as refusal:
        print(f"  spec asks for {Isolation.CONTAINER} isolation")
        print(f"    {type(refusal).__name__}: {refusal}\n")

    needs_files_out = SandboxSpec(
        kind="operator", image=IMAGE, requires=DEFAULT_CAPABILITIES | {Capability.FILES_OUT}
    )
    try:
        router.ensure_can_serve(needs_files_out)
    except SandboxCapabilityNotSupported as refusal:
        print(f"  spec requires {Capability.FILES_OUT}")
        print(f"    {type(refusal).__name__}: {refusal}\n")

    print(f"  Both refused while {container.name!r} — registered above, declaring")
    print(f"  {Isolation.CONTAINER} and {Capability.FILES_OUT} — sat unused. The router")
    print("  resolves one backend at construction and never reconsiders, so a spec the")
    print("  serving backend cannot meet is a refusal, not a reroute. Selecting per spec")
    print("  is #328; assuming it already happens is the dangerous way to be wrong, because")
    print("  the assumption is that something stronger quietly took over.\n")


async def act_three_disposal_reaches_everyone() -> tuple[int, int]:
    """The one place holding more than one backend is live at run time.

    Returns what it observed — sandboxes disposed, and how many backends were registered — so
    the footer reports measurements rather than the numbers this file expects.
    """
    print("== 3. Disposal goes to every registered backend ==\n")

    local, container = backends()
    registered = [local, container]
    router = SandboxRouter(registered, min_isolation=FLOOR, selected="docker")
    spec = SandboxSpec(kind="operator", image=IMAGE)

    # Everything from the first `acquire` sits in a `try`, and disposal in the `finally`, as in
    # every other container sample. One of these is a real Docker container: a raise between
    # creating it and disposing it — a failed write, a timed-out exec, a cancelled run — would
    # otherwise leave it on the machine, and the backend has no auto-delete timer to catch it.
    try:
        # A sandbox on each, the way a host that switched `selected=` between deployments would
        # have: one left on the backend no longer serving, one on the backend now serving.
        await local.acquire(KEY, spec)
        print(f"  acquired on {local.name!r} (not serving — a leftover from an earlier config)")
        sandbox = await router.acquire(KEY, spec)
        print(f"  acquired on {serving(router)!r} (serving)")
        # `write_file` before `exec`, and not only to have something to echo: a container starts
        # with nothing at `work_dir`, and `exec` would fail to chdir into a directory that does
        # not exist. Writing creates the parents, which is why a kind pushes its inputs first.
        await sandbox.write_file(f"{spec.work_dir}/marker", "routed\n")
        result = await sandbox.exec("cat marker", working_directory=spec.work_dir, timeout=60)
        print(f"  it runs: {result.stdout.strip()!r}\n")
    finally:
        disposed = await router.dispose_scope(KEY.scope, KEY.thread_id)
        print(f"  dispose_scope reached both backends and disposed {disposed} sandbox(es).")
    print("  Not only the serving one — which is the point: a host that changes `selected=`")
    print("  would otherwise leak whatever the previous choice left running, and on a")
    print("  billable backend that leak has a price.\n")
    return disposed, len(registered)


async def main() -> int:
    """Three acts. Both counts in the footer are read back, not written down."""
    act_one_the_switch()
    act_two_the_spec_cannot_pick()
    disposed, registered = await act_three_disposal_reaches_everyone()

    print(f"Completed 3 of 3 acts. Disposed {disposed} sandbox(es) across {registered} backends.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
