"""Tests for the sandbox router.

The router has exactly five jobs, and all of them are tested here rather than inferred:

- picking a backend from configuration;
- refusing a backend below the minimum-isolation floor the host — or a spec — requires;
- refusing a backend that cannot do what a workload's spec requires;
- refusing a spec whose transfer caps sit above what the backend allows;
- refusing a backend that cannot confine egress to what a workload's spec allows.

The floor, the transfer ceilings and the egress rule are security properties. A shared-kernel
container sits next to the host's credentials, and the posture a deployment claims rests on
the boundary it actually got — so "the router would refuse" needs to be a test, not a comment.
"""

from __future__ import annotations

import asyncio
import dataclasses

import pytest

from maf_sandbox import (
    DEFAULT_CAPABILITIES,
    DEFAULT_SANDBOX_LIMITS,
    DEFAULT_TRANSFER_LIMITS,
    ISOLATION_RANK,
    Capability,
    DeclaredOutput,
    Egress,
    EntryKind,
    Identity,
    Isolation,
    NoSandboxBackend,
    OutputDisposition,
    SandboxBackend,
    SandboxBackendNotPermitted,
    SandboxCapabilityDenied,
    SandboxCapabilityNotSupported,
    SandboxEgressNotEnforced,
    SandboxEntry,
    SandboxIdentityDenied,
    SandboxKey,
    SandboxLimits,
    SandboxPurger,
    SandboxRouter,
    SandboxSpec,
    SandboxTransferLimitsNotPermitted,
    TransferLimits,
    meets_floor,
)
from maf_sandbox.testing import InProcessSandboxBackend

_KEY = SandboxKey(scope="scope-a", thread_id="thread-1", agent_dir="devops-engineer")
_SPEC = SandboxSpec(kind="test")


class _ExplodingBackend(InProcessSandboxBackend):
    async def dispose_scope(self, scope: str, thread_id: str) -> int:
        raise RuntimeError("service unavailable")

    async def dispose(self, key) -> None:
        raise RuntimeError("service unavailable")


class TestProtocolConformance:
    def test_a_fake_backend_satisfies_the_protocol(self):
        """If this fails the fakes below have drifted from the contract they stand in for."""
        assert isinstance(InProcessSandboxBackend(), SandboxBackend)


class TestSelection:
    """Every fake here declares `process` isolation, so these routers opt below the floor."""

    def test_no_backends_means_not_enabled(self):
        router = SandboxRouter([])
        assert router.enabled is False
        assert router.backend is None

    def test_defaults_to_the_first_registered_backend(self):
        first, second = (
            InProcessSandboxBackend(name="first"),
            InProcessSandboxBackend(name="second"),
        )
        router = SandboxRouter([first, second], min_isolation=Isolation.PROCESS)
        assert router.backend is first
        assert router.enabled is True

    def test_selects_by_name(self):
        first, second = (
            InProcessSandboxBackend(name="first"),
            InProcessSandboxBackend(name="second"),
        )
        router = SandboxRouter([first, second], min_isolation=Isolation.PROCESS, selected="second")
        assert router.backend is second

    def test_unknown_name_raises_with_the_registered_ones_named(self):
        with pytest.raises(NoSandboxBackend, match="registered: aca, fake"):
            SandboxRouter(
                [InProcessSandboxBackend(name="fake"), InProcessSandboxBackend(name="aca")],
                min_isolation=Isolation.PROCESS,
                selected="docker",
            )

    def test_acquire_without_a_backend_raises(self):
        with pytest.raises(NoSandboxBackend):
            asyncio.run(SandboxRouter([]).acquire(_KEY, _SPEC))

    def test_acquire_delegates_to_the_selected_backend(self):
        backend = InProcessSandboxBackend()
        router = SandboxRouter([backend], min_isolation=Isolation.PROCESS)
        sandbox = asyncio.run(router.acquire(_KEY, _SPEC))
        assert backend.keys == [_KEY]
        assert sandbox is backend.sandbox


class TestIsolationLadder:
    """The ladder is data, and its order is load-bearing: `meets_floor` is a rank comparison."""

    def test_every_member_is_ranked(self):
        """An unranked rung would raise `KeyError` inside a policy check, at attach time."""
        assert set(ISOLATION_RANK) == set(Isolation)

    def test_the_ranks_are_dense_and_distinct(self):
        assert sorted(ISOLATION_RANK.values()) == list(range(len(Isolation)))

    def test_the_order_runs_from_no_boundary_to_the_strongest_one(self):
        assert list(ISOLATION_RANK) == [
            Isolation.PROCESS,
            Isolation.RUNTIME,
            Isolation.CONTAINER,
            Isolation.HARDENED_CONTAINER,
            Isolation.MICROVM,
            Isolation.VM,
        ]

    def test_a_rung_compares_and_serializes_as_its_string_value(self):
        """Declarations and configuration are plain strings; `StrEnum` keeps them working."""
        assert Isolation.CONTAINER == "container"
        assert str(Isolation.CONTAINER) == "container"
        assert Isolation(str(Isolation.CONTAINER)) is Isolation.CONTAINER

    def test_an_unknown_string_does_not_become_a_rung(self):
        """The constructor's `ValueError` *is* the refuse-unknown policy."""
        with pytest.raises(ValueError, match="quantum"):
            Isolation("quantum")

    @pytest.mark.parametrize(
        ("declared", "permitted"),
        [
            (Isolation.CONTAINER, False),
            (Isolation.HARDENED_CONTAINER, False),
            (Isolation.MICROVM, True),
            (Isolation.VM, True),
        ],
    )
    def test_meets_floor_admits_the_floor_itself_and_everything_above_it(self, declared, permitted):
        assert meets_floor(declared, Isolation.MICROVM) is permitted


class TestIsolationFloor:
    """Supersedes the deployed-isolation rule: the host declares a minimum-isolation floor.

    (Two-axis sandbox policy, axis 1.) `deployed=True` was one boolean standing in for "at
    least a hypervisor boundary". A floor says which rung, defaults to that rung, and makes a
    developer machine opt down explicitly rather than depend on a flag nobody set.
    """

    def test_the_default_floor_is_a_micro_vm(self):
        """A host that configures nothing gets the production posture."""
        router = SandboxRouter([InProcessSandboxBackend(isolation=Isolation.MICROVM)])
        assert router.enabled is True

    @pytest.mark.parametrize(
        "isolation",
        [
            Isolation.PROCESS,
            Isolation.RUNTIME,
            Isolation.CONTAINER,
            Isolation.HARDENED_CONTAINER,
        ],
    )
    def test_a_backend_below_the_default_floor_is_refused(self, isolation):
        with pytest.raises(SandboxBackendNotPermitted, match="minimum-isolation floor"):
            SandboxRouter([InProcessSandboxBackend(name="docker", isolation=isolation)])

    def test_the_refusal_happens_at_construction_not_at_first_use(self):
        """A misconfigured deployment must not start with the feature apparently enabled."""
        with pytest.raises(SandboxBackendNotPermitted):
            SandboxRouter([InProcessSandboxBackend(name="docker", isolation=Isolation.CONTAINER)])

    def test_a_host_opts_down_explicitly(self):
        router = SandboxRouter(
            [InProcessSandboxBackend(name="docker", isolation=Isolation.CONTAINER)],
            min_isolation=Isolation.CONTAINER,
        )
        assert router.enabled is True

    def test_a_stricter_floor_refuses_the_rung_below_it(self):
        """A host that wants dedicated infrastructure raises the floor above `microvm`."""
        with pytest.raises(SandboxBackendNotPermitted, match="minimum-isolation floor"):
            SandboxRouter(
                [InProcessSandboxBackend(isolation=Isolation.MICROVM)], min_isolation=Isolation.VM
            )

    def test_an_unknown_isolation_value_is_refused_at_any_floor(self):
        """Refused, not guessed at: the router cannot rank a rung it has never heard of."""
        with pytest.raises(SandboxBackendNotPermitted, match="not a rung"):
            SandboxRouter(
                [InProcessSandboxBackend(name="lab", isolation="quantum")],
                min_isolation=Isolation.PROCESS,
            )

    @pytest.mark.parametrize("bad", ["MICROVM", "micro-vm"])
    def test_a_malformed_floor_string_raises_valueerror_not_keyerror(self, bad):
        """Coerced through `Isolation()` at construction, not left for `meets_floor` to `KeyError` on later.

        With no backend registered, nothing else in `__init__` would ever have looked at
        `min_isolation` to catch it.
        """
        with pytest.raises(ValueError, match="not a valid Isolation"):
            SandboxRouter([], min_isolation=bad)

    def test_it_refuses_rather_than_falling_back_to_a_stronger_backend(self):
        """Falling back would hide the misconfiguration — the whole reason this is an error."""
        docker = InProcessSandboxBackend(name="docker", isolation=Isolation.CONTAINER)
        aca = InProcessSandboxBackend(name="aca", isolation=Isolation.MICROVM)
        with pytest.raises(SandboxBackendNotPermitted):
            SandboxRouter([docker, aca], selected="docker")

    def test_an_unselected_weak_backend_does_not_poison_a_valid_selection(self):
        docker = InProcessSandboxBackend(name="docker", isolation=Isolation.CONTAINER)
        aca = InProcessSandboxBackend(name="aca", isolation=Isolation.MICROVM)
        assert SandboxRouter([aca, docker], selected="aca").backend is aca


class TestSpecFloorRaise:
    """A spec may raise the host's floor and never lower it — the owners stay separate.

    How strong the boundary must be *here* is the host's policy; "this kind refuses to run
    below `microvm` anywhere" is a property of the workload.
    """

    def _router(self, isolation: Isolation, floor: Isolation) -> SandboxRouter:
        return SandboxRouter([InProcessSandboxBackend(isolation=isolation)], min_isolation=floor)

    def test_a_spec_asking_above_the_backends_rung_is_refused(self):
        router = self._router(Isolation.CONTAINER, Isolation.CONTAINER)
        with pytest.raises(SandboxBackendNotPermitted, match="requires at least"):
            router.ensure_can_serve(SandboxSpec(kind="codeact", min_isolation=Isolation.MICROVM))

    def test_a_spec_asking_for_exactly_the_backends_rung_is_served(self):
        router = self._router(Isolation.CONTAINER, Isolation.CONTAINER)
        router.ensure_can_serve(SandboxSpec(kind="codeact", min_isolation=Isolation.CONTAINER))

    def test_a_lax_spec_is_served_by_a_backend_already_at_the_hosts_floor(self):
        """Not a live check of "never lower" — that property is structural, not behavioural.

        `SandboxRouter.__init__` already refuses any backend below `min_isolation`, so by the
        time `ensure_can_serve` runs, the selected backend already meets the host's floor; a
        spec asking for less than that can only ever be served. `_effective_floor`'s `max`
        only ever raises the floor, never lowers it — the leg that actually exercises a raise
        is `test_a_spec_asking_above_the_backends_rung_is_refused`, above.
        """
        router = self._router(Isolation.MICROVM, Isolation.MICROVM)
        router.ensure_can_serve(SandboxSpec(kind="codeact", min_isolation=Isolation.PROCESS))

    def test_a_spec_with_no_opinion_leaves_the_floor_to_the_host(self):
        self._router(Isolation.PROCESS, Isolation.PROCESS).ensure_can_serve(_SPEC)


class _BackendWithoutCapabilities:
    """A backend written before the capability axis existed: it declares what it had.

    Written out rather than subclassed, because the fake now always has the property. It has no
    ``limits`` either, which makes it the only fixture here that exercises *both* of the
    router's `getattr` fallbacks — see `TestTransferLimitMatch`.
    """

    name = "legacy"
    isolation = Isolation.MICROVM
    egress = Egress.ALLOWLIST

    async def acquire(self, key: SandboxKey, spec: SandboxSpec) -> object:
        return object()

    async def dispose(self, key: SandboxKey) -> None:
        return None

    async def dispose_scope(self, scope: str, thread_id: str) -> int:
        return 0


class TestCapabilityMatch:
    """What a backend can *do*, matched against what a workload needs — axis 2.

    Its own exception, because the fix is its own: register a backend that implements the
    capability, or ask for less. Nothing here is a boundary claim.
    """

    def _router(self, **kwargs) -> SandboxRouter:
        return SandboxRouter([InProcessSandboxBackend(**kwargs)], min_isolation=Isolation.PROCESS)

    def test_a_capability_compares_as_its_string_value(self):
        assert Capability.RUN_CODE == "run_code"

    def test_the_default_set_is_what_every_sandbox_already_owes(self):
        assert DEFAULT_CAPABILITIES == frozenset({Capability.EXEC, Capability.FILES_IN})

    def test_a_backend_declaring_a_superset_serves_the_spec(self):
        router = self._router(capabilities=DEFAULT_CAPABILITIES | {Capability.RUN_CODE})
        router.ensure_can_serve(
            SandboxSpec(kind="codeact", requires=frozenset({Capability.RUN_CODE}))
        )

    def test_a_missing_capability_is_refused_and_named(self):
        with pytest.raises(SandboxCapabilityNotSupported, match="run_code"):
            self._router().ensure_can_serve(
                SandboxSpec(
                    kind="codeact", requires=frozenset({Capability.EXEC, Capability.RUN_CODE})
                )
            )

    def test_a_backend_that_declares_nothing_is_read_as_the_default_set(self):
        """Silence is a functionality claim, not a safety one — `Sandbox` already owes both."""
        router = SandboxRouter([_BackendWithoutCapabilities()])
        router.ensure_can_serve(SandboxSpec(kind="test"))
        with pytest.raises(SandboxCapabilityNotSupported):
            router.ensure_can_serve(
                SandboxSpec(kind="codeact", requires=frozenset({Capability.FILES_OUT}))
            )


class TestRouterDenials:
    """The hard stop: capabilities and identities a host refuses whatever the backend can do.

    A posture statement about the *spec*, not a property of the backend — which is why the
    denial fires even when the backend genuinely implements the capability, and why its
    exception is a `PermissionError` where the capability *match* is a `RuntimeError`.
    """

    def _router(self, **kwargs) -> SandboxRouter:
        backend = InProcessSandboxBackend(
            capabilities=DEFAULT_CAPABILITIES | {Capability.HOST_TOOLS}
        )
        return SandboxRouter([backend], min_isolation=Isolation.PROCESS, **kwargs)

    def test_a_denied_capability_is_refused_even_when_the_backend_has_it(self):
        router = self._router(denied_capabilities={Capability.HOST_TOOLS})
        spec = SandboxSpec(kind="codeact", requires=DEFAULT_CAPABILITIES | {Capability.HOST_TOOLS})
        with pytest.raises(SandboxCapabilityDenied, match="host_tools"):
            router.ensure_can_serve(spec)

    def test_a_spec_not_requiring_the_denied_capability_is_served(self):
        """The denial reads `requires`: a workload that dispatches nothing is untouched."""
        self._router(denied_capabilities={Capability.HOST_TOOLS}).ensure_can_serve(
            SandboxSpec(kind="test")
        )

    def test_acquire_enforces_the_denial_too(self):
        router = self._router(denied_capabilities={Capability.HOST_TOOLS})
        spec = SandboxSpec(kind="codeact", requires=DEFAULT_CAPABILITIES | {Capability.HOST_TOOLS})
        with pytest.raises(SandboxCapabilityDenied):
            asyncio.run(router.acquire(_KEY, spec))

    def test_a_denied_identity_is_refused_at_attach(self):
        """`denied_identities={USER}` is the one-statement ban on model-orchestrated user
        authority the identity leg exists to make possible."""
        router = self._router(denied_identities={Identity.USER})
        spec = SandboxSpec(kind="codeact", identities=frozenset({Identity.USER}))
        with pytest.raises(SandboxIdentityDenied, match="user"):
            router.ensure_can_serve(spec)

    def test_an_undenied_identity_is_served(self):
        router = self._router(denied_identities={Identity.USER})
        router.ensure_can_serve(SandboxSpec(kind="codeact", identities=frozenset({Identity.APP})))

    def test_an_unknown_denied_capability_is_refused_at_construction(self):
        """A deny list that silently never matched would read as protection and provide none."""
        with pytest.raises(ValueError):
            self._router(denied_capabilities={"teleport"})  # type: ignore[arg-type]

    def test_an_unknown_denied_identity_is_refused_at_construction(self):
        with pytest.raises(ValueError):
            self._router(denied_identities={"root"})  # type: ignore[arg-type]


class TestFileTransferVocabulary:
    """The closed sets the pull surface speaks in, each member written out here on purpose.

    Same shape as the `ISOLATION_RANK` exhaustiveness test above: a member added to either enum
    fails this until someone says what it means, rather than reaching a backend as a value
    nobody decided the handling of.
    """

    def test_every_entry_kind_is_accounted_for(self):
        assert set(EntryKind) == {EntryKind.FILE, EntryKind.DIRECTORY, EntryKind.OTHER}

    def test_every_disposition_is_accounted_for(self):
        assert set(OutputDisposition) == {OutputDisposition.LAND, OutputDisposition.CONSUME}

    @pytest.mark.parametrize(
        ("member", "value"),
        [
            (EntryKind.FILE, "file"),
            (EntryKind.DIRECTORY, "directory"),
            (EntryKind.OTHER, "other"),
            (OutputDisposition.LAND, "land"),
            (OutputDisposition.CONSUME, "consume"),
        ],
    )
    def test_a_member_compares_and_serializes_as_its_string_value(self, member, value: str):
        """Backends outside this repository report strings; `StrEnum` keeps them matching."""
        assert member == value
        assert str(member) == value
        assert type(member)(value) is member

    @pytest.mark.parametrize("enum", [EntryKind, OutputDisposition])
    def test_an_unknown_string_does_not_become_a_member(self, enum):
        """The constructor's `ValueError` *is* the refuse-unknown policy."""
        with pytest.raises(ValueError, match="junction"):
            enum("junction")

    def test_a_size_the_backend_could_not_determine_is_representable(self):
        """`None`, not `0` — a cap that read an unmeasurable file as free would pass it."""
        assert SandboxEntry(path="out.png", kind=EntryKind.FILE, size_bytes=None).size_bytes is None

    def test_a_declared_output_lands_and_is_required_unless_said_otherwise(self):
        """The safe defaults: an artifact goes to the sink, and a missing one is an error."""
        output = DeclaredOutput(path="out.png")
        assert output.disposition is OutputDisposition.LAND
        assert output.required is True
        assert output.media_type is None

    @pytest.mark.parametrize(
        "name",
        [
            "DEFAULT_SANDBOX_LIMITS",
            "DEFAULT_TRANSFER_LIMITS",
            "DeclaredOutput",
            "EntryKind",
            "OutputDisposition",
            "SandboxEntry",
            "SandboxLimits",
            "SandboxTransferLimitsNotPermitted",
            "TransferLimits",
        ],
    )
    def test_the_vocabulary_is_importable_from_the_package(self, name: str):
        """A kind declares its outputs and a backend its ceilings — both need these names."""
        import maf_sandbox

        assert name in maf_sandbox.__all__
        assert hasattr(maf_sandbox, name)


#: Well under the silent default on every axis, so a spec asking for it is served everywhere.
_TIGHT_LIMITS = TransferLimits(max_bytes_per_file=1024, max_total_bytes=4096, max_files=2)


class TestTransferLimits:
    """One constant on both sides of the match, and a `within` that checks every field."""

    def test_the_default_is_within_itself(self):
        """The invariant that keeps this axis inert: spec default and backend default are one.

        Get it wrong in the other direction — a spec default above what a silent backend is
        assumed to allow — and *every* spec fails at attach, the published wheel's smoke test
        included.
        """
        assert DEFAULT_TRANSFER_LIMITS.within(DEFAULT_TRANSFER_LIMITS) is True

    def test_a_backend_that_declares_nothing_is_read_as_that_same_constant(self):
        assert DEFAULT_SANDBOX_LIMITS.files_in is DEFAULT_TRANSFER_LIMITS
        assert DEFAULT_SANDBOX_LIMITS.files_out is DEFAULT_TRANSFER_LIMITS

    def test_asking_for_less_than_the_ceiling_is_within_it(self):
        assert _TIGHT_LIMITS.within(DEFAULT_TRANSFER_LIMITS) is True

    @pytest.mark.parametrize("field_name", [f.name for f in dataclasses.fields(TransferLimits)])
    def test_a_single_field_over_the_ceiling_is_enough_to_be_outside_it(self, field_name: str):
        """Read off the dataclass, so a fourth cap cannot be added without being checked."""
        over = dataclasses.replace(
            DEFAULT_TRANSFER_LIMITS,
            **{field_name: getattr(DEFAULT_TRANSFER_LIMITS, field_name) + 1},
        )
        assert over.within(DEFAULT_TRANSFER_LIMITS) is False


#: The two ways a backend ends up on the default ceiling: declaring it, and declaring nothing
#: at all.  The router's `getattr` fallback is the only thing that makes those the same, so
#: every leg below that says "the default ceiling" runs against both — a fallback pointed at a
#: different constant would pass the first and fail the second.  Classes, not instances, so the
#: parametrized cases do not share one object.
_DEFAULT_CEILING_BACKENDS = [
    pytest.param(InProcessSandboxBackend, id="declares-the-default"),
    pytest.param(_BackendWithoutCapabilities, id="declares-nothing"),
]


class TestTransferLimitMatch:
    """A spec's caps against a backend's ceilings — the third declaration read by `getattr`.

    Read like `egress` rather than like `capabilities`: a cap is a safety claim, so an
    undeclared one resolves to the default ceiling and a bigger ask is refused, where an
    undeclared capability set is read charitably.
    """

    _CEILING = SandboxLimits(files_in=_TIGHT_LIMITS, files_out=_TIGHT_LIMITS)

    def _router(self, backend) -> SandboxRouter:
        return SandboxRouter([backend], min_isolation=Isolation.PROCESS)

    def test_the_two_fixtures_really_are_one_declared_and_one_silent(self):
        """A `getattr` fallback nobody is on the far side of would pass by accident forever.

        The shared fake grew a `limits` property, so it stopped being evidence about silence;
        if the legacy fake ever grows one too, both parametrized cases below become the same
        case and stop covering the fallback at all.
        """
        assert InProcessSandboxBackend().limits == DEFAULT_SANDBOX_LIMITS
        assert not hasattr(_BackendWithoutCapabilities(), "limits")

    @pytest.mark.parametrize("backend_class", _DEFAULT_CEILING_BACKENDS)
    def test_the_default_ceiling_serves_every_existing_spec(self, backend_class):
        """Including `SandboxSpec(kind="smoke")`, which `scripts/smoke_install.py` acquires."""
        router = self._router(backend_class())
        router.ensure_can_serve(_SPEC)
        router.ensure_can_serve(SandboxSpec(kind="smoke"))

    @pytest.mark.parametrize("backend_class", _DEFAULT_CEILING_BACKENDS)
    def test_a_spec_the_default_ceiling_cannot_hold_is_refused(self, backend_class):
        """An absent ceiling is the default one, not "no ceiling" — the `Egress` rule."""
        huge = dataclasses.replace(
            DEFAULT_TRANSFER_LIMITS, max_files=DEFAULT_TRANSFER_LIMITS.max_files + 1
        )
        with pytest.raises(SandboxTransferLimitsNotPermitted):
            self._router(backend_class()).ensure_can_serve(
                SandboxSpec(kind="diagram", files_out=huge)
            )

    def test_a_spec_within_the_declared_ceilings_is_served(self):
        router = self._router(InProcessSandboxBackend(limits=self._CEILING))
        router.ensure_can_serve(
            SandboxSpec(kind="diagram", files_in=_TIGHT_LIMITS, files_out=_TIGHT_LIMITS)
        )

    @pytest.mark.parametrize("direction", ["files_in", "files_out"])
    def test_a_spec_above_the_ceiling_is_refused_and_the_direction_named(self, direction: str):
        router = self._router(InProcessSandboxBackend(limits=self._CEILING))
        spec = dataclasses.replace(
            SandboxSpec(kind="diagram", files_in=_TIGHT_LIMITS, files_out=_TIGHT_LIMITS),
            **{direction: DEFAULT_TRANSFER_LIMITS},
        )
        with pytest.raises(SandboxTransferLimitsNotPermitted, match=direction):
            router.ensure_can_serve(spec)

    def test_acquire_refuses_it_too(self):
        """`acquire` runs the same checks, so a caller that skips `ensure_can_serve` is caught."""
        router = self._router(InProcessSandboxBackend(limits=self._CEILING))
        spec = SandboxSpec(
            kind="diagram", files_in=_TIGHT_LIMITS, files_out=DEFAULT_TRANSFER_LIMITS
        )
        with pytest.raises(SandboxTransferLimitsNotPermitted, match="files_out"):
            asyncio.run(router.acquire(_KEY, spec))


class _BackendDeclaringTheWrongLimits(InProcessSandboxBackend):
    """A third party that reached for the adjacent type, or left the field unfilled."""

    def __init__(self, declared, **kwargs):
        super().__init__(**kwargs)
        self._declared = declared

    @property
    def limits(self):
        return self._declared


class TestAMalformedLimitsDeclarationIsRefused:
    """The same refuse-unknown policy `_declared_isolation` applies, for the same reason.

    `TransferLimits` is one direction's caps and `SandboxLimits` is the pair; they are adjacent
    in one module and both exported, so handing over the wrong one is the obvious third-party
    mistake — and it used to surface as a bare `AttributeError` out of a host's agent factory,
    which is where a host's own wiring test is least able to say what happened.
    """

    @pytest.mark.parametrize("declared", [_TIGHT_LIMITS, None], ids=["a-TransferLimits", "None"])
    def test_a_declaration_that_is_not_a_sandbox_limits_is_refused_and_named(self, declared):
        router = SandboxRouter(
            [_BackendDeclaringTheWrongLimits(declared)], min_isolation=Isolation.PROCESS
        )
        with pytest.raises(SandboxTransferLimitsNotPermitted, match="SandboxLimits"):
            router.ensure_can_serve(_SPEC)

    def test_acquire_refuses_it_too(self):
        router = SandboxRouter(
            [_BackendDeclaringTheWrongLimits(None)], min_isolation=Isolation.PROCESS
        )
        with pytest.raises(SandboxTransferLimitsNotPermitted):
            asyncio.run(router.acquire(_KEY, _SPEC))

    def test_the_default_ceilings_are_still_what_silence_means(self):
        """The guard refuses a declaration it cannot read — it does not make one mandatory."""
        SandboxRouter(
            [_BackendWithoutCapabilities()], min_isolation=Isolation.PROCESS
        ).ensure_can_serve(_SPEC)


class _BackendWithoutEgress:
    """A third-party backend that satisfied the protocol as it stood: one property short.

    Written out rather than subclassed, because the fake now always has the property.
    """

    name = "legacy"
    isolation = Isolation.VM

    async def acquire(self, key: SandboxKey, spec: SandboxSpec) -> object:
        return object()

    async def dispose(self, key: SandboxKey) -> None:
        return None

    async def dispose_scope(self, scope: str, thread_id: str) -> int:
        return 0


class TestEgressRule:
    """The security property nothing used to check.

    A backend that reads `egress_allow` and one that ignores it have the same type, the same
    methods and the same passing tests, so the difference has to be declared — and both
    directions of missing it pinned, since only one of them is refused.
    """

    _ALLOWLIST_SPEC = SandboxSpec(kind="bicep", egress_allow=("example.invalid",))
    _CLOSED_SPEC = SandboxSpec(kind="bicep")

    def _router(self, egress: str) -> SandboxRouter:
        return SandboxRouter(
            [InProcessSandboxBackend(egress=egress)], min_isolation=Isolation.PROCESS
        )

    @pytest.mark.parametrize("spec", [_ALLOWLIST_SPEC, _CLOSED_SPEC])
    def test_an_allowlist_backend_serves_any_spec(self, spec: SandboxSpec):
        self._router(Egress.ALLOWLIST).ensure_can_serve(spec)

    def test_a_declaration_still_matches_when_it_is_a_plain_string(self):
        """Backends outside this repository declare strings; `StrEnum` keeps them matching."""
        assert Egress.ALLOWLIST == "allowlist"
        self._router("allowlist").ensure_can_serve(self._ALLOWLIST_SPEC)

    def test_a_closed_backend_serves_a_spec_that_wants_no_network(self, caplog):
        with caplog.at_level("WARNING"):
            self._router(Egress.CLOSED).ensure_can_serve(self._CLOSED_SPEC)
        assert caplog.records == []

    def test_a_closed_backend_serves_an_allowlist_spec_but_says_so(self, caplog):
        with caplog.at_level("WARNING"):
            self._router(Egress.CLOSED).ensure_can_serve(self._ALLOWLIST_SPEC)
        (record,) = caplog.records
        # Off the spec, not a literal: the warning is useful only if it names the hosts.
        assert all(host in record.getMessage() for host in self._ALLOWLIST_SPEC.egress_allow)

    @pytest.mark.parametrize("spec", [_ALLOWLIST_SPEC, _CLOSED_SPEC])
    def test_an_unrestricted_backend_is_refused(self, spec: SandboxSpec):
        with pytest.raises(SandboxEgressNotEnforced):
            self._router(Egress.UNRESTRICTED).ensure_can_serve(spec)

    def test_a_backend_that_declares_nothing_is_refused(self):
        """Absent and unenforced are the same thing from the outside, so they land the same."""
        router = SandboxRouter([_BackendWithoutEgress()])
        with pytest.raises(SandboxEgressNotEnforced):
            router.ensure_can_serve(self._ALLOWLIST_SPEC)

    def test_no_backend_configured_is_not_an_egress_failure(self):
        """Nothing runs, so nothing reaches anything — and no tool is attached either."""
        SandboxRouter([]).ensure_can_serve(self._ALLOWLIST_SPEC)


class TestAcquireEnforcesPolicy:
    """`acquire` refuses on the same three grounds as `ensure_can_serve`, minus its warning.

    Before this, `acquire` delegated straight to the backend: a caller who never called
    `ensure_can_serve` first got no floor, capability or egress check at all. The
    closed-egress-vs-allowlist-spec WARNING stays `ensure_can_serve`-only, because a warm
    fix-round loop calls `acquire` every iteration and would otherwise log it every time.
    """

    def test_acquire_refuses_a_spec_above_the_backends_rung(self):
        router = SandboxRouter(
            [InProcessSandboxBackend(isolation=Isolation.CONTAINER)],
            min_isolation=Isolation.CONTAINER,
        )
        spec = SandboxSpec(kind="codeact", min_isolation=Isolation.MICROVM)
        with pytest.raises(SandboxBackendNotPermitted, match="requires at least"):
            asyncio.run(router.acquire(_KEY, spec))

    def test_acquire_refuses_a_missing_capability(self):
        router = SandboxRouter([InProcessSandboxBackend()], min_isolation=Isolation.PROCESS)
        spec = SandboxSpec(
            kind="codeact", requires=frozenset({Capability.EXEC, Capability.RUN_CODE})
        )
        with pytest.raises(SandboxCapabilityNotSupported, match="run_code"):
            asyncio.run(router.acquire(_KEY, spec))

    def test_acquire_refuses_an_unrestricted_egress_backend(self):
        router = SandboxRouter(
            [InProcessSandboxBackend(egress=Egress.UNRESTRICTED)], min_isolation=Isolation.PROCESS
        )
        with pytest.raises(SandboxEgressNotEnforced):
            asyncio.run(router.acquire(_KEY, _SPEC))

    def test_a_closed_backend_warns_on_ensure_can_serve_but_not_on_acquire(self, caplog):
        router = SandboxRouter(
            [InProcessSandboxBackend(egress=Egress.CLOSED)], min_isolation=Isolation.PROCESS
        )
        spec = SandboxSpec(kind="bicep", egress_allow=("example.invalid",))

        with caplog.at_level("WARNING"):
            router.ensure_can_serve(spec)
        assert len(caplog.records) == 1

        caplog.clear()
        with caplog.at_level("WARNING"):
            asyncio.run(router.acquire(_KEY, spec))
        assert caplog.records == []


class TestPurge:
    def test_dispose_scope_asks_every_backend_not_only_the_selected_one(self):
        """A conversation may have been served while a different backend was configured."""
        first, second = (
            InProcessSandboxBackend(name="first"),
            InProcessSandboxBackend(name="second"),
        )
        router = SandboxRouter([first, second], min_isolation=Isolation.PROCESS)
        total = asyncio.run(router.dispose_scope("scope-a", "thread-1"))
        assert total == 2
        assert first.purged == second.purged == [("scope-a", "thread-1")]

    def test_a_failing_backend_does_not_stop_the_others(self):
        good = InProcessSandboxBackend(name="good")
        router = SandboxRouter(
            [_ExplodingBackend(name="bad"), good], min_isolation=Isolation.PROCESS
        )
        total = asyncio.run(router.dispose_scope("scope-a", "thread-1"))
        assert total == 1
        assert good.purged == [("scope-a", "thread-1")]

    def test_dispose_never_raises(self):
        router = SandboxRouter([_ExplodingBackend()], min_isolation=Isolation.PROCESS)
        asyncio.run(router.dispose(_KEY))

    def test_purger_is_duck_typed_on_purge_scoped_thread(self):
        """The host awaits this without importing the class, so the name is the contract."""
        backend = InProcessSandboxBackend()
        purger = SandboxPurger(SandboxRouter([backend], min_isolation=Isolation.PROCESS))
        assert asyncio.run(purger.purge_scoped_thread("scope-a", "thread-1")) == 1
        assert backend.purged == [("scope-a", "thread-1")]


class TestSpecDefaults:
    def test_egress_defaults_to_denying_everything(self):
        """A spec that forgets to mention egress must get the closed configuration."""
        assert SandboxSpec(kind="test").egress_allow == ()

    def test_labels_are_not_shared_between_specs(self):
        a, b = SandboxSpec(kind="a"), SandboxSpec(kind="b")
        a.labels["x"] = "1"
        assert b.labels == {}

    def test_it_requires_only_what_every_sandbox_already_owes(self):
        """So a spec written before the capability axis asks for nothing new."""
        assert SandboxSpec(kind="test").requires == DEFAULT_CAPABILITIES

    def test_it_states_no_isolation_opinion(self):
        """`None`, not `PROCESS` — the second would be an opinion, and the weakest one."""
        assert SandboxSpec(kind="test").min_isolation is None

    def test_it_exercises_no_identities(self):
        """A workload that dispatches nothing declares nothing — and passes every deny list."""
        assert SandboxSpec(kind="test").identities == frozenset()

    def test_it_declares_no_outputs(self):
        """A tuple, as `egress_allow` is: a spec that collects nothing declares nothing."""
        assert SandboxSpec(kind="test").declared_outputs == ()

    def test_it_does_not_claim_to_name_outputs_later_either(self):
        """The flag makes a tool require a sink and carry an outbound cap, so silence is off."""
        assert SandboxSpec(kind="test").outputs_named_at_call_time is False

    def test_a_new_field_is_appended_rather_than_grouped_where_it_reads_best(self):
        """This dataclass is public and is not keyword-only, so a field inserted before another
        rebinds every positional argument after it — a caller's `files_in` would silently
        become the next field, with no error anywhere. New fields go on the end."""
        names = [f.name for f in dataclasses.fields(SandboxSpec)]
        # A prefix rather than the whole list, so a field appended at the end needs no edit
        # here — but a field joins this list in the change that adds it, because from that
        # moment on it is a position a caller can pass. Leaving the newest one out would
        # leave exactly one field unguarded: the next insertion could land in front of it
        # and this test would still be green.
        settled = [
            "kind",
            "image",
            "image_id",
            "egress_allow",
            "work_dir",
            "labels",
            "requires",
            "min_isolation",
            "declared_outputs",
            "files_in",
            "files_out",
            "outputs_named_at_call_time",
            "identities",
        ]
        assert names[: len(settled)] == settled

    @pytest.mark.parametrize("direction", ["files_in", "files_out"])
    def test_both_transfer_directions_ask_for_the_shared_default(self, direction: str):
        """The same object the silent backend ceiling is, so the two cannot drift apart."""
        assert getattr(SandboxSpec(kind="test"), direction) is DEFAULT_TRANSFER_LIMITS

    def test_work_dir_defaults_to_a_conventional_root(self):
        """A default, not a requirement — see `TestWorkDirIsPlatformNeutral` for why it is only that."""
        assert SandboxSpec(kind="test").work_dir == "/work"


class TestWorkDirIsPlatformNeutral:
    """The door issue #111 asks to keep open: nothing here commits the protocol to a Linux guest.

    The invariant: nothing in the protocol infers a guest OS or rejects a `work_dir` for not
    looking like one, so a platform axis can be added as a new optional field rather than a
    breaking change. Rationale and the decision to defer live in #111.
    """

    @pytest.mark.parametrize(
        "work_dir",
        [
            "C:/agent/work",  # a Windows guest's drive-rooted path
            "/opt/somewhere/else",  # a different POSIX root
            r"D:\agent\work",  # a Windows guest's own separator — raw, or `\a` is a bell
        ],
    )
    def test_the_spec_imposes_no_platform_constraint_on_work_dir(self, work_dir: str):
        """`SandboxSpec` accepts any `work_dir`: it neither requires a POSIX-absolute path nor
        infers a guest OS from it, so a future non-Linux-guest backend needs no protocol change."""
        assert SandboxSpec(kind="test", work_dir=work_dir).work_dir == work_dir

    def test_the_spec_has_no_platform_field_yet(self):
        """The axis is unbuilt: a spec carries no platform requirement. Adding one later is
        additive (a new optional field), which is the whole point of leaving it out now."""
        fields = {f.name for f in dataclasses.fields(SandboxSpec)}
        assert "platform" not in fields
        assert "requires_platform" not in fields

    def test_a_backend_declaring_no_platform_is_still_a_backend(self):
        """The three optional declarations the router reads are all `getattr`-based, so a fourth
        (a platform) is additive too — a backend that declares none still satisfies the protocol
        and is served, which is what makes the axis a non-breaking addition."""
        backend = InProcessSandboxBackend(isolation=Isolation.MICROVM)
        assert not hasattr(backend, "platform")
        # It resolves and serves today, so adding a getattr-read platform later breaks nothing.
        SandboxRouter([backend]).ensure_can_serve(SandboxSpec(kind="test"))


class TestPolicyVocabularyExports:
    """The package's public vocabulary — a name a host cannot import is a name it cannot use."""

    @pytest.mark.parametrize(
        "name",
        [
            "Capability",
            "DEFAULT_CAPABILITIES",
            "HostToolRegistry",
            "INTEGRITY_RANK",
            "ISOLATION_RANK",
            "Identity",
            "Isolation",
            "SandboxCapabilityDenied",
            "SandboxCapabilityNotSupported",
            "SandboxIdentityDenied",
            "SourceIntegrity",
            "meets_floor",
            "sandbox_tool",
        ],
    )
    def test_the_two_axes_are_importable_from_the_package(self, name: str):
        import maf_sandbox

        assert name in maf_sandbox.__all__
        assert hasattr(maf_sandbox, name)

    def test_the_deployed_isolation_set_is_gone(self):
        """Superseded by the minimum-isolation floor (two-axis sandbox policy, axis 1)."""
        import maf_sandbox

        assert not hasattr(maf_sandbox, "DEPLOYED_ISOLATION")
        assert "DEPLOYED_ISOLATION" not in maf_sandbox.__all__


#: The modules the stdlib-only claim covers, named one by one.  An **allowlist**, not a
#: denylist of exemptions: a module added to this package is outside the claim until someone
#: writes it in here, so widening the scan is a line in a diff rather than something that
#: happens by omission.  `TestModuleInventory` pins the complement, so a new module cannot be
#: quietly neither.
_PROTOCOL_MODULES = frozenset(
    {
        "_error_detail",
        "_host_tools",
        "_outputs",
        "_protocol",
        "_purger",
        "_router",
        "paths",
        "testing",
    }
)

#: The modules deliberately OUTSIDE the stdlib-only claim.  `maf` is the MAF-glue
#: module, the one place `agent_framework` may be imported — see `TestMafIsTheOnlyMafImporter`
#: for the other half of that rule.  `__init__` is here because it is not protocol either: it
#: re-exports and carries the experimental notice.  Both are still covered by
#: `TestOnlyDeclaredDependencies` below and by the `agent_framework` boundary below; what
#: they are exempt from is only the "standard library and nothing else" claim.
_NON_PROTOCOL_MODULES = frozenset({"__init__", "maf"})

#: The one module allowed to import `agent_framework`.
_MAF_GLUE_MODULE = "maf"


def _package_modules():
    """Every module in the installed `maf_sandbox`, as `{stem: path}`."""
    import pathlib

    import maf_sandbox

    root = pathlib.Path(maf_sandbox.__file__).parent  # type: ignore[arg-type]
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


class TestModuleInventory:
    """Every module is either in the stdlib-only claim or explicitly outside it.

    Without this, the allowlist above would decay into a snapshot: a module added later would
    simply never be scanned, and the zero-dependency guarantee would erode silently rather
    than fail. Adding a module has to be a decision — this is where it is recorded.
    """

    def test_every_module_is_classified(self):
        assert set(_package_modules()) == _PROTOCOL_MODULES | _NON_PROTOCOL_MODULES, (
            "a module was added to or removed from maf_sandbox without deciding whether the "
            "stdlib-only claim covers it. Put it in _PROTOCOL_MODULES (and keep it importing "
            "nothing but the standard library) or in _NON_PROTOCOL_MODULES (and say why here)."
        )


class TestZeroDependencies:
    """The protocol modules import nothing but the standard library.

    This layer is protocol and policy: giving THOSE modules a backend dependency, or a MAF
    one, would make them the thing they exist to keep apart (see the package docstring).
    Nothing else pins it; a dependency added to a module without a matching `pyproject.toml`
    entry would still import fine in this workspace, because every other member is already on
    the path.

    The claim is scoped to :data:`_PROTOCOL_MODULES` rather than to the whole distribution:
    the dist now declares `agent-framework-core` for `maf_sandbox.maf`, and a scan that kept
    asserting "nothing here imports anything" would have had to be deleted outright — trading
    a precise, still-true invariant for none at all.
    """

    def test_the_protocol_modules_import_nothing_outside_the_standard_library(self):
        import sys

        stdlib = set(sys.stdlib_module_names)
        offenders: list[str] = []
        for stem, path in sorted(_package_modules().items()):
            if stem not in _PROTOCOL_MODULES:
                continue
            offenders.extend(
                f"{path.name}: import {name}"
                for name in _imported_top_levels(path)
                if name != "__future__" and name not in stdlib
            )
        assert offenders == [], (
            f"maf_sandbox's protocol modules import outside the standard library: "
            f"{offenders}. Their entire reason to exist is zero dependencies — see the "
            "package docstring and pyproject.toml's dependency comment."
        )


class TestMafIsTheOnlyMafImporter:
    """`agent_framework` may be imported by `maf_sandbox.maf` and by nothing else.

    The distribution depends on MAF for that one glue module. The rule keeps that dependency
    from spreading: a protocol module that reached for `agent_framework` would tie the
    vocabulary a backend and a workload share to the framework a *host* happens to use, which
    is the coupling this whole split exists to prevent — and it would do so invisibly, since
    every module here imports fine in a workspace where MAF is installed anyway.
    """

    def test_only_the_glue_module_imports_agent_framework(self):
        offenders = sorted(
            f"{stem} ({path.name})"
            for stem, path in _package_modules().items()
            if stem != _MAF_GLUE_MODULE
            and any(name == "agent_framework" for name in _imported_top_levels(path))
        )
        assert offenders == [], (
            f"these maf_sandbox modules import agent_framework: {offenders}. Only "
            f"{_MAF_GLUE_MODULE!r} may — everything else is the backend-neutral vocabulary a "
            "provider and a workload share, and it must not require the host's framework."
        )

    def test_the_glue_module_really_is_the_importer(self):
        """A boundary nobody is on the far side of would pass by accident forever."""
        glue = _package_modules()[_MAF_GLUE_MODULE]
        assert "agent_framework" in _imported_top_levels(glue), (
            "maf.py no longer imports agent_framework, so the test above proves nothing. "
            "If the glue moved, point _MAF_GLUE_MODULE at its new home."
        )

    def test_importing_the_package_does_not_reach_the_glue(self):
        """`import maf_sandbox` must stay framework-free, which means `__init__` stays clean.

        The glue module's own `agent_framework` import is lazy (inside `sandboxed_tool`, not
        at module top), so importing `.maf` would not itself pull the framework in. The guard
        exists anyway to keep `import maf_sandbox` from pulling in the glue module, full stop
        — a consumer speaking only the protocol should not import code written against a host
        framework it may not use.
        """
        import ast

        source = _package_modules()["__init__"].read_text(encoding="utf-8")
        relative_targets: set[str | None] = set()
        for node in ast.walk(ast.parse(source)):
            if not (isinstance(node, ast.ImportFrom) and node.level > 0):
                continue
            if node.module is not None:
                relative_targets.add(node.module)
            else:
                # `from . import maf` — the target names live in the aliases, not `.module`.
                relative_targets.update(alias.name for alias in node.names)
        assert _MAF_GLUE_MODULE not in relative_targets, (
            f"maf_sandbox/__init__.py imports .{_MAF_GLUE_MODULE}, which makes "
            "`import maf_sandbox` import agent_framework. Consumers reach the glue by name: "
            f"`from maf_sandbox.{_MAF_GLUE_MODULE} import ...`."
        )


# ---------------------------------------------------------------------------
# Dependency discipline — every import must be traceable to a reason
# ---------------------------------------------------------------------------

#: A requirement string's distribution name is not always its import name: `pip install
#: agent-framework-core` puts `agent_framework` on the path, and `azure-identity` and
#: `azure-containerapps-sandbox` both extend the single `azure` namespace package rather
#: than each owning a top-level name of their own. Anything not listed here is assumed to
#: import under its distribution name with hyphens turned to underscores — true of every
#: dependency any of the three maf-sandbox* packages declares today. A dependency where
#: that guess is wrong fails the test below with a readable "imports X" message, which is
#: the right place to notice a new exception belongs here.
_DISTRIBUTION_TO_IMPORT_NAME = {
    "agent-framework-core": "agent_framework",
    "maf-sandbox": "maf_sandbox",
    "azure-identity": "azure",
    "azure-containerapps-sandbox": "azure",
}


def _declared_import_names():
    """The import names `pyproject.toml` licenses `maf_sandbox` to reach for, or `None`.

    `None` means there is no `pyproject.toml` next to the installed package — an
    sdist/wheel-only install with no source tree alongside it — and the caller must skip
    rather than let an empty dependency list pass the scan below vacuously.
    """
    import pathlib
    import re
    import tomllib

    import maf_sandbox

    root = pathlib.Path(maf_sandbox.__file__).parents[2]  # type: ignore[arg-type]
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

    This is the invariant that replaced ``TestNoHostDependency`` (a source scan for the name
    of the private application these packages were extracted from, back when this package
    lived inside it). That name was one instance of a broader risk: a module reaching for
    anything not on *this package's own* dependency list. Nothing else here would notice —
    the workspace running this suite has every sibling package, and everything a host
    application needs, already importable, so a stray import resolves fine in this
    environment regardless of what it names. The first sign of trouble is a downstream
    consumer who installs the published wheel alone, and what they get is an
    ``ImportError`` with no test pointing at the cause.

    Reading ``pyproject.toml`` at test time, rather than hard-coding the allowed names, is
    what keeps this from becoming a second list to update by hand alongside the first: the
    two would drift, and a stale allowlist is a test that passes for the wrong reason.
    """

    def test_sources_exist(self):
        """Guards the scan below against silently finding nothing."""
        assert len(_package_modules()) >= 7

    def test_every_module_only_imports_what_it_is_declared_to_need(self):
        import sys

        declared = _declared_import_names()
        if declared is None:
            pytest.skip(
                "pyproject.toml is not next to the installed maf_sandbox package — this "
                "check only runs against a source checkout, not an installed-only wheel"
            )

        allowed = set(sys.stdlib_module_names) | declared | {"maf_sandbox"}
        offenders = [
            f"{path.name}: import {name}"
            for _, path in sorted(_package_modules().items())
            for name in _imported_top_levels(path)
            if name not in allowed
        ]
        assert offenders == [], (
            f"these maf_sandbox modules import something outside the standard library, the "
            f"package itself, and pyproject.toml's declared dependencies: {offenders}. "
            "Either the import is a mistake, or the dependency belongs in pyproject.toml."
        )
