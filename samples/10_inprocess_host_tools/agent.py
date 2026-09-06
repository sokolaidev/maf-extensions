"""What a host configures before a sandboxed program may call back into it, in four acts.

Registration, the aggregate the registry folds to, a router that permits the spec built from
it, and the two axes a router refuses on. Every one of them is answered at attach, so this
runs with no sandbox, no model and no configuration.

Why the channel is shaped this way, and why no host-tool call is made, is in this directory's
README.
"""

# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "maf-sandbox>=0.34",
# ]
# ///

from __future__ import annotations

import warnings

from _scaffold import installed_versions
from host_tools import (
    fetch_changelog,
    publish_release_note,
    rerun_failed_jobs,
    semver_bump,
)
from maf_sandbox import (
    DEFAULT_CAPABILITIES,
    BackendDeclarations,
    Capability,
    Egress,
    HostToolAggregate,
    HostToolIdentityNotAllowed,
    HostToolNotDeclared,
    HostToolRegistry,
    Identity,
    MafSandboxHostToolsWarning,
    SandboxCapabilityDenied,
    SandboxIdentityDenied,
    SandboxLimits,
    SandboxRouter,
    SandboxSpec,
    TransferLimits,
)
from maf_sandbox.testing import InProcessSandboxBackend

#: Wide enough for the fold. Attaching a surface makes the router add the host-tool call
#: transport's worst case to *both* directions, and it does that whether or not the backend has
#: a transport at all — this one runs in-process and has none, so the headroom is for arithmetic
#: the match performs rather than for bytes that will move. `files_out` needs the most room: a
#: whole run's response budget folds into its per-file cap, so trimming it back reinstates the
#: refusal this constant exists to avoid.
_ROOMY = SandboxLimits(
    files_in=TransferLimits(1 << 26, 1 << 31, 4096),
    files_out=TransferLimits(1 << 26, 1 << 31, 4096),
)

#: What a backend that would serve this sample's workload declares: the capability the surface
#: needs, and ceilings the fold fits inside.
_DECLARES_HOST_TOOLS = BackendDeclarations(
    capabilities=DEFAULT_CAPABILITIES | {Capability.HOST_TOOLS},
    limits=_ROOMY,
    egress_modes=frozenset({Egress.ALLOWLIST, Egress.CLOSED}),
)

#: The kind this host would be attaching. Named once; every act below builds a spec for it.
KIND = "release-notes"


def act_one_registration() -> HostToolRegistry:
    """Register the surface. The registry is the one door, and two gates guard it.

    `require_declared=True` is the host saying it will not call what nobody classified as a
    host tool. `allowed_identities` is the host saying which authorities it will run at all:
    the default is `{Identity.APP}`, so a tool exercising the *user's* authority is refused at
    registration until the host opts in. Both gates refuse at the configuration site, where
    the fix is one line away, rather than later at the call.
    """
    print("== 1. Registration ==\n")

    # The identity gate, before anything else is built. A default registry is APP-only, so the
    # tool that acts as the user is refused at registration. Declaring it as APP to dodge this
    # would be the lie the identity leg exists to prevent — so the host opts in below,
    # deliberately and in one place, rather than weakening the declaration.
    app_only = HostToolRegistry(require_declared=True)
    try:
        app_only.register(publish_release_note)
    except HostToolIdentityNotAllowed as refusal:
        print(f"  refused:    publish_release_note (registry is APP-only) — {refusal}\n")

    # The registration notice fires once per process and is informational — it says out loud
    # that host-tool calls bypass middleware. Shown rather than suppressed, because a reader
    # meeting this channel for the first time is exactly who it is written for.
    registry = HostToolRegistry(
        require_declared=True,
        allowed_identities=frozenset({Identity.APP, Identity.USER}),
    )
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", MafSandboxHostToolsWarning)
        for tool in (semver_bump, fetch_changelog, publish_release_note):
            registry.register(tool)
    for warning in caught:
        print(f"  notice: {warning.message}\n")

    print(f"  registered: {', '.join(sorted(registry.names()))}")

    # The declaration gate, doing its job at the configuration site.
    try:
        registry.register(rerun_failed_jobs)
    except HostToolNotDeclared as refusal:
        print(f"  refused:    rerun_failed_jobs — {refusal}")

    print(f"\n  the callable surface is {len(registry)} functions, and nothing else.")
    print("  Least privilege here is what was registered, not what was declared.\n")
    return registry


def act_two_aggregate(registry: HostToolRegistry) -> HostToolAggregate:
    """Ask the registry what its contents mean for the one model-facing tool.

    Per leg, over the relevant subset — not a summary of the three declarations but a
    different statement, about `execute_code` itself. Taking it *seals* the registry, which
    is the subtle part and the reason this returns rather than being read twice.
    """
    print("== 2. What the surface means ==\n")
    aggregate = registry.aggregate()

    print(f"  result_integrity:  {aggregate.result_integrity}")
    print("                     the weakest level over sources only — fetch_changelog drags")
    print("                     the whole result down, and semver_bump cannot drag it up.")
    print(f"  outbound_caps:     {set(aggregate.outbound_caps) or '{}'}")
    print("                     verbatim and unfolded: confidentiality is the host's")
    print("                     vocabulary, and this library never guesses at an ordering.")
    print(f"  identities:        {{{', '.join(sorted(aggregate.identities))}}}")
    print(f"  requires_approval: {aggregate.requires_approval}")
    print("                     because one USER tool is enough — a single host-tool call")
    print("                     could exercise the user's delegated authority.")
    print(f"  has_undeclared:    {aggregate.has_undeclared}  (the gate refused the fourth)")

    # Sealing. A host has now derived a policy view from this surface, so the surface stops
    # moving — otherwise a tool registered afterwards would be called past a router that
    # never saw it.
    try:
        registry.register(semver_bump, name="semver_bump_again")
    except ValueError as refusal:
        print(f"\n  sealed:     {refusal}")

    print()
    return aggregate


def act_three_permitted(surface: HostToolAggregate) -> InProcessSandboxBackend:
    """A router that permits this, and the spec the aggregate feeds.

    The surface is carried from act two rather than re-derived, and that is deliberate: taking
    the policy view is what seals it. The spec's `identities` are read straight off it, so a
    host cannot be shown one posture while the surface carries another.
    """
    print("== 3. A host that permits it ==\n")

    # Docker and ACAS declare HOST_TOOLS, but reaching either needs an engine or a subscription
    # and this sample needs neither. `InProcessSandboxBackend` takes its declarations as an
    # argument, so the permitted shape can be shown with nothing installed. The router does not
    # know the difference — a declaration is a declaration, whoever made it.
    backend = InProcessSandboxBackend(
        name="in-process (host tools declared by hand)",
        declarations=_DECLARES_HOST_TOOLS,
    )
    router = SandboxRouter([backend], min_isolation=backend.isolation)
    spec = SandboxSpec(
        kind=KIND,
        requires=DEFAULT_CAPABILITIES | {Capability.HOST_TOOLS},
        host_tools=surface,
    )

    router.ensure_can_serve(spec)
    print(f"  ensure_can_serve({KIND!r}) returned. The kind may attach.")
    print("  One call is the whole of a host's wiring test — and the whole of this sample's")
    print(
        "  happy path, because a host-tool call needs a guest program and this sample runs none.\n"
    )
    # Handed back so `main` can read what it recorded rather than assert what it expects:
    # `InProcessSandboxBackend` appends to `keys` on every `acquire`.
    return backend


def act_four_refused(surface: HostToolAggregate) -> InProcessSandboxBackend:
    """The two ways a host says no, on the two axes, both before a sandbox exists."""
    print("== 4. The two refusals ==\n")

    backend = InProcessSandboxBackend(declarations=_DECLARES_HOST_TOOLS)
    spec = SandboxSpec(
        kind=KIND,
        requires=DEFAULT_CAPABILITIES | {Capability.HOST_TOOLS},
        host_tools=surface,
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
    print("  attach.\n")

    # The way out, run rather than described. It is not a narrower call against the objects
    # above: `spec` is frozen, `registry` sealed when its aggregate was taken, and there is no
    # unregister — all three deliberate, so a surface a policy was derived from cannot quietly
    # change. Narrowing means going back to the registration site and building the smaller
    # surface from the start, which is what "least privilege comes from what a host registers"
    # costs in practice.
    narrower = HostToolRegistry(require_declared=True)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", MafSandboxHostToolsWarning)
        for tool in (semver_bump, fetch_changelog):  # no publish_release_note
            narrower.register(tool)
    narrowed = narrower.aggregate()
    no_user.ensure_can_serve(
        SandboxSpec(
            kind=KIND,
            requires=DEFAULT_CAPABILITIES | {Capability.HOST_TOOLS},
            host_tools=narrowed,
        )
    )
    print("  The way past the second refusal is a different registry, not a different call:")
    print(
        f"    a registry without publish_release_note folds to identities="
        f"{{{', '.join(sorted(narrowed.identities))}}}, requires_approval="
        f"{narrowed.requires_approval},"
    )
    print("    and the same denied_identities router serves the spec built from it.")
    print("    Least privilege is what a host registers, and the cost of that is real:")
    print("    the spec is frozen, the registry sealed, and there is no unregister.\n")
    return backend


def main() -> int:
    """Four acts, in the order a host meets them."""
    # Both counts in the footer are read back from real state rather than written down. The
    # acts, because a literal would still say four after one stopped running; the sandboxes,
    # because a literal zero is not an observation of anything — and that number is the
    # sample's central claim, so it is the last one that may be asserted rather than measured.
    done: list[str] = []
    backends: list[InProcessSandboxBackend] = []

    registry = act_one_registration()
    done.append("registration")
    surface = act_two_aggregate(registry)
    done.append("aggregate")
    backends.append(act_three_permitted(surface))
    done.append("permitted")
    backends.append(act_four_refused(surface))
    done.append("refused")

    print("== What is not here ==\n")
    print("  A host-tool call. The transport a guest sends a request over has landed (#327),")
    print("  maf-sandbox-codeact makes host-tool calls over it, and the docker and acas backends")
    print("  declare Capability.HOST_TOOLS — so one would run. It needs a real sandbox,")
    print("  a guest program and a model, and this sample uses none of the three (#302).")
    print("  Everything above is the half a host configures on day one regardless, and it")
    print("  is the half that decides whether the other half ever runs.\n")

    # `InProcessSandboxBackend` appends to `keys` on every `acquire`, so this counts what the
    # backends were actually asked for. Every other sample's footer counts the sandboxes it
    # disposed and its check requires at least one; this one requires zero, because the claim
    # is that all four acts are answered at attach and no backend is ever reached.
    acquired = sum(len(backend.keys) for backend in backends)
    print(f"Completed {len(done)} of 4 acts. Acquired {acquired} sandbox(es).")
    return 0


if __name__ == "__main__":
    print(installed_versions())
    raise SystemExit(main())
