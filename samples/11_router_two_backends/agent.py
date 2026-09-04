"""Two backends behind one router: who serves, who cannot, who still gets cleaned up.

Which backend serves is a deployment decision made at construction. A spec can raise the bar
and be refused by the one that is serving; it cannot pick a different one. Disposal is the
exception and goes to every backend registered.

Isolation is the axis acts 1 and 2 argue about. Egress is the other one, and acts 3 and 4 are
where it is shown — act 3 in arithmetic, act 4 with an agent doing work that cannot succeed
without the network it asked for. Act 6 is the same refusal as act 2, served instead: a host
that opts into per-spec selection gets the second backend tried. Read this directory's README
for why the pairing is Docker beside the in-process backend.
"""

# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "agent-framework-openai",
#     "azure-core[aio]",
#     "azure-identity",
#     "maf-sandbox-bicep",
#     "maf-sandbox-docker>=0.4",
#     "maf-sandbox>=0.32",
# ]
# ///

from __future__ import annotations

import asyncio
import re
from pathlib import Path

from _scaffold import MEASURED, evidence, installed_versions, quoted, require_env_vars, tool_results
from agent_framework import Agent, InMemoryAgentFileStore
from agent_framework.openai import OpenAIChatClient
from azure.identity.aio import DefaultAzureCredential
from maf_sandbox import (
    DEFAULT_CAPABILITIES,
    Capability,
    Egress,
    Isolation,
    SandboxBackendNotPermitted,
    SandboxCapabilityNotSupported,
    SandboxEgressNotEnforced,
    SandboxKey,
    SandboxRouter,
    SandboxSpec,
)
from maf_sandbox.maf import list_all_files, make_caller_context
from maf_sandbox.testing import InProcessSandboxBackend
from maf_sandbox_bicep import bicep_sandbox_spec, make_bicep_tools
from maf_sandbox_docker import BACKEND_NAME as DOCKER_BACKEND
from maf_sandbox_docker import (
    DockerSandboxBackend,
    DockerSandboxConfig,
    proxy_build_context,
)

#: Whether the installed core can select a backend **per spec**. `Selection` shipped with #328;
#: before it, `SandboxRouter` resolved one backend at construction and act 6 has nothing to
#: show. The import is the marker rather than a version comparison, for the reason sample 15
#: gives about its own: the symbol is what the code needs, where a version string is a proxy
#: for it. This sample resolves the *published* wheel, so it straddles the release rather than
#: waiting for it.
try:
    from maf_sandbox import Selection as _Selection
except ImportError:  # the published core before #328
    _Selection = None

#: A tiny image for acts 1, 2 and 5, because nothing there compiles anything — the point is
#: which backend runs the command, not what the command is. Act 4 runs a real compiler and
#: brings its own image.
IMAGE = "mcr.microsoft.com/devcontainers/python:3.13-bookworm"

#: One request's key. A host reads scope and thread from its own request context; this program
#: serves one request, so they are constants, and `dispose_scope` uses them at the end.
KEY = SandboxKey(scope="samples", thread_id="11-two-backends", agent_dir="operator")

#: The workload kind acts 1, 2 and 5 ask for. Named once because it is quoted back inside both
#: refusal messages: a typo in one of the four specs would still route, still refuse, and still
#: print a sentence about a kind this sample never mentions anywhere else.
KIND = "operator"

#: The floor this host is willing to go down to. The router floor-checks the backend it
#: resolves to, not the whole list, and refuses at construction — `NONE` is the bottom
#: rung, so either of these clears it.
FLOOR = Isolation.NONE

#: Act 4's agent, and the file it validates. The file lives beside this one and contains a
#: `br/public:` AVM module, which is what makes its egress needs real rather than illustrative.
BICEP_AGENT_DIR = "devops-engineer"
BICEP_FILE = "main.bicep"

#: The tool act 4 counts results from. What it returned is what the live check reads: the model
#: writes the prose around it, the framework writes this.
BICEP_TOOL = "bicep_validate"

#: The rule id a blocked module restore produces, and the only thing act 4 asks of the text.
#: Not "no diagnostics": the file is pinned to one module version, and a lint rule about newer
#: versions would some day add a warning that says nothing about egress. This one does.
_RESTORE_FAILED = "BCP192"

#: What a result that reached the sandbox looks like — sample 05's regex, for its reason.
#: `bicep_validate` answers with an error string *before* it acquires anything when no
#: conversation is bound, when a name has the wrong suffix, and when the sandbox will not
#: start, so counting every result would credit act 4 with a compile that never ran. Each
#: phase renders one line at the start of a line, which a refusal has no way to produce. It
#: matters more here than in sample 05: a blocked restore is a compile that ran and failed,
#: and act 4's whole claim is that it can tell that apart from a sandbox that never came up.
_PHASES = re.compile(r"^build\(.*^lint\(", re.MULTILINE | re.DOTALL)

#: Everything act 4 needs. The other four acts need none of it, so this is read inside act 4
#: rather than at startup — a reader with only Docker still sees four fifths of the sample.
#: `BICEP_SANDBOX_IMAGE` and `MAF_EGRESS_PROXY_IMAGE` are local image references, built rather
#: than pulled; the model is reached with `DefaultAzureCredential`, so there is no key here.
ACT_FOUR_VARS = (
    "BICEP_SANDBOX_IMAGE",
    "MAF_EGRESS_PROXY_IMAGE",
    "AZURE_OPENAI_ENDPOINT",
    "AZURE_OPENAI_CHAT_MODEL",
)


def backends() -> tuple[InProcessSandboxBackend, DockerSandboxBackend]:
    """The two, with the declarations they make for themselves.

    Neither is configured to be weaker than the other for the sake of the demonstration, which
    is the reason for this pairing rather than two in-process instances: `docker` really does
    declare `CONTAINER` and `FILES_OUT`, and the in-process backend really does declare
    `NONE` and only `EXEC | FILES_IN`. Every refusal below follows from what they are.

    Neither name is written down here, and the two get their names from different places —
    which is worth seeing, because a reader selecting a backend in their own host has to know
    which kind they are dealing with.

    `maf_sandbox_docker` exports `BACKEND_NAME`, imported above as `DOCKER_BACKEND`. That is
    what a host writes when it reads `selected=` out of configuration and has no backend built
    yet. Aliased at the import rather than taken bare because every backend package exports
    that same symbol, so a host registering two would have the second silently shadow the
    first.

    `InProcessSandboxBackend` exports none, and correctly: its name is a constructor parameter
    with a default, so it is the *caller's* to choose — that is how a host registers two of
    them apart. This sample registers one, lets it keep the default, and reads `.name` back off
    it.
    """
    return InProcessSandboxBackend(), DockerSandboxBackend(DockerSandboxConfig())


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
    spec = SandboxSpec(kind=KIND, image=IMAGE)

    # `DOCKER_BACKEND` is the package's own constant; `local.name` is read off the object,
    # because the in-process backend's name belongs to whoever constructed it. `backends()`
    # says why the two differ. Both are the same kind of value here — the string `selected=`
    # matches on — and neither is written out in this file.
    for chosen in (local.name, DOCKER_BACKEND):
        router = SandboxRouter([local, container], min_isolation=FLOOR, selected=chosen)
        router.ensure_can_serve(spec)
        print(f"{MEASURED}selected={chosen!r:14} -> router.backend.name == {serving(router)!r}")

    # Registration order is the default, and it is worth seeing beside the explicit form: a
    # host that omits `selected=` gets whichever it happened to list first.
    default = SandboxRouter([local, container], min_isolation=FLOOR)
    print(
        f"{MEASURED}selected omitted        -> router.backend.name == {serving(default)!r}"
        "  (the first registered)\n"
    )


def act_two_the_spec_cannot_pick() -> None:
    """Both refusals, with a backend that could serve sitting registered and unused."""
    print("== 2. A spec may raise the bar. It may not change who serves ==\n")

    local, container = backends()
    # `docker` is registered throughout this act and is never reached. That is the lesson.
    router = SandboxRouter([local, container], min_isolation=FLOOR, selected=local.name)

    raises_floor = SandboxSpec(kind=KIND, image=IMAGE, min_isolation=Isolation.CONTAINER)
    try:
        router.ensure_can_serve(raises_floor)
    except SandboxBackendNotPermitted as refusal:
        print(f"  spec asks for {Isolation.CONTAINER} isolation")
        print(f"{MEASURED}{type(refusal).__name__}: {refusal}\n")

    needs_files_out = SandboxSpec(
        kind=KIND, image=IMAGE, requires=DEFAULT_CAPABILITIES | {Capability.FILES_OUT}
    )
    try:
        router.ensure_can_serve(needs_files_out)
    except SandboxCapabilityNotSupported as refusal:
        print(f"  spec requires {Capability.FILES_OUT}")
        print(f"{MEASURED}{type(refusal).__name__}: {refusal}\n")

    print(f"  Both refused while {container.name!r} — registered above, declaring")
    print(f"  {Isolation.CONTAINER} and {Capability.FILES_OUT} — sat unused. The router")
    print("  resolves one backend at construction and never reconsiders, so a spec the")
    print("  serving backend cannot meet is a refusal, not a reroute. Selecting per spec")
    print("  is #328; assuming it already happens is the dangerous way to be wrong, because")
    print("  the assumption is that something stronger quietly took over.\n")


def act_three_the_other_axis() -> None:
    """Egress, and why a backend serves the exact mode a workload runs in or refuses it.

    The isolation axis above refuses in one direction only; egress does not substitute in either.
    A workload names the one posture it runs in, and the router serves it only on a backend that
    *enforces* that exact mode — never a more open one (a silent widening) nor a more isolated
    one (a quietly different posture called a success). Refuse, never degrade. No other sample
    shows it.
    """
    print("== 3. The other axis: what a backend can enforce ==\n")

    closed = DockerSandboxBackend(DockerSandboxConfig())
    # A reference that need not resolve: nothing is started here, and the modes are read off the
    # configuration rather than the image. Act 4 uses one that has to be real.
    allowlisting = DockerSandboxBackend(DockerSandboxConfig(egress_proxy_image="maf-egress-proxy"))
    print(
        f"  DockerSandboxConfig()                        -> {sorted(closed.declarations.egress_modes)}"
    )
    print(
        f"  DockerSandboxConfig(egress_proxy_image=...)   -> {sorted(allowlisting.declarations.egress_modes)}"
    )
    print("  Same backend class. The set is a fact about the deployment's wiring, and it is what")
    print("  a workload's chosen mode is matched against.\n")

    wants_a_host = SandboxSpec(
        kind=KIND, image=IMAGE, egress=Egress.ALLOWLIST, egress_allow=("mcr.microsoft.com",)
    )
    try:
        SandboxRouter([closed], min_isolation=FLOOR).ensure_can_serve(wants_a_host)
        print("  BUG: a closed-only backend served an ALLOWLIST run.")
    except SandboxEgressNotEnforced:
        print(
            f"  An ALLOWLIST run on a {sorted(closed.declarations.egress_modes)} backend: refused, not"
        )
        print("    degraded. The closed backend does not quietly serve it behind a closed")
        print("    boundary — the workload asked to reach a host, and this backend cannot.\n")

    runs_closed = SandboxSpec(kind=KIND, image=IMAGE)  # egress defaults to CLOSED
    SandboxRouter([closed], min_isolation=FLOOR).ensure_can_serve(runs_closed)
    SandboxRouter([allowlisting], min_isolation=FLOOR).ensure_can_serve(runs_closed)
    print("  A CLOSED run is served on both — both enforce CLOSED. A workload gets the exact")
    print("  mode it runs in or nothing; no shipped backend enforces `unrestricted`, so one that")
    print("  must run open needs a backend that offers exactly that. Act 4 is where the mode a")
    print(
        "  workload runs in stops being a word and becomes a module that restores, or does not.\n"
    )


async def _validate_under(
    proxy_image: str | None,
    env: dict[str, str],
    credential: DefaultAzureCredential,
    source: str,
) -> tuple[Egress, bool]:
    """Run one agent turn against one egress posture.

    Returns the posture the workload ran in and whether the module restored under it. The
    posture is chosen to match what the backend enforces — `ALLOWLIST` when a proxy image is
    wired, `CLOSED` when not — because the router serves the exact mode a workload runs in or
    refuses it, and a mode the backend cannot enforce would not run at all.

    Everything here except `proxy_image` (and the mode it dictates) is identical across the two
    calls — the same file, the same instructions, the same model. That is what makes the
    difference in the output attributable to the deployment's wiring and to nothing else.
    """
    backend = DockerSandboxBackend(DockerSandboxConfig(egress_proxy_image=proxy_image))
    posture = Egress.ALLOWLIST if proxy_image else Egress.CLOSED
    # Above the `NONE` floor the other acts use: a compiler running downloaded code has no
    # business in the host process, and this is the floor sample 05 opts down to as well.
    router = SandboxRouter([backend], min_isolation=Isolation.CONTAINER)

    # A thread of its own per posture. The two runs are separate conversations as far as the
    # router is concerned, so neither can be handed the other's sandbox by the reuse path.
    thread = f"{KEY.thread_id}-{posture}"

    store = InMemoryAgentFileStore()
    await store.write(BICEP_FILE, source)
    context = make_caller_context(list_all_files, lambda: KEY.scope, lambda: thread)

    # The allowlist hosts come from the kind, not this file; only the *mode* is chosen here, to
    # match what the backend enforces. A deployment cannot widen the hosts, only pick CLOSED,
    # ALLOWLIST or UNRESTRICTED — and here it picks the one the wiring can deliver.
    tools = make_bicep_tools(
        router, store, BICEP_AGENT_DIR, context, image=env["BICEP_SANDBOX_IMAGE"], egress=posture
    )
    agent = Agent(
        client=OpenAIChatClient(
            model=env["AZURE_OPENAI_CHAT_MODEL"],
            azure_endpoint=env["AZURE_OPENAI_ENDPOINT"],
            credential=credential,
        ),
        name=BICEP_AGENT_DIR,
        instructions=(
            "You validate Azure Bicep. Always call the bicep_validate tool and report "
            "exactly the diagnostics it returns — rule id, severity, file, line and "
            "message. Never judge the file from reading it, and never invent, reword or "
            "omit a diagnostic."
        ),
        tools=tools,
    )

    print(f"  --- egress {posture} ---\n")
    try:
        reply = await agent.run(f"Validate {BICEP_FILE} and list every diagnostic you get back.")
        # Quoted first, because the reply and the block below share one stream and the live
        # check trusts the `[measured]` tag completely.
        print(quoted(reply.text))
        # The compiler's own words, taken from the tool result rather than from the reply. A
        # model that never called the tool can still write a convincing summary of what it
        # would have said, and the whole claim of this act rests on which one of these ran.
        compiles = [result for result in tool_results(reply, BICEP_TOOL) if _PHASES.search(result)]
        print()
        print(
            evidence(
                f"bicep_validate under egress {posture}",
                compiles,
                "compiles that reached the sandbox",
            )
        )
        print()
        # No compile is not a restore. An empty list here means the sandbox never started, and
        # reporting that as a successful restore would turn the loudest failure into the
        # quietest one.
        restored = bool(compiles) and not any(_RESTORE_FAILED in result for result in compiles)
        return posture, restored
    finally:
        await router.dispose_scope(KEY.scope, thread)


def _verdict(restored: bool) -> str:
    """The two words `scripts/check_live_router_sample.py` matches on.

    Written here rather than inline at each call because they are a contract with a file in
    another directory, and a contract spelled in four places is one somebody edits in three.
    """
    return "RESTORED" if restored else "FAILED"


async def act_four_the_egress_the_workload_asked_for() -> tuple[bool, bool] | None:
    """The same agent, the same file, two deployments — and only one of them can do the work.

    Returns whether the module restored under each posture, or ``None`` when act 4 is not
    configured. The two booleans are read back from what the compiler said, so the footer
    reports what happened rather than what this file expects.
    """
    print("== 4. An allowlist the workload wrote, and work that needs it ==\n")

    env = require_env_vars(ACT_FOUR_VARS)
    if env is None:
        print("  Skipped. Act 4 needs a Bicep sandbox image, a built egress proxy and a")
        print("  model. The proxy is built from the installed package rather than pulled —")
        print("  the recipe ships with it rather than as an image you have to trust:\n")
        print(f"    docker build -t maf-egress-proxy:local {proxy_build_context()}")
        print("    docker build -t bicep-sandbox:local images/bicep-sandbox\n")
        print("  See this directory's README for the four variables and what each is for.\n")
        return None

    spec = bicep_sandbox_spec(image=env["BICEP_SANDBOX_IMAGE"])
    print(f"  {BICEP_FILE} uses a br/public AVM module, so the compiler cannot type-check it")
    print("  without downloading one. `bicep_sandbox_spec()` names what that download needs:\n")
    for host in spec.egress_allow:
        print(f"    {host}")
    print("\n  That list is the kind's, not this sample's. A deployment does not get to widen")
    print("  it, and a workload that needs a fifth host has to say so where the other four")
    print("  are written down.\n")

    source = (Path(__file__).parent / BICEP_FILE).read_text(encoding="utf-8")
    credential = DefaultAzureCredential()
    try:
        # `None` is "no proxy configured", which is what closed egress means here. An empty
        # string now says the same thing, but it did not always: it used to declare closed and
        # then fail trying to start a proxy with an unusable reference, fixed in 0.4.0 (#407).
        without, closed = await _validate_under(None, env, credential, source)
        with_proxy, allowlisted = await _validate_under(
            env["MAF_EGRESS_PROXY_IMAGE"], env, credential, source
        )
    finally:
        await credential.close()

    print(f"{MEASURED}AVM restore under egress {without}: {_verdict(closed)}")
    print(
        f"{MEASURED}AVM restore under egress {with_proxy}: {_verdict(allowlisted)} "
        f"({len(spec.egress_allow)} hosts allowed)"
    )
    print("\n  Both halves carry the claim. The failure shows the container has no route out")
    print("  that the deployment did not give it; the success shows the deployment gave it")
    print("  exactly the four the workload named. Either one alone is consistent with a")
    print("  sandbox that confines nothing.\n")
    return closed, allowlisted


async def act_five_disposal_reaches_everyone() -> tuple[int, int]:
    """The one place holding more than one backend is live at run time.

    Returns what it observed — sandboxes disposed, and how many backends were registered — so
    the footer reports measurements rather than the numbers this file expects.
    """
    print("== 5. Disposal goes to every registered backend ==\n")

    local, container = backends()
    registered = [local, container]
    # Named by the constant rather than by `container.name`, though both are in scope: this is
    # the line a host copies, and a host choosing from configuration has no backend to ask.
    router = SandboxRouter(registered, min_isolation=FLOOR, selected=DOCKER_BACKEND)
    spec = SandboxSpec(kind=KIND, image=IMAGE)

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
        await sandbox.write_file(
            f"{spec.work_dir}/marker", "routed\n", working_directory=spec.work_dir
        )
        result = await sandbox.exec("cat marker", working_directory=spec.work_dir, timeout=60)
        print(f"{MEASURED}it runs: {result.stdout.strip()!r}\n")
    finally:
        purge = await router.dispose_scope(KEY.scope, KEY.thread_id)
        print(
            f"{MEASURED}dispose_scope reached both backends and disposed {purge.disposed} sandbox(es)."
        )
    print("  Not only the serving one — which is the point: a host that changes `selected=`")
    print("  would otherwise leak whatever the previous choice left running, and on a")
    print("  billable backend that leak has a price.\n")
    return purge.disposed, len(registered)


async def act_six_the_spec_picks() -> bool:
    """Act 2's refusal, served — the same two backends, in the same order, one keyword apart.

    This is the whole of what per-spec selection changes, and running it directly after act 2
    is the argument: nothing about the backends differs, nothing about the spec differs, and
    the router reads past the first one only because the host asked it to.

    The *plain* spec is here for the property that makes the feature safe to have at all.
    Both backends can serve it, and it goes to the first registered one — the same backend the
    fixed selection resolves to. Routing can only ever serve what is refused today; it never
    moves a workload that already runs, which is why turning it on cannot quietly relocate
    existing traffic onto a backend that charges for it.

    Returns whether the act ran, so the footer counts what happened rather than what this file
    hoped for.
    """
    print("== 6. The spec picks, when the host asks it to ==\n")
    if _Selection is None:
        print(f"{MEASURED}core supports per-spec selection: no")
        print("  The published `maf-sandbox` this run resolved predates `Selection`, so there")
        print("  is nothing here to show. Every other act is the shipped behaviour on each")
        print("  version this sample runs against, and none of them is affected.\n")
        return False
    print(f"{MEASURED}core supports per-spec selection: yes")

    local, container = backends()
    # The same list, in the same order, as act 2 — which refused. `selection=` is the only
    # difference between the two routers, and `selected=` is absent because a pin and a route
    # are two answers to one question and the router refuses them together.
    router = SandboxRouter([local, container], min_isolation=FLOOR, selection=_Selection.PER_SPEC)

    needs_files_out = SandboxSpec(
        kind=KIND, image=IMAGE, requires=DEFAULT_CAPABILITIES | {Capability.FILES_OUT}
    )
    plain = SandboxSpec(kind=KIND, image=IMAGE)
    for label, spec in (("files_out", needs_files_out), ("plain", plain)):
        chosen = router.backend_for(spec)
        print(f"{MEASURED}routed {label} spec -> {(None if chosen is None else chosen.name)!r}")

    print()
    try:
        # Routing decided; this is what proves the decision reached a real container rather
        # than only a report about one. Same `finally` discipline as act 5, and for the same
        # reason: one of these is a Docker container with no auto-delete timer behind it.
        sandbox = await router.acquire(KEY, needs_files_out)
        await sandbox.write_file(
            f"{needs_files_out.work_dir}/marker",
            "routed per spec\n",
            working_directory=needs_files_out.work_dir,
        )
        result = await sandbox.exec(
            "cat marker", working_directory=needs_files_out.work_dir, timeout=60
        )
        print(f"{MEASURED}the routed backend runs: {result.stdout.strip()!r}")
    finally:
        # No total printed, and the omission is deliberate: one sandbox was created here,
        # and `dispose_scope` asks both backends — the in-process one answers a purge with
        # a fixed number of its own, so a total printed here would not be a measurement of
        # this act. Act 5 is where the disposal claim is made, over two acquires that
        # really happened on two backends.
        await router.dispose_scope(KEY.scope, KEY.thread_id)
        print("  the routed sandbox is disposed, by the same scope purge act 5 measures\n")

    print("  The refusal in act 2 and the route here differ by `selection=` and nothing else.")
    print("  It is off by default, and the reason is a bill rather than a scruple: what it")
    print("  changes is that a refusal becomes a running sandbox, and on a remote backend a")
    print("  running sandbox has a price. Registration order is the preference, and the route")
    print("  is a pure function of the spec — never load, latency or cost — so one spec")
    print("  always routes to the same backend and its warm sandbox stays reusable. Per")
    print("  spec rather than per conversation: two kinds under one key may route apart,")
    print("  by design, which is why disposal asks every registered backend.\n")
    return True


async def main() -> int:
    """Six acts, and every number in the footer is read back rather than written down.

    Two of the six can be absent from a healthy run, and each says so out loud: act 4 when its
    four variables are unset, act 6 when the core this run resolved predates the feature.
    """
    act_one_the_switch()
    act_two_the_spec_cannot_pick()
    act_three_the_other_axis()
    restored = await act_four_the_egress_the_workload_asked_for()
    disposed, registered = await act_five_disposal_reaches_everyone()
    routed = await act_six_the_spec_picks()

    ran = 4 + (1 if restored else 0) + (1 if routed else 0)
    print(
        f"{MEASURED}Completed {ran} of 6 acts. "
        f"Disposed {disposed} sandbox(es) across {registered} backends."
    )
    return 0


if __name__ == "__main__":
    print(installed_versions())
    raise SystemExit(asyncio.run(main()))
