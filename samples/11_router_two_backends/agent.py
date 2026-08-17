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
#     "maf-sandbox>=0.16",
# ]
# ///

from __future__ import annotations

import asyncio
import os

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
from maf_sandbox_docker import (
    DockerSandboxBackend,
    DockerSandboxConfig,
    proxy_build_context,
)

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

#: A built egress proxy image, or empty. Act 4 needs one to enforce an allowlist with and says
#: how to build it when it is missing, so the rest of the sample still runs without it.
PROXY_IMAGE = os.environ.get("MAF_EGRESS_PROXY_IMAGE", "")


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


def act_three_the_other_axis() -> None:
    """Egress, and why confining *more* than a spec asked is allowed while less is refused.

    The isolation axis above refuses in one direction only, and so does this one — but the
    direction is the interesting part, and no other sample shows it. A backend that cannot
    confine as *precisely* as the spec asked still serves, because denying a host the workload
    wanted makes the workload fail at the fetch, loudly. A backend that cannot confine *at all*
    is refused, because silently widening what a workload reaches has no such symptom.
    """
    print("== 3. The other axis: what a backend can confine ==\n")

    closed = DockerSandboxBackend(DockerSandboxConfig())
    allowlisting = DockerSandboxBackend(DockerSandboxConfig(egress_proxy_image=PROXY_IMAGE or "x"))
    print(f"  DockerSandboxConfig()                        -> {closed.egress}")
    print(f"  DockerSandboxConfig(egress_proxy_image=...)   -> {allowlisting.egress}")
    print("  Same backend class. The declaration is a fact about the deployment's wiring,")
    print("  not about the workload, which is why it is read off the backend and not the spec.\n")

    wants_a_host = SandboxSpec(kind="operator", image=IMAGE, egress_allow=("mcr.microsoft.com",))
    router = SandboxRouter([closed], min_isolation=FLOOR)
    # Served, not refused — and the router logs a warning naming the hosts that will be
    # unreachable. Printed here because a warning a reader never sees is the whole hazard.
    router.ensure_can_serve(wants_a_host)
    print(f"  A spec allowing {wants_a_host.egress_allow[0]!r} on a {closed.egress} backend:")
    print("    served. The allowlist is honoured by denying everything, which is more")
    print("    confinement than was asked for. The router warns; the workload will report")
    print("    what it could not fetch.\n")

    print("  The refused direction needs a backend declaring `unrestricted`, and none of the")
    print("  three shipped ones does — every backend here can at least deny everything. That")
    print("  asymmetry is the design: `Egress`'s own docstring is where it is written down.\n")


async def act_four_an_allowlist_that_is_enforced() -> tuple[str, str] | None:
    """Reach one host and fail at another, from inside the same sandbox.

    Returns the two HTTP statuses so the footer reports what happened rather than what this
    file hoped for, or ``None`` when there is no proxy image to enforce anything with.
    """
    print("== 4. An allowlist, enforced ==\n")

    if not PROXY_IMAGE:
        print("  Skipped: set MAF_EGRESS_PROXY_IMAGE to a built proxy image. Build it from")
        print("  the installed package — the recipe ships with it rather than as an image")
        print("  you have to trust:\n")
        print(f"    docker build -t maf-egress-proxy:local {proxy_build_context()}")
        print("    MAF_EGRESS_PROXY_IMAGE=maf-egress-proxy:local uv run agent.py\n")
        return None

    backend = DockerSandboxBackend(DockerSandboxConfig(egress_proxy_image=PROXY_IMAGE))
    spec = SandboxSpec(kind="operator", image=IMAGE, egress_allow=("mcr.microsoft.com",))
    router = SandboxRouter([backend], min_isolation=FLOOR)

    async def status(sandbox, url: str) -> str:
        result = await sandbox.exec(
            ["sh", "-c", f"curl -s -o /dev/null -w '%{{http_code}}' --max-time 25 {url}"],
            working_directory=spec.work_dir,
            timeout=45,
        )
        return result.stdout.strip()

    try:
        sandbox = await router.acquire(KEY, spec)
        # The first write creates the working directory `exec -w` needs, as in act 5.
        await sandbox.write_file(f"{spec.work_dir}/.keep", "")
        allowed = await status(sandbox, "https://mcr.microsoft.com/v2/")
        denied = await status(sandbox, "https://pypi.org/simple/")
    finally:
        await router.dispose_scope(KEY.scope, KEY.thread_id)

    print(f"  {spec.egress_allow[0]:<20} (allowed) -> HTTP {allowed}")
    print(f"  {'pypi.org':<20} (not named) -> HTTP {denied}")
    print("  `000` is curl reporting no connection at all: the proxy refused the tunnel.")
    print("  Nothing in the container was configured to obey the allowlist — the container")
    print("  has no route out except the proxy, so the topology enforces it rather than the")
    print("  HTTP_PROXY variables, which only tell ordinary clients where to look.\n")
    return allowed, denied


async def act_five_disposal_reaches_everyone() -> tuple[int, int]:
    """The one place holding more than one backend is live at run time.

    Returns what it observed — sandboxes disposed, and how many backends were registered — so
    the footer reports measurements rather than the numbers this file expects.
    """
    print("== 5. Disposal goes to every registered backend ==\n")

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
    """Five acts. Every number in the footer is read back, not written down."""
    act_one_the_switch()
    act_two_the_spec_cannot_pick()
    act_three_the_other_axis()
    reached = await act_four_an_allowlist_that_is_enforced()
    disposed, registered = await act_five_disposal_reaches_everyone()

    ran = 5 if reached else 4
    print(
        f"Completed {ran} of 5 acts. Disposed {disposed} sandbox(es) across {registered} backends."
    )
    if reached:
        allowed, denied = reached
        print(f"Allowlisted host answered HTTP {allowed}; an unlisted one answered HTTP {denied}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
