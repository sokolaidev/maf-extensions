"""What a host configures before a sandboxed program may call back into it, in four acts.

Registration, the aggregate the registry folds to, a router that permits the spec built from
it, and the two axes a router refuses on. Every one of them is answered at attach, so this
runs with no sandbox, no model and no configuration.

Why the channel is shaped this way, and why nothing is dispatched, is in this directory's
README.
"""

# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "maf-sandbox>=0.18",
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


def act_three_permitted(identities: frozenset[Identity]) -> InProcessSandboxBackend:
    """A router that permits this, and the spec the aggregate feeds.

    `identities` is not re-derived from the registry here — it is carried from the aggregate,
    which is the only way to read it, and that is deliberate: taking the policy view is what
    seals the surface it describes.
    """
    print("== 3. A host that permits it ==\n")

    # Docker and ACAS declare HOST_TOOLS, but reaching either needs an engine or a subscription
    # and this sample needs neither. `InProcessSandboxBackend` takes its capabilities as an
    # argument, so the permitted shape can be shown with nothing installed. The router does not
    # know the difference — a declaration is a declaration, whoever made it.
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
    print("  happy path, because a dispatch needs a guest program and this sample runs none.\n")
    # Handed back so `main` can read what it recorded rather than assert what it expects:
    # `InProcessSandboxBackend` appends to `keys` on every `acquire`.
    return backend


def act_four_refused(identities: frozenset[Identity]) -> InProcessSandboxBackend:
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
            identities=narrowed.identities,
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
    identities = act_two_aggregate(registry)
    done.append("aggregate")
    backends.append(act_three_permitted(identities))
    done.append("permitted")
    backends.append(act_four_refused(identities))
    done.append("refused")

    print("== What is not here ==\n")
    print("  A dispatch. The transport a guest sends a request over has landed (#327),")
    print("  maf-sandbox-codeact dispatches over it, and the docker and acas backends")
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
