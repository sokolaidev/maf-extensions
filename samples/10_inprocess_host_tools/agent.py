"""What a host configures before a sandboxed program may call back into it — and what refuses it.

This is the `app` box again, one layer earlier than every other sample::

    app  ->  maf_sandbox (router)  ->  a backend  ->  the sandbox
     ^ everything below happens here, before a sandbox exists

`HOST_TOOLS` is the one capability where trust crosses *outward*: a dispatched body runs in
the host process, with the host's authority, driven by model-written code, and its call is
invisible to the host's middleware — the boundary sees only `execute_code`'s aggregate
result. So the interesting part is not the call. It is everything a host decides before one
is possible, and the router turning a whole kind away when that decision says no.

**Nothing is dispatched here, and nothing can be yet.** No shipped backend declares
`Capability.HOST_TOOLS` — the constant lives in `maf_sandbox` and in no backend — and the
transport that would carry a request out of a guest is still being designed (#133). That is
why this sample builds its own backend below: to have something that declares the capability,
the wiring is written by hand, and having to write it is the honest demonstration.

What *is* real today is everything a host gets wrong first: which functions are reachable at
all, what the surface as a whole then means, and whether this deployment permits it. All of
it is decided at attach, before a sandbox is acquired, so all of it runs here in a second
with no cloud account, no container runtime, no model and no configuration.

Read `host_tools.py` first — the four functions and their declarations — then the four acts
below.
"""

# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "maf-sandbox>=0.13",
# ]
# ///

from __future__ import annotations

import warnings

from host_tools import (
    fetch_changelog,
    publish_release_note,
    rerun_failed_jobs,
    semver_bump,
)
from maf_sandbox import (
    DEFAULT_CAPABILITIES,
    Capability,
    HostToolNotDeclared,
    HostToolRegistry,
    Identity,
    MafSandboxHostToolsWarning,
    SandboxCapabilityDenied,
    SandboxIdentityDenied,
    SandboxRouter,
    SandboxSpec,
)
from maf_sandbox.testing import InProcessSandboxBackend

#: The kind this host would be attaching. Named once; every act below builds a spec for it.
KIND = "release-notes"


def act_one_registration() -> HostToolRegistry:
    """Register the surface. The registry is the one door, and the gate is on.

    `require_declared=True` is the host saying it will not dispatch what nobody classified.
    It is worth turning on precisely because the library's default is off: a host that has
    not thought about the question gets the degrading behaviour, and a host that has gets to
    say so.
    """
    print("== 1. Registration ==\n")

    # The registration notice fires once per process and is informational — it says out loud
    # that dispatched calls bypass middleware. Shown rather than suppressed, because a reader
    # meeting this channel for the first time is exactly who it is written for.
    registry = HostToolRegistry(require_declared=True)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", MafSandboxHostToolsWarning)
        for tool in (semver_bump, fetch_changelog, publish_release_note):
            registry.register(tool)
    for warning in caught:
        print(f"  notice: {warning.message}\n")

    print(f"  registered: {', '.join(sorted(registry.names()))}")

    # The gate, doing its job at the configuration site.
    try:
        registry.register(rerun_failed_jobs)
    except HostToolNotDeclared as refusal:
        print(f"  refused:    rerun_failed_jobs — {refusal}")

    print(f"\n  the dispatchable surface is {len(registry)} functions, and nothing else.")
    print("  Least privilege here is what was registered, not what was declared.\n")
    return registry


def act_two_aggregate(registry: HostToolRegistry) -> frozenset[Identity]:
    """Ask the registry what its contents mean for the one model-facing tool.

    Per leg, over the relevant subset — not a summary of the three declarations but a
    different statement, about `execute_code` itself. Taking it *seals* the registry, which
    is the subtle part and the reason this returns rather than being read twice.
    """
    print("== 2. What the surface means ==\n")
    aggregate = registry.aggregate()

    print(f"  result_integrity:  {aggregate.result_integrity}")
    print("                     the weakest tier over sources only — fetch_changelog drags")
    print("                     the whole result down, and semver_bump cannot drag it up.")
    print(f"  outbound_caps:     {set(aggregate.outbound_caps) or '{}'}")
    print("                     verbatim and unfolded: confidentiality is the host's")
    print("                     vocabulary, and this library never guesses at an ordering.")
    print(f"  identities:        {{{', '.join(sorted(aggregate.identities))}}}")
    print(f"  requires_approval: {aggregate.requires_approval}")
    print("                     because one USER tool is enough — a single dispatch could")
    print("                     exercise the user's delegated authority.")
    print(f"  has_undeclared:    {aggregate.has_undeclared}  (the gate refused the fourth)")

    # Sealing. A host has now derived a policy view from this surface, so the surface stops
    # moving — otherwise a tool registered afterwards would be dispatched past a router that
    # never saw it.
    try:
        registry.register(semver_bump, name="semver_bump_again")
    except ValueError as refusal:
        print(f"\n  sealed:     {refusal}")

    print()
    return aggregate.identities


def act_three_permitted(identities: frozenset[Identity]) -> None:
    """A router that permits this, and the spec the aggregate feeds.

    `identities` is not re-derived from the registry here — it is carried from the aggregate,
    which is the only way to read it, and that is deliberate: taking the policy view is what
    seals the surface it describes.
    """
    print("== 3. A host that permits it ==\n")

    # No shipped backend declares HOST_TOOLS, so one is built that does. `InProcessSandboxBackend`
    # takes its capabilities as an argument, which makes it the honest way to show the permitted
    # shape without pretending a real backend serves this yet.
    backend = InProcessSandboxBackend(
        name="in-process (host tools declared by hand)",
        capabilities=DEFAULT_CAPABILITIES | {Capability.HOST_TOOLS},
    )
    router = SandboxRouter([backend], min_isolation=backend.isolation)
    spec = SandboxSpec(
        kind=KIND,
        requires=DEFAULT_CAPABILITIES | {Capability.HOST_TOOLS},
        identities=identities,
    )

    router.ensure_can_serve(spec)
    print(f"  ensure_can_serve({KIND!r}) returned. The kind may attach.")
    print("  One call is the whole of a host's wiring test — and the whole of this sample's")
    print("  happy path, because what a dispatch would do next has no transport yet.\n")


def act_four_refused(identities: frozenset[Identity]) -> None:
    """The two ways a host says no, on the two axes, both before a sandbox exists."""
    print("== 4. The two refusals ==\n")

    backend = InProcessSandboxBackend(
        capabilities=DEFAULT_CAPABILITIES | {Capability.HOST_TOOLS},
    )
    spec = SandboxSpec(
        kind=KIND,
        requires=DEFAULT_CAPABILITIES | {Capability.HOST_TOOLS},
        identities=identities,
    )

    # The capability axis: this deployment does not want the outward channel at all.
    closed = SandboxRouter(
        [backend],
        min_isolation=backend.isolation,
        denied_capabilities={Capability.HOST_TOOLS},
    )
    try:
        closed.ensure_can_serve(spec)
    except SandboxCapabilityDenied as refusal:
        print(f"  denied_capabilities={{HOST_TOOLS}}\n    {type(refusal).__name__}: {refusal}\n")

    # The identity axis: the channel is fine, model-orchestrated *user* authority is not.
    no_user = SandboxRouter(
        [backend],
        min_isolation=backend.isolation,
        denied_identities={Identity.USER},
    )
    try:
        no_user.ensure_can_serve(spec)
    except SandboxIdentityDenied as refusal:
        print(f"  denied_identities={{USER}}\n    {type(refusal).__name__}: {refusal}\n")

    print("  Both are PermissionError, both name the deployment's own setting, and both")
    print("  turn away the whole kind rather than one function — there is no partial")
    print("  attach. Drop publish_release_note from the registry and the second router")
    print("  serves this spec: the identities came from what was registered.\n")


def main() -> int:
    """Four acts, in the order a host meets them."""
    registry = act_one_registration()
    identities = act_two_aggregate(registry)
    act_three_permitted(identities)
    act_four_refused(identities)

    print("== What is not here ==\n")
    print("  A dispatch. No shipped backend declares Capability.HOST_TOOLS, and the")
    print("  transport a guest would send a request over is still being designed (#133).")
    print("  Everything above is the half a host configures on day one regardless, and it")
    print("  is the half that decides whether the other half ever runs.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
