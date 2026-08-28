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
import gc
import math
import typing

import pytest

from maf_sandbox import (
    DEFAULT_CAPABILITIES,
    DEFAULT_RECLAIM_CONFIG,
    DEFAULT_SANDBOX_LIMITS,
    DEFAULT_TRANSFER_LIMITS,
    ISOLATION_RANK,
    Capability,
    DeclaredOutput,
    DisposalFailure,
    Egress,
    EntryKind,
    FailedReclaimPolicy,
    HostToolAggregate,
    Identity,
    Isolation,
    NoSandboxBackend,
    OsFamily,
    OutputDisposition,
    ReclaimConfig,
    SandboxBackend,
    SandboxBackendNotPermitted,
    SandboxCapabilityDenied,
    SandboxCapabilityNotSupported,
    SandboxEgressNotEnforced,
    SandboxEntry,
    SandboxIdentityDenied,
    SandboxKey,
    SandboxLimits,
    SandboxOsFamilyNotSupported,
    SandboxPurger,
    SandboxRouter,
    SandboxSpec,
    SandboxTransferLimitsNotPermitted,
    SandboxUnclean,
    ScopePurge,
    TransferLimits,
    fold_disposal_failures,
    fold_host_tool_call_transfer_limits,
    meets_floor,
)
from maf_sandbox.testing import InProcessSandbox, InProcessSandboxBackend

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
    """Every fake here declares `none` isolation, so these routers opt below the floor."""

    def test_no_backends_means_not_enabled(self):
        router = SandboxRouter([])
        assert router.enabled is False
        assert router.backend is None

    def test_defaults_to_the_first_registered_backend(self):
        first, second = (
            InProcessSandboxBackend(name="first"),
            InProcessSandboxBackend(name="second"),
        )
        router = SandboxRouter([first, second], min_isolation=Isolation.NONE)
        assert router.backend is first
        assert router.enabled is True

    def test_selects_by_name(self):
        first, second = (
            InProcessSandboxBackend(name="first"),
            InProcessSandboxBackend(name="second"),
        )
        router = SandboxRouter([first, second], min_isolation=Isolation.NONE, selected="second")
        assert router.backend is second

    def test_unknown_name_raises_with_the_registered_ones_named(self):
        with pytest.raises(NoSandboxBackend, match="registered: aca, fake"):
            SandboxRouter(
                [InProcessSandboxBackend(name="fake"), InProcessSandboxBackend(name="aca")],
                min_isolation=Isolation.NONE,
                selected="docker",
            )

    def test_acquire_without_a_backend_raises(self):
        with pytest.raises(NoSandboxBackend):
            asyncio.run(SandboxRouter([]).acquire(_KEY, _SPEC))

    def test_acquire_delegates_to_the_selected_backend(self):
        backend = InProcessSandboxBackend()
        router = SandboxRouter([backend], min_isolation=Isolation.NONE)
        sandbox = asyncio.run(router.acquire(_KEY, _SPEC))
        assert backend.keys == [_KEY]
        assert sandbox is backend.sandbox

    def test_default_reclaim_config_is_applied(self):
        backend = InProcessSandboxBackend()
        router = SandboxRouter([backend], min_isolation=Isolation.NONE)
        assert router.reclaim == DEFAULT_RECLAIM_CONFIG
        assert router.reclaim.timeout == 30.0
        assert router.reclaim.failed_reclaim_policy is FailedReclaimPolicy.DISPOSE
        assert router.reclaim.on_failure is None

    def test_custom_reclaim_config_is_stored(self):
        backend = InProcessSandboxBackend()
        custom = ReclaimConfig(
            timeout=15.0,
            failed_reclaim_policy=FailedReclaimPolicy.KEEP,
            on_failure=lambda failure: asyncio.sleep(0),
        )
        router = SandboxRouter([backend], min_isolation=Isolation.NONE, reclaim=custom)
        assert router.reclaim is custom

    @pytest.mark.parametrize("bad_timeout", [math.inf, math.nan, 0.0, -10.0])
    def test_reclaim_config_timeout_validation(self, bad_timeout):
        backend = InProcessSandboxBackend()
        with pytest.raises(ValueError, match="reclaim.timeout must be a finite positive number"):
            SandboxRouter(
                [backend],
                min_isolation=Isolation.NONE,
                reclaim=ReclaimConfig(timeout=bad_timeout),
            )

    def test_reclaim_config_policy_string_is_normalized_to_enum(self):
        backend = InProcessSandboxBackend()
        config = ReclaimConfig(failed_reclaim_policy="keep")  # type: ignore[arg-type]
        assert config.failed_reclaim_policy is FailedReclaimPolicy.KEEP
        router = SandboxRouter([backend], min_isolation=Isolation.NONE, reclaim=config)
        assert router.reclaim.failed_reclaim_policy is FailedReclaimPolicy.KEEP

    def test_reclaim_config_policy_validation(self):
        backend = InProcessSandboxBackend()
        with pytest.raises(ValueError):
            SandboxRouter(
                [backend],
                min_isolation=Isolation.NONE,
                reclaim=ReclaimConfig(failed_reclaim_policy="unsupported_policy"),  # type: ignore[arg-type]
            )


class TestIsolationLadder:
    """The ladder is data, and its order is load-bearing: `meets_floor` is a rank comparison."""

    def test_every_member_is_ranked(self):
        """An unranked rung would raise `KeyError` inside a policy check, at attach time."""
        assert set(ISOLATION_RANK) == set(Isolation)

    def test_the_ranks_are_dense_and_distinct(self):
        assert sorted(ISOLATION_RANK.values()) == list(range(len(Isolation)))

    def test_the_order_runs_from_no_boundary_to_the_strongest_one(self):
        assert list(ISOLATION_RANK) == [
            Isolation.NONE,
            Isolation.RUNTIME,
            Isolation.PROCESS,
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

    def test_the_process_rung_sits_between_a_software_boundary_and_a_container(self):
        """A kernel-enforced address space is more than software fault isolation and less than namespaces."""
        assert meets_floor(Isolation.PROCESS, Isolation.RUNTIME)
        assert not meets_floor(Isolation.PROCESS, Isolation.CONTAINER)
        assert not meets_floor(Isolation.RUNTIME, Isolation.PROCESS)

    def test_the_old_bottom_rung_took_its_name_back_but_not_its_string(self):
        """`"process"` has to keep failing now that the name it used to carry is a real rung.

        `PROCESS` was the bottom rung and meant no boundary at all; it now means a genuine
        separate-OS-process one, two ranks above. Reusing the attribute is safe because it is
        resolved where the code is written. Reusing the value would not be: a declaration
        crosses into this vocabulary through `Isolation(raw)` at run time, out of
        configuration nobody re-reads, so a backend that declared `"process"` *because* it
        drew no boundary would come back ranked above `RUNTIME`, having claimed one it never
        had. This `ValueError` is the whole of what keeps that a refusal rather than a
        promotion, and it has to outlive the deprecation window rather than expire with it.
        """
        assert Isolation.PROCESS.value == "os_process"
        assert ISOLATION_RANK[Isolation.PROCESS] > ISOLATION_RANK[Isolation.RUNTIME]
        with pytest.raises(ValueError, match="process"):
            Isolation("process")

    @pytest.mark.parametrize(
        ("declared", "permitted"),
        [
            (Isolation.PROCESS, False),
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
            Isolation.NONE,
            Isolation.RUNTIME,
            Isolation.PROCESS,
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
                min_isolation=Isolation.NONE,
            )

    def test_a_backend_still_declaring_the_old_process_string_is_refused(self):
        """What a backend written against the old vocabulary meets: a refusal, not a promotion.

        Down the same path as any unknown value, which is the point — `"process"` is not
        special-cased, it is simply not a value any rung carries. So such a backend is turned
        away rather than ranked above `RUNTIME` on the strength of a word it meant the
        opposite way. The `PROCESS` *attribute* means that stronger rung now; this string
        never comes to mean anything again.
        """
        with pytest.raises(SandboxBackendNotPermitted, match="not a rung"):
            SandboxRouter(
                [InProcessSandboxBackend(name="legacy", isolation="process")],
                min_isolation=Isolation.NONE,
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
        router.ensure_can_serve(SandboxSpec(kind="codeact", min_isolation=Isolation.NONE))

    def test_a_spec_with_no_opinion_leaves_the_floor_to_the_host(self):
        self._router(Isolation.NONE, Isolation.NONE).ensure_can_serve(_SPEC)


class _BackendWithoutCapabilities:
    """A backend written before the capability axis existed: it declares what it had.

    Written out rather than subclassed, because the fake now always has the property. It has no
    ``limits`` either, which makes it the only fixture here that exercises *both* of the
    router's `getattr` fallbacks — see `TestTransferLimitMatch`.
    """

    name = "legacy"
    isolation = Isolation.MICROVM
    egress_modes = frozenset({Egress.ALLOWLIST, Egress.CLOSED})

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
        return SandboxRouter([InProcessSandboxBackend(**kwargs)], min_isolation=Isolation.NONE)

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


def _a_surface(identities: frozenset[Identity] = frozenset()) -> HostToolAggregate:
    """A sealed surface carrying `identities` and nothing else of interest."""
    return HostToolAggregate(
        result_integrity=None,
        outbound_caps=frozenset(),
        identities=identities,
        requires_approval=False,
        has_undeclared=False,
        # Deliberately tiny: attaching a surface folds its transport into the transfer-limit
        # match, and a default-sized one would refuse these specs for a reason none of them
        # is about.
        response_limits=TransferLimits(1024, 1024, 1),
        max_host_tool_calls_per_run=1,
    )


class TestRouterDenials:
    """The hard stop: capabilities and identities a host refuses whatever the backend can do.

    A posture statement about the *spec*, not a property of the backend — which is why the
    denial fires even when the backend genuinely implements the capability, and why its
    exception is a `PermissionError` where the capability *match* is a `RuntimeError`.
    """

    def _router(self, **kwargs) -> SandboxRouter:
        # Room for the fold: a spec can only carry identities by carrying a surface, and the
        # router adds that surface's transport to the transfer-limit match. Denials are what
        # these tests are about, so the ceiling must not refuse first.
        roomy = TransferLimits(1 << 26, 1 << 31, 4096)
        backend = InProcessSandboxBackend(
            capabilities=DEFAULT_CAPABILITIES | {Capability.HOST_TOOLS},
            limits=SandboxLimits(files_in=roomy, files_out=roomy),
        )
        return SandboxRouter([backend], min_isolation=Isolation.NONE, **kwargs)

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
        spec = SandboxSpec(
            kind="codeact",
            requires=DEFAULT_CAPABILITIES | {Capability.HOST_TOOLS},
            host_tools=_a_surface(frozenset({Identity.USER})),
        )
        with pytest.raises(SandboxIdentityDenied, match="user"):
            router.ensure_can_serve(spec)

    def test_an_undenied_identity_is_served(self):
        router = self._router(denied_identities={Identity.USER})
        router.ensure_can_serve(
            SandboxSpec(
                kind="codeact",
                requires=DEFAULT_CAPABILITIES | {Capability.HOST_TOOLS},
                host_tools=_a_surface(frozenset({Identity.APP})),
            )
        )

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
        assert set(EntryKind) == {
            EntryKind.FILE,
            EntryKind.DIRECTORY,
            EntryKind.SYMLINK,
            EntryKind.OTHER,
        }

    def test_a_link_and_an_unclassifiable_entry_are_separate_members(self):
        """The distinction #142 needed and #214 added: an escape is not an ENOTDIR.

        Both are refused — only `FILE` is ever read — so what the split buys is the *reason*,
        which is the part a caller above the backend cannot reconstruct from `OTHER` alone.
        """
        assert EntryKind.SYMLINK is not EntryKind.OTHER

    def test_every_disposition_is_accounted_for(self):
        assert set(OutputDisposition) == {OutputDisposition.LAND, OutputDisposition.CONSUME}

    @pytest.mark.parametrize(
        ("member", "value"),
        [
            (EntryKind.FILE, "file"),
            (EntryKind.DIRECTORY, "directory"),
            (EntryKind.SYMLINK, "symlink"),
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
            "DEFAULT_RECLAIM_CONFIG",
            "DEFAULT_SANDBOX_LIMITS",
            "DEFAULT_TRANSFER_LIMITS",
            "DeclaredOutput",
            "EntryKind",
            "FailedReclaimPolicy",
            "OutputDisposition",
            "ReclaimConfig",
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
        return SandboxRouter([backend], min_isolation=Isolation.NONE)

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


class TestTheRouterFoldsADispatchSurface:
    """A spec carrying a host-tool surface has the registry's ceilings folded into the
    transfer-limit match, transiently: a backend that serves the bare caps but not the transport
    is refused at attach, and the spec's stored caps are left for the kind's own runtime tally.
    """

    _RL = TransferLimits(max_bytes_per_file=8_000_000, max_total_bytes=32_000_000, max_files=64)
    _SMALL = TransferLimits(max_bytes_per_file=64 * 1024, max_total_bytes=256 * 1024, max_files=4)

    def _surface(self, response_limits: TransferLimits, dispatches: int = 1) -> HostToolAggregate:
        # The router folds the surface's limits and its dispatch bound; the other legs do not
        # enter the match, so they take their least-opinionated values here.
        return HostToolAggregate(
            result_integrity=None,
            outbound_caps=frozenset(),
            identities=frozenset(),
            requires_approval=False,
            has_undeclared=False,
            response_limits=response_limits,
            max_host_tool_calls_per_run=dispatches,
        )

    def _router(self, ceiling: SandboxLimits) -> SandboxRouter:
        # Serving the capability the dispatching specs require, so what these tests exercise is
        # the transfer match rather than the capability one.
        return SandboxRouter(
            [
                InProcessSandboxBackend(
                    limits=ceiling,
                    capabilities=DEFAULT_CAPABILITIES | {Capability.HOST_TOOLS},
                )
            ],
            min_isolation=Isolation.NONE,
        )

    def _dispatching(self, surface: HostToolAggregate, **kw: object) -> SandboxSpec:
        """A spec whose declarations admit the surface it carries, which `SandboxSpec` requires:
        the router answers posture from `requires` and the surface's own identities, so a spec
        that carries one without asking for the capability would slip past a deny list."""
        return SandboxSpec(
            kind="codeact",
            requires=DEFAULT_CAPABILITIES | {Capability.HOST_TOOLS},
            host_tools=surface,
            **{"files_in": self._SMALL, "files_out": self._SMALL, **kw},  # type: ignore[arg-type]
        )

    def test_a_backend_the_bare_spec_passes_is_refused_once_a_surface_is_attached(self):
        # Wide enough for the workload's own 64 KiB files_out, but under the 32 MB the folded
        # output read needs: the bare spec attaches, the dispatching one does not.
        ceiling = SandboxLimits(
            files_in=TransferLimits(64 * 1024 * 1024, 64 * 1024 * 1024, 64),
            files_out=TransferLimits(1024 * 1024, 64 * 1024 * 1024, 64),
        )
        bare = SandboxSpec(kind="codeact", files_in=self._SMALL, files_out=self._SMALL)
        self._router(ceiling).ensure_can_serve(bare)
        dispatching = self._dispatching(self._surface(self._RL))
        with pytest.raises(SandboxTransferLimitsNotPermitted, match="files_out") as excinfo:
            self._router(ceiling).ensure_can_serve(dispatching)
        # The refusal names the fold, so the requirement the caller never typed is not read as
        # their own mistake.
        assert "folded to include" in str(excinfo.value)

    def test_a_backend_wide_enough_for_the_fold_serves_the_surface(self):
        ceiling = SandboxLimits(
            files_in=TransferLimits(64 * 1024 * 1024, 64 * 1024 * 1024, 64),
            files_out=TransferLimits(64 * 1024 * 1024, 64 * 1024 * 1024, 64),
        )
        spec = self._dispatching(self._surface(self._RL))
        self._router(ceiling).ensure_can_serve(spec)

    def test_the_fold_is_transient_and_leaves_the_specs_own_caps(self):
        # A backend too small even for the bare caps, so the match fails; the point is that after
        # the match the stored caps are unchanged — the fold existed only inside the comparison.
        ceiling = SandboxLimits(files_in=self._SMALL, files_out=self._SMALL)
        spec = self._dispatching(self._surface(self._RL))
        with pytest.raises(SandboxTransferLimitsNotPermitted):
            self._router(ceiling).ensure_can_serve(spec)
        assert spec.files_in == self._SMALL
        assert spec.files_out == self._SMALL

    def test_a_workload_already_over_the_ceiling_is_not_blamed_on_the_fold(self):
        """The note must mark the fold as the *cause*, not merely as something that also grew a
        leg. A workload whose own per-file cap already exceeds the backend would have been
        refused with no surface at all, so pointing at the transport sends the caller to their
        registry to fix a number they typed into their spec."""
        ceiling = SandboxLimits(
            files_in=TransferLimits(64 * 1024 * 1024, 64 * 1024 * 1024, 64 * 1024),
            files_out=TransferLimits(1024, 64 * 1024 * 1024, 64 * 1024),
        )
        over = TransferLimits(max_bytes_per_file=8192, max_total_bytes=8192, max_files=2)
        spec = self._dispatching(self._surface(self._RL), files_in=over, files_out=over)
        with pytest.raises(SandboxTransferLimitsNotPermitted, match="files_out") as excinfo:
            self._router(ceiling).ensure_can_serve(spec)
        assert "folded to include" not in str(excinfo.value)

    def test_a_refusal_with_no_surface_does_not_mention_a_fold(self):
        """The note is decided per direction, by whether *that* direction's number actually grew.
        A workload refused on caps it typed itself must not be told a transport widened them."""
        ceiling = SandboxLimits(files_in=self._SMALL, files_out=self._SMALL)
        big = TransferLimits(max_bytes_per_file=99_000_000, max_total_bytes=99_000_000, max_files=9)
        bare = SandboxSpec(kind="codeact", files_in=big, files_out=big)
        with pytest.raises(SandboxTransferLimitsNotPermitted) as excinfo:
            self._router(ceiling).ensure_can_serve(bare)
        assert "folded to include" not in str(excinfo.value)

    def test_the_fold_grows_with_the_surfaces_dispatch_bound(self):
        """The bound rides in the surface because the transport's file counts and its refusal
        budget both scale with it — a backend sized for a few calls cannot serve many."""
        ceiling = SandboxLimits(
            files_in=TransferLimits(64 * 1024 * 1024, 64 * 1024 * 1024, 12),
            files_out=TransferLimits(64 * 1024 * 1024, 64 * 1024 * 1024, 12),
        )
        modest = self._dispatching(self._surface(self._RL, dispatches=1))
        self._router(ceiling).ensure_can_serve(modest)
        chatty = self._dispatching(self._surface(self._RL, dispatches=50))
        with pytest.raises(SandboxTransferLimitsNotPermitted):
            self._router(ceiling).ensure_can_serve(chatty)


class TestASpecMustAdmitTheSurfaceItCarries:
    """`identities` comes off the surface, so only the capability half can still disagree: a
    spec carrying a callable surface without asking for `HOST_TOOLS` would be served by a host
    that denies exactly it. Refused where the spec is built."""

    def _surface(self, identities: frozenset[Identity] = frozenset()) -> HostToolAggregate:
        return _a_surface(identities)

    def test_a_surface_without_the_capability_is_refused(self):
        with pytest.raises(ValueError, match="denied_capabilities"):
            SandboxSpec(kind="codeact", host_tools=self._surface())

    def test_a_surfaces_identities_are_the_specs_own(self):
        """What the removed refusal used to catch, now unrepresentable rather than refused."""
        surface = self._surface(frozenset({Identity.USER}))
        spec = SandboxSpec(
            kind="codeact",
            requires=DEFAULT_CAPABILITIES | {Capability.HOST_TOOLS},
            host_tools=surface,
        )
        assert spec.identities == frozenset({Identity.USER})
        assert SandboxSpec(kind="codeact").identities == frozenset()

    def test_a_spec_that_asks_for_the_capability_is_accepted(self):
        surface = self._surface(frozenset({Identity.USER}))
        spec = SandboxSpec(
            kind="codeact",
            requires=DEFAULT_CAPABILITIES | {Capability.HOST_TOOLS},
            host_tools=surface,
        )
        assert spec.host_tools is surface

    def test_the_hosts_identity_denial_then_actually_reaches_it(self):
        """The point of the invariant: with it, a wired USER surface meets `denied_identities`."""
        surface = self._surface(frozenset({Identity.USER}))
        wide = TransferLimits(1 << 30, 1 << 34, 1 << 16)
        router = SandboxRouter(
            [
                InProcessSandboxBackend(
                    limits=SandboxLimits(files_in=wide, files_out=wide),
                    capabilities=DEFAULT_CAPABILITIES | {Capability.HOST_TOOLS},
                )
            ],
            min_isolation=Isolation.NONE,
            denied_identities={Identity.USER},
        )
        spec = SandboxSpec(
            kind="codeact",
            requires=DEFAULT_CAPABILITIES | {Capability.HOST_TOOLS},
            host_tools=surface,
        )
        with pytest.raises(SandboxIdentityDenied):
            router.ensure_can_serve(spec)

    def test_every_public_surface_annotated_with_the_aggregate_resolves(self):
        """Each of these is public and annotated against the aggregate, so its hints must resolve
        at runtime: a `TYPE_CHECKING`-only import satisfies the type checker while leaving
        `typing.get_type_hints` raising for every caller that reads annotations."""
        for subject in (SandboxSpec, fold_host_tool_call_transfer_limits, HostToolAggregate):
            typing.get_type_hints(subject)


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
            [_BackendDeclaringTheWrongLimits(declared)], min_isolation=Isolation.NONE
        )
        with pytest.raises(SandboxTransferLimitsNotPermitted, match="SandboxLimits"):
            router.ensure_can_serve(_SPEC)

    def test_acquire_refuses_it_too(self):
        router = SandboxRouter(
            [_BackendDeclaringTheWrongLimits(None)], min_isolation=Isolation.NONE
        )
        with pytest.raises(SandboxTransferLimitsNotPermitted):
            asyncio.run(router.acquire(_KEY, _SPEC))

    def test_the_default_ceilings_are_still_what_silence_means(self):
        """The guard refuses a declaration it cannot read — it does not make one mandatory."""
        SandboxRouter(
            [_BackendWithoutCapabilities()], min_isolation=Isolation.NONE
        ).ensure_can_serve(_SPEC)


class _BackendWithoutEgress:
    """A third-party backend one property short: no `egress_modes` at all.

    Read as the empty set — enforces nothing — so every ask is refused. Written out rather than
    subclassed, because the fake now always has the property.
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
    """Egress is resolved, not matched: a workload runs one mode, served iff the backend enforces
    it, refused otherwise — never a substitute. Refuse, never degrade.
    """

    _CLOSED_SPEC = SandboxSpec(kind="bicep")  # egress defaults to CLOSED
    _ALLOWLIST_SPEC = SandboxSpec(
        kind="bicep", egress=Egress.ALLOWLIST, egress_allow=("example.invalid",)
    )
    _UNRESTRICTED_SPEC = SandboxSpec(kind="bicep", egress=Egress.UNRESTRICTED)

    def _router(self, *modes: Egress) -> SandboxRouter:
        return SandboxRouter(
            [InProcessSandboxBackend(egress_modes=frozenset(modes))], min_isolation=Isolation.NONE
        )

    def test_a_backend_serves_a_mode_it_enforces(self):
        self._router(Egress.CLOSED, Egress.ALLOWLIST).ensure_can_serve(self._CLOSED_SPEC)
        self._router(Egress.CLOSED, Egress.ALLOWLIST).ensure_can_serve(self._ALLOWLIST_SPEC)

    def test_a_mode_matches_when_declared_as_a_plain_string(self):
        """Backends outside this repository declare strings; `StrEnum` keeps them matching."""
        assert Egress.CLOSED == "closed"
        SandboxRouter(
            [InProcessSandboxBackend(egress_modes=frozenset({"closed"}))],  # type: ignore[arg-type]
            min_isolation=Isolation.NONE,
        ).ensure_can_serve(self._CLOSED_SPEC)

    def test_a_closed_only_backend_refuses_an_allowlist_workload_never_degrades(self, caplog):
        """No degrade to CLOSED, and no warning — the ALLOWLIST run is refused outright."""
        with caplog.at_level("WARNING"):
            with pytest.raises(SandboxEgressNotEnforced, match="cannot enforce the 'allowlist'"):
                self._router(Egress.CLOSED).ensure_can_serve(self._ALLOWLIST_SPEC)
        assert caplog.records == []

    def test_an_unrestricted_only_backend_refuses_a_closed_workload_not_best_effort(self):
        """The correction that shapes the rule: CLOSED is not free. A backend that cannot cut the
        network is refused a CLOSED workload rather than approximating it."""
        with pytest.raises(SandboxEgressNotEnforced, match="cannot enforce the 'closed'"):
            self._router(Egress.UNRESTRICTED).ensure_can_serve(self._CLOSED_SPEC)

    def test_an_unrestricted_only_backend_serves_an_unrestricted_workload(self):
        """The honest dev opt-in: run open on a backend that enforces exactly open."""
        self._router(Egress.UNRESTRICTED).ensure_can_serve(self._UNRESTRICTED_SPEC)

    def test_a_backend_enforcing_nothing_refuses_even_a_closed_workload(self):
        """An empty enforceable set serves nothing, and the message names what it enforces."""
        with pytest.raises(SandboxEgressNotEnforced, match="it enforces nothing") as raised:
            self._router().ensure_can_serve(self._CLOSED_SPEC)
        assert "cannot enforce the 'closed'" in str(raised.value)

    def test_a_backend_without_the_property_enforces_nothing(self):
        """A backend one property short is the empty set: refused, and told what it enforces."""
        router = SandboxRouter([_BackendWithoutEgress()])
        with pytest.raises(SandboxEgressNotEnforced, match="it enforces nothing"):
            router.ensure_can_serve(self._ALLOWLIST_SPEC)

    def test_no_backend_configured_is_not_an_egress_failure(self):
        """Nothing runs, so nothing reaches anything — and no tool is attached either."""
        SandboxRouter([]).ensure_can_serve(self._ALLOWLIST_SPEC)


class TestAcquireEnforcesPolicy:
    """`acquire` refuses on the same grounds as `ensure_can_serve`.

    Before this, `acquire` delegated straight to the backend: a caller who never called
    `ensure_can_serve` first got no floor, capability or egress check at all.
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
        router = SandboxRouter([InProcessSandboxBackend()], min_isolation=Isolation.NONE)
        spec = SandboxSpec(
            kind="codeact", requires=frozenset({Capability.EXEC, Capability.RUN_CODE})
        )
        with pytest.raises(SandboxCapabilityNotSupported, match="run_code"):
            asyncio.run(router.acquire(_KEY, spec))

    def test_acquire_refuses_a_backend_that_cannot_enforce_the_mode(self):
        # _SPEC runs CLOSED (the default); an unrestricted-only backend cannot enforce it.
        router = SandboxRouter(
            [InProcessSandboxBackend(egress_modes=frozenset({Egress.UNRESTRICTED}))],
            min_isolation=Isolation.NONE,
        )
        with pytest.raises(SandboxEgressNotEnforced, match="cannot enforce the 'closed'"):
            asyncio.run(router.acquire(_KEY, _SPEC))

    def test_acquire_refuses_an_allowlist_run_a_closed_backend_cannot_serve(self, caplog):
        # Refuse, never degrade — and no warning on either path, since nothing is served.
        router = SandboxRouter(
            [InProcessSandboxBackend(egress_modes=frozenset({Egress.CLOSED}))],
            min_isolation=Isolation.NONE,
        )
        spec = SandboxSpec(kind="bicep", egress=Egress.ALLOWLIST, egress_allow=("example.invalid",))
        with caplog.at_level("WARNING"):
            with pytest.raises(SandboxEgressNotEnforced):
                router.ensure_can_serve(spec)
            with pytest.raises(SandboxEgressNotEnforced):
                asyncio.run(router.acquire(_KEY, spec))
        assert caplog.records == []


class TestPurge:
    def test_dispose_scope_asks_every_backend_not_only_the_selected_one(self):
        """A conversation may have been served while a different backend was configured."""
        first, second = (
            InProcessSandboxBackend(name="first"),
            InProcessSandboxBackend(name="second"),
        )
        router = SandboxRouter([first, second], min_isolation=Isolation.NONE)
        total = asyncio.run(router.dispose_scope("scope-a", "thread-1")).disposed
        assert total == 2
        assert first.purged == second.purged == [("scope-a", "thread-1")]

    def test_a_failing_backend_does_not_stop_the_others(self):
        good = InProcessSandboxBackend(name="good")
        router = SandboxRouter([_ExplodingBackend(name="bad"), good], min_isolation=Isolation.NONE)
        total = asyncio.run(router.dispose_scope("scope-a", "thread-1")).disposed
        assert total == 1
        assert good.purged == [("scope-a", "thread-1")]

    def test_dispose_never_raises(self):
        router = SandboxRouter([_ExplodingBackend()], min_isolation=Isolation.NONE)
        asyncio.run(router.dispose(_KEY))

    def test_purger_is_duck_typed_on_purge_scoped_thread(self):
        """The host awaits this without importing the class, so the name is the contract."""
        backend = InProcessSandboxBackend()
        purger = SandboxPurger(SandboxRouter([backend], min_isolation=Isolation.NONE))
        assert asyncio.run(purger.purge_scoped_thread("scope-a", "thread-1")).disposed == 1
        assert backend.purged == [("scope-a", "thread-1")]


class TestAKeyTheRouterCouldNotDisposeIsRefused:
    """Better a failed run than leaked data: a key whose disposal did not land is not served."""

    def _router(self, *backends):
        return SandboxRouter(list(backends), min_isolation=Isolation.NONE)

    def test_a_landed_disposal_answers_true_and_serves_the_key_again(self):
        backend = InProcessSandboxBackend()
        router = self._router(backend)
        assert asyncio.run(router.dispose_unclean(_KEY, timeout=1.0)) is True
        assert backend.disposed == [_KEY]
        asyncio.run(router.acquire(_KEY, _SPEC))

    def test_a_refused_disposal_answers_false_and_the_key_is_refused(self):
        router = self._router(InProcessSandboxBackend(dispose_error=RuntimeError("down")))
        assert asyncio.run(router.dispose_unclean(_KEY, timeout=1.0)) is False
        with pytest.raises(SandboxUnclean, match="refused until a disposal lands"):
            asyncio.run(router.acquire(_KEY, _SPEC))

    def test_another_key_in_the_same_conversation_is_still_served(self):
        backend = InProcessSandboxBackend(dispose_error=RuntimeError("down"))
        router = self._router(backend)
        asyncio.run(router.dispose_unclean(_KEY, timeout=1.0))
        other = SandboxKey(scope="scope-a", thread_id="thread-1", agent_dir="another-agent")
        asyncio.run(router.acquire(other, _SPEC))

    def test_a_disposal_that_hangs_is_bounded_and_counts_as_not_landed(self):
        class _Hangs(InProcessSandboxBackend):
            async def dispose(self, key):
                await asyncio.Event().wait()

        router = self._router(_Hangs())
        assert asyncio.run(router.dispose_unclean(_KEY, timeout=0.05)) is False
        with pytest.raises(SandboxUnclean):
            asyncio.run(router.acquire(_KEY, _SPEC))

    def test_a_non_finite_or_non_positive_timeout_is_refused_before_the_key_is_marked(self):
        """`asyncio.timeout(math.inf)` never expires, so an infinite bound would let a hanging
        backend hang the caller past the bound this method documents. Rejected like the other
        timeout-taking helpers — and before the key is marked, so a rejected call leaves the
        ledger untouched and the key still servable."""
        backend = InProcessSandboxBackend()
        router = self._router(backend)
        for bad in (math.inf, math.nan, 0.0, -1.0):
            with pytest.raises(ValueError, match="finite positive"):
                asyncio.run(router.dispose_unclean(_KEY, timeout=bad))
        # Nothing was marked or disposed by the rejected calls: the key is still served.
        asyncio.run(router.acquire(_KEY, _SPEC))
        assert backend.disposed == []

    def test_the_key_is_refused_while_a_disposal_is_still_running(self):
        """Refused from the moment the disposal starts, not only once it fails: calls sharing a
        key are not serialized, so a concurrent acquire during a hanging disposal must not be
        handed the dirty sandbox."""
        started = asyncio.Event()

        class _HangsAfterStarting(InProcessSandboxBackend):
            async def dispose(self, key: SandboxKey) -> None:
                started.set()
                await asyncio.Event().wait()

        async def drive() -> None:
            router = self._router(_HangsAfterStarting())
            disposing = asyncio.create_task(router.dispose_unclean(_KEY, timeout=10.0))
            await started.wait()  # the disposal is in flight and has not yet landed
            try:
                with pytest.raises(SandboxUnclean):
                    await router.acquire(_KEY, _SPEC)
            finally:
                disposing.cancel()
                try:
                    await disposing
                except asyncio.CancelledError:
                    # `disposing` is the task we just cancelled; its CancelledError is expected.
                    pass

        asyncio.run(drive())

    def test_a_later_plain_dispose_that_lands_reopens_it(self):
        backend = InProcessSandboxBackend(dispose_error=RuntimeError("down"))
        router = self._router(backend)
        asyncio.run(router.dispose_unclean(_KEY, timeout=1.0))
        backend.dispose_error = None
        asyncio.run(router.dispose(_KEY))
        asyncio.run(router.acquire(_KEY, _SPEC))

    def test_a_scope_purge_that_lands_reopens_its_keys_and_no_others(self):
        backend = InProcessSandboxBackend(dispose_error=RuntimeError("down"))
        router = self._router(backend)
        elsewhere = SandboxKey(scope="scope-a", thread_id="thread-2", agent_dir="devops-engineer")
        asyncio.run(router.dispose_unclean(_KEY, timeout=1.0))
        asyncio.run(router.dispose_unclean(elsewhere, timeout=1.0))
        asyncio.run(router.dispose_scope("scope-a", "thread-1"))
        asyncio.run(router.acquire(_KEY, _SPEC))
        with pytest.raises(SandboxUnclean):
            asyncio.run(router.acquire(elsewhere, _SPEC))

    def test_a_scope_purge_a_backend_refused_reopens_nothing(self):
        backend = InProcessSandboxBackend(dispose_error=RuntimeError("down"))
        router = self._router(backend, _ExplodingBackend(name="bad"))
        asyncio.run(router.dispose_unclean(_KEY, timeout=1.0))
        asyncio.run(router.dispose_scope("scope-a", "thread-1"))
        with pytest.raises(SandboxUnclean):
            asyncio.run(router.acquire(_KEY, _SPEC))

    def test_every_backend_is_asked_and_one_refusal_is_enough(self):
        good = InProcessSandboxBackend(name="good")
        router = self._router(good, InProcessSandboxBackend(name="bad", dispose_error=OSError()))
        assert asyncio.run(router.dispose_unclean(_KEY, timeout=1.0)) is False
        assert good.disposed == [_KEY]

    def test_the_refusal_is_unknown_to_a_second_router(self):
        """In-process knowledge only, the same bound `dispose_scope` exists to reach past."""
        backend = InProcessSandboxBackend(dispose_error=RuntimeError("down"))
        asyncio.run(self._router(backend).dispose_unclean(_KEY, timeout=1.0))
        asyncio.run(self._router(backend).acquire(_KEY, _SPEC))


class TestABackendReportsAFailedDeleteWithoutRaising:
    """A returned reason refuses the key; only silence lets it be served again."""

    def _router(self, *backends):
        return SandboxRouter(list(backends), min_isolation=Isolation.NONE)

    def test_a_reported_reason_refuses_the_key(self):
        router = self._router(
            InProcessSandboxBackend(
                dispose_failure=DisposalFailure("refused", "container 7 still running")
            )
        )
        assert asyncio.run(router.dispose_unclean(_KEY, timeout=1.0)) is False
        with pytest.raises(SandboxUnclean, match="refused until a disposal lands"):
            asyncio.run(router.acquire(_KEY, _SPEC))

    def test_saying_nothing_still_lands(self):
        """The compatibility half: a backend with no way to check keeps today's behaviour."""
        backend = InProcessSandboxBackend()
        router = self._router(backend)
        assert asyncio.run(router.dispose_unclean(_KEY, timeout=1.0)) is True
        assert backend.disposed == [_KEY]
        asyncio.run(router.acquire(_KEY, _SPEC))

    def test_the_reason_and_the_backend_that_gave_it_are_logged(self, caplog):
        router = self._router(
            InProcessSandboxBackend(
                name="acas", dispose_failure=DisposalFailure("refused", "403 denied")
            )
        )
        with caplog.at_level("WARNING"):
            asyncio.run(router.dispose_unclean(_KEY, timeout=1.0))
        assert "acas" in caplog.text
        assert "403 denied" in caplog.text

    def test_one_backend_reporting_is_enough_and_the_rest_are_still_asked(self):
        good = InProcessSandboxBackend(name="good")
        router = self._router(
            InProcessSandboxBackend(name="bad", dispose_failure=DisposalFailure("refused", "no")),
            good,
        )
        assert asyncio.run(router.dispose_unclean(_KEY, timeout=1.0)) is False
        assert good.disposed == [_KEY]

    def test_a_plain_dispose_that_reports_does_not_reopen_the_key(self):
        """`dispose` reaches the same ledger, so a reported failure must not clear a refusal."""
        backend = InProcessSandboxBackend(dispose_error=RuntimeError("down"))
        router = self._router(backend)
        asyncio.run(router.dispose_unclean(_KEY, timeout=1.0))
        backend.dispose_error = None
        backend.dispose_failure = DisposalFailure(
            "refused", "delete accepted but the sandbox is still listed"
        )
        asyncio.run(router.dispose(_KEY))
        with pytest.raises(SandboxUnclean):
            asyncio.run(router.acquire(_KEY, _SPEC))

    def test_a_later_disposal_that_says_nothing_reopens_it(self):
        backend = InProcessSandboxBackend(dispose_failure=DisposalFailure("refused", "still there"))
        router = self._router(backend)
        asyncio.run(router.dispose_unclean(_KEY, timeout=1.0))
        backend.dispose_failure = None
        asyncio.run(router.dispose(_KEY))
        asyncio.run(router.acquire(_KEY, _SPEC))


class TestAScopePurgeReportsWhatItCouldNotDelete:
    """A conversation delete that deleted nothing must not reopen the keys it refused (#641).

    `dispose_scope` clears the ledger for the whole conversation, so reading only the raise
    here loses more than `dispose` does: every agent's key under that thread, not one.
    """

    def _router(self, *backends):
        return SandboxRouter(list(backends), min_isolation=Isolation.NONE)

    def test_a_reported_purge_failure_leaves_the_conversation_refused(self):
        backend = InProcessSandboxBackend(
            dispose_failure=DisposalFailure("refused", "down"),
            purge_failure=DisposalFailure("unlisted", "the label query failed"),
        )
        router = self._router(backend)
        asyncio.run(router.dispose_unclean(_KEY, timeout=1.0))
        purge = asyncio.run(router.dispose_scope("scope-a", "thread-1"))
        assert purge.undisposed is not None
        assert purge.undisposed.code == "unlisted"
        assert "the label query failed" in purge.undisposed.detail
        with pytest.raises(SandboxUnclean):
            asyncio.run(router.acquire(_KEY, _SPEC))

    def test_a_purge_that_says_nothing_still_reopens_them(self):
        backend = InProcessSandboxBackend(dispose_failure=DisposalFailure("refused", "down"))
        router = self._router(backend)
        asyncio.run(router.dispose_unclean(_KEY, timeout=1.0))
        assert asyncio.run(router.dispose_scope("scope-a", "thread-1")).undisposed is None
        asyncio.run(router.acquire(_KEY, _SPEC))

    def test_the_backend_that_reported_is_named(self):
        router = self._router(
            InProcessSandboxBackend(
                name="acas", purge_failure=DisposalFailure("refused", "403 denied")
            ),
            InProcessSandboxBackend(name="docker"),
        )
        purge = asyncio.run(router.dispose_scope("scope-a", "thread-1"))
        assert purge.undisposed == DisposalFailure("refused", "acas: 403 denied")
        assert purge.disposed == 2, "a backend that reported still deleted what it could"

    def test_the_scope_context_manager_carries_it_out_of_the_block(self):
        backend = InProcessSandboxBackend(
            purge_failure=DisposalFailure("unreachable", "the group was unreachable")
        )

        async def scenario():
            router = self._router(backend)
            async with router.scope("scope-a", "thread-1") as disposal:
                assert disposal.undisposed is None, "it means nothing until the block ends"
            return disposal

        assert asyncio.run(scenario()).undisposed == DisposalFailure(
            "unreachable", "in-process: the group was unreachable"
        )

    def test_the_purger_hands_a_host_the_same_answer(self):
        purger = SandboxPurger(
            self._router(
                InProcessSandboxBackend(
                    purge_failure=DisposalFailure("unlisted", "the label query failed")
                )
            )
        )
        purge = asyncio.run(purger.purge_scoped_thread("scope-a", "thread-1"))
        assert purge.undisposed == DisposalFailure("unlisted", "in-process: the label query failed")


class TestTheDisposalCodeIsWhatACallerBranchesOn:
    """The code is the contract; the detail is for a log. Folding several keeps both."""

    def _router(self, *backends):
        return SandboxRouter(list(backends), min_isolation=Isolation.NONE)

    def test_the_most_actionable_code_wins_a_fold(self):
        """A caller that retries on `unreachable` should not be talked out of it by a second
        sandbox whose delete was merely refused."""
        folded = fold_disposal_failures(
            [DisposalFailure("refused", "a"), DisposalFailure("unreachable", "b")]
        )
        assert folded is not None
        assert folded.code == "unreachable"

    def test_a_fold_keeps_every_detail_in_one_shape(self):
        """One shape whether one backend reported or three: a log template built against the
        single-failure form must not silently misread the folded one."""
        one_only = fold_disposal_failures([DisposalFailure("refused", "a")])
        several = fold_disposal_failures(
            [DisposalFailure("refused", "a"), DisposalFailure("unlisted", "b")]
        )
        assert one_only is not None and several is not None
        assert one_only.detail == "a"
        assert several.detail == "a; b"

    def test_one_failure_folds_to_itself_unchanged(self):
        only = DisposalFailure("refused", "a")
        assert fold_disposal_failures([only]) is only

    def test_nothing_folds_to_nothing(self):
        assert fold_disposal_failures([]) is None

    def test_a_lone_code_outside_the_vocabulary_is_normalised_too(self):
        """Otherwise the set is closed only when more than one backend failed: a kind branching
        on the code would see a backend's typo whenever it was the single failure."""
        folded = fold_disposal_failures([DisposalFailure("wierd", "a")])  # type: ignore[list-item]
        assert folded is not None
        assert folded.code == "unknown"
        assert folded.detail == "a"

    def test_a_code_outside_the_vocabulary_does_not_raise(self):
        """`DisposalCode` is a `Literal`, so nothing enforces it at run time - a typo or a
        newer core's word must not raise out of a path that never raises."""
        folded = fold_disposal_failures(
            [DisposalFailure("weird", "a"), DisposalFailure("odd", "b")]  # type: ignore[arg-type]
        )
        assert folded is not None
        assert folded.code == "unknown"

    def test_the_sentence_still_names_the_code_for_whoever_reads_the_log(self):
        """The attribute is the contract; the sentence is what an operator sees in an alert,
        and it should not need the attribute beside it to be intelligible."""
        router = self._router(
            InProcessSandboxBackend(dispose_failure=DisposalFailure("unreachable", "daemon down"))
        )
        asyncio.run(router.dispose_unclean(_KEY, timeout=1.0))
        with pytest.raises(SandboxUnclean) as refusal:
            asyncio.run(router.acquire(_KEY, _SPEC))
        assert "did not land (unreachable)" in str(refusal.value)
        assert "daemon down" not in str(refusal.value), "the detail is still log-only"

    @pytest.mark.parametrize(
        ("reported", "expected"),
        [
            (DisposalFailure("unreachable", "the daemon is down"), "unreachable"),
            (DisposalFailure("refused", "403"), "refused"),
            (DisposalFailure("unlisted", "the query failed"), "unlisted"),
            (DisposalFailure("unknown", "no idea"), "unknown"),
        ],
    )
    def test_a_backends_code_reaches_the_refusal_intact(self, reported, expected):
        router = self._router(InProcessSandboxBackend(name="acas", dispose_failure=reported))
        asyncio.run(router.dispose_unclean(_KEY, timeout=1.0))
        with pytest.raises(SandboxUnclean) as refusal:
            asyncio.run(router.acquire(_KEY, _SPEC))
        assert refusal.value.code == expected

    def test_the_detail_stays_out_of_the_refusal_and_in_the_log(self, caplog):
        """A backend's detail can carry an endpoint, a subscription id or a raw response body,
        and `acquire` reaches any host directly - not only `sandboxed_tool`, which sanitizes."""
        secret = "https://tenant-7.internal.example/subscriptions/abc-123"
        router = self._router(
            InProcessSandboxBackend(name="acas", dispose_failure=DisposalFailure("refused", secret))
        )
        with caplog.at_level("WARNING"):
            asyncio.run(router.dispose_unclean(_KEY, timeout=1.0))
        with pytest.raises(SandboxUnclean) as refusal:
            asyncio.run(router.acquire(_KEY, _SPEC))

        assert secret not in str(refusal.value), "the detail must not ride the exception"
        assert "refused" in str(refusal.value), "the code is what a caller branches on"
        assert secret in caplog.text, "and the operator still gets it, in the log"

    def test_a_backend_answering_with_neither_shape_does_not_raise(self):
        """The annotation binds nobody at run time, and this release narrows it — a backend
        built against the previous one still answers with whatever it answered before. Reading
        `.code` off that would raise out of a `finally`."""

        class _Odd(InProcessSandboxBackend):
            async def dispose(self, key: SandboxKey):  # type: ignore[override]
                return True

        router = self._router(_Odd())
        assert asyncio.run(router.dispose_unclean(_KEY, timeout=1.0)) is False
        with pytest.raises(SandboxUnclean) as refusal:
            asyncio.run(router.acquire(_KEY, _SPEC))
        assert refusal.value.code == "unknown"

    def test_a_bound_that_expired_is_a_timeout_not_a_guess(self):
        class _Hangs(InProcessSandboxBackend):
            async def dispose(self, key: SandboxKey) -> DisposalFailure | None:
                await asyncio.Event().wait()

        router = self._router(_Hangs())
        asyncio.run(router.dispose_unclean(_KEY, timeout=0.05))
        with pytest.raises(SandboxUnclean) as refusal:
            asyncio.run(router.acquire(_KEY, _SPEC))
        assert refusal.value.code == "timeout"

    def test_a_timeout_does_not_outrank_what_a_previous_attempt_reported(self):
        """`unreachable` is first in the precedence because it is the most actionable; a later
        bound expiring must not discard it."""

        class _Hangs(InProcessSandboxBackend):
            async def dispose(self, key: SandboxKey) -> DisposalFailure | None:
                if self.dispose_failure is None:
                    await asyncio.Event().wait()  # never returns; the bound expires first
                return self.dispose_failure

        backend = _Hangs(dispose_failure=DisposalFailure("unreachable", "daemon down"))
        router = self._router(backend)
        asyncio.run(router.dispose_unclean(_KEY, timeout=1.0))
        backend.dispose_failure = None
        asyncio.run(router.dispose_unclean(_KEY, timeout=0.05))
        with pytest.raises(SandboxUnclean) as refusal:
            asyncio.run(router.acquire(_KEY, _SPEC))
        assert refusal.value.code == "unreachable"

    def test_a_code_reported_before_the_bound_expired_outranks_the_timeout(self):
        """The *same* attempt, not a previous one: with several backends, a reason held in a
        local list until the loop ends dies with the coroutine the bound cancels, and the
        timeout is then recorded over a code that outranks it."""

        class _Hangs(InProcessSandboxBackend):
            async def dispose(self, key: SandboxKey) -> DisposalFailure | str | None:
                await asyncio.Event().wait()

        router = self._router(
            InProcessSandboxBackend(
                name="acas", dispose_failure=DisposalFailure("unreachable", "the daemon is down")
            ),
            _Hangs(name="docker"),
        )
        asyncio.run(router.dispose_unclean(_KEY, timeout=0.05))
        with pytest.raises(SandboxUnclean) as refusal:
            asyncio.run(router.acquire(_KEY, _SPEC))
        assert refusal.value.code == "unreachable"

    def test_a_backend_that_breaks_its_contract_and_raises_is_unknown(self):
        """Nothing a backend says while violating never-raises can be classified."""
        router = self._router(
            InProcessSandboxBackend(name="bad", dispose_error=RuntimeError("boom"))
        )
        asyncio.run(router.dispose_unclean(_KEY, timeout=1.0))
        with pytest.raises(SandboxUnclean) as refusal:
            asyncio.run(router.acquire(_KEY, _SPEC))
        assert refusal.value.code == "unknown"


class TestOnlyTheUncleanPathClosesAKey:
    """`dispose` is best-effort and claims nothing about what the sandbox held."""

    def _router(self, *backends, **kwargs):
        return SandboxRouter(list(backends), min_isolation=Isolation.NONE, **kwargs)

    def test_a_plain_dispose_that_fails_leaves_the_key_servable(self):
        """Its caller never said the sandbox was unclean, so a transient failure here must not
        make the key unservable - and `dispose` returns nothing, so nobody would know why."""
        router = self._router(
            InProcessSandboxBackend(dispose_failure=DisposalFailure("unreachable", "transient"))
        )
        asyncio.run(router.dispose(_KEY))
        asyncio.run(router.acquire(_KEY, _SPEC))

    def test_a_plain_dispose_still_clears_a_refusal_when_it_lands(self):
        backend = InProcessSandboxBackend(dispose_failure=DisposalFailure("refused", "down"))
        router = self._router(backend)
        asyncio.run(router.dispose_unclean(_KEY, timeout=1.0))
        backend.dispose_failure = None
        asyncio.run(router.dispose(_KEY))
        asyncio.run(router.acquire(_KEY, _SPEC))

    def test_the_opt_down_does_not_loosen_the_bound(self):
        """`FailedReclaimPolicy.KEEP` is about not closing the key. The bound is about not hanging the call
        that asked — and this method validates `timeout` precisely so it always holds."""

        class _Hangs(InProcessSandboxBackend):
            async def dispose(self, key: SandboxKey) -> DisposalFailure | str | None:
                await asyncio.Event().wait()

        router = self._router(
            _Hangs(),
            reclaim=ReclaimConfig(failed_reclaim_policy=FailedReclaimPolicy.KEEP),
        )

        async def scenario() -> bool:
            # `wait_for` well past the bound, so a regression fails the test rather than hanging
            # the suite on a backend that never returns.
            return await asyncio.wait_for(router.dispose_unclean(_KEY, timeout=0.05), timeout=5)

        assert asyncio.run(scenario()) is False
        asyncio.run(router.acquire(_KEY, _SPEC))  # and the opt-down still leaves the key servable

    def test_keep_policy_opts_down_from_the_refusal_too(self):
        """The host asked the framework not to destroy a sandbox it could not clean; closing
        the key is the other half of that same act."""
        router = self._router(
            InProcessSandboxBackend(dispose_failure=DisposalFailure("refused", "down")),
            reclaim=ReclaimConfig(failed_reclaim_policy=FailedReclaimPolicy.KEEP),
        )
        assert asyncio.run(router.dispose_unclean(_KEY, timeout=1.0)) is False
        asyncio.run(router.acquire(_KEY, _SPEC))


class _BlocksUntilReleased(InProcessSandboxBackend):
    """A backend whose `dispose` parks until let go, so two can be made to contend."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.reset()

    def reset(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def dispose(self, key: SandboxKey) -> DisposalFailure | None:
        self.entered.set()
        await self.release.wait()
        return None


class TestDisposalsForOneKeyDoNotInterleave:
    """#642 — acquire and disposal for one key were not coordinated at all.

    Only the disposals are serialised. `acquire` takes no lock: its ledger reads carry no
    await and are already atomic, and a lock held across a cold create would block the very
    disposal that exists to bound how long a dirty sandbox lives.
    """

    def _router(self, *backends, **kwargs):
        return SandboxRouter(list(backends), min_isolation=Isolation.NONE, **kwargs)

    def test_a_fast_disposal_does_not_clear_a_slow_one_underneath_it(self):
        """A disposal must not clear the key while another is still deleting it. Each marks the
        key before its first await, so the one that lands first would otherwise pop a mark that
        belongs to the one still running, and an acquire in that window is served a sandbox the
        other is about to destroy."""
        inside = asyncio.Event()
        release = asyncio.Event()
        calls = 0

        class _SlowFirst(InProcessSandboxBackend):
            async def dispose(self, key: SandboxKey) -> DisposalFailure | None:
                nonlocal calls
                calls += 1
                if calls == 1:
                    inside.set()
                    await release.wait()
                return None

        async def scenario() -> bool:
            router = self._router(_SlowFirst())
            slow = asyncio.create_task(router.dispose_unclean(_KEY, timeout=5))
            await inside.wait()
            fast = asyncio.create_task(router.dispose_unclean(_KEY, timeout=5))
            await asyncio.sleep(0.05)
            open_mid_flight = _KEY not in router._unclean  # noqa: SLF001
            release.set()
            await asyncio.gather(slow, fast)
            return open_mid_flight

        assert asyncio.run(scenario()) is False, "the key was reopened mid-disposal"

    def test_an_acquire_mid_create_is_refused_when_the_key_closes_under_it(self):
        """Race F. The ledger check runs before the create's await and nothing re-read it
        after, so a disposal that failed meanwhile left the caller holding a sandbox for a key
        that is now refused."""
        creating = asyncio.Event()
        release = asyncio.Event()

        class _SlowCreate(InProcessSandboxBackend):
            async def acquire(self, key: SandboxKey, spec: SandboxSpec):
                creating.set()
                await release.wait()
                return await super().acquire(key, spec)

            async def dispose(self, key: SandboxKey) -> DisposalFailure | None:
                return DisposalFailure("refused", "still there")

        async def scenario() -> None:
            router = self._router(_SlowCreate())
            acquiring = asyncio.create_task(router.acquire(_KEY, _SPEC))
            await creating.wait()
            await router.dispose_unclean(_KEY, timeout=5)
            release.set()
            with pytest.raises(SandboxUnclean, match="while this acquire was creating it"):
                await acquiring

        asyncio.run(scenario())

    def test_the_sandbox_that_acquire_created_is_disposed_before_it_refuses(self):
        """A refused acquire owes nothing billable left running — the same rule the reclaim
        refusal follows, and the reason this does not simply raise."""
        creating = asyncio.Event()
        release = asyncio.Event()
        disposed: list[SandboxKey] = []

        class _SlowCreate(InProcessSandboxBackend):
            async def acquire(self, key: SandboxKey, spec: SandboxSpec):
                creating.set()
                await release.wait()
                return await super().acquire(key, spec)

            async def dispose(self, key: SandboxKey) -> DisposalFailure | None:
                disposed.append(key)
                return DisposalFailure("refused", "still there") if len(disposed) == 1 else None

        async def scenario() -> None:
            router = self._router(_SlowCreate())
            acquiring = asyncio.create_task(router.acquire(_KEY, _SPEC))
            await creating.wait()
            await router.dispose_unclean(_KEY, timeout=5)
            release.set()
            with pytest.raises(SandboxUnclean):
                await acquiring

        asyncio.run(scenario())
        assert len(disposed) == 2, "the sandbox created mid-refusal was left running"

    def test_a_slow_create_does_not_hold_up_a_disposal(self):
        """What `acquire` taking the lock would have cost. `dispose_unclean` runs in a tool
        call's `finally` under a bound; waiting on a cold create could burn that bound having
        attempted nothing, and the mechanism that limits a dirty sandbox's life is the one
        thing a create must never block."""
        creating = asyncio.Event()
        release = asyncio.Event()

        class _NeverFinishesCreating(InProcessSandboxBackend):
            async def acquire(self, key: SandboxKey, spec: SandboxSpec):
                creating.set()
                await release.wait()
                return await super().acquire(key, spec)

        async def scenario() -> bool:
            router = self._router(_NeverFinishesCreating())
            acquiring = asyncio.create_task(router.acquire(_KEY, _SPEC))
            await creating.wait()
            try:
                # Far under the create, which never finishes on its own.
                landed = await asyncio.wait_for(router.dispose_unclean(_KEY, timeout=5), 2)
            finally:
                release.set()
                acquiring.cancel()
            return landed

        assert asyncio.run(scenario()) is True

    def test_the_refusal_says_when_it_could_not_dispose_what_it_created(self):
        """An operator who reads "disposed" stops looking, and what is still running bills."""
        creating = asyncio.Event()
        release = asyncio.Event()

        class _NothingGoes(InProcessSandboxBackend):
            async def acquire(self, key: SandboxKey, spec: SandboxSpec):
                creating.set()
                await release.wait()
                return await super().acquire(key, spec)

            async def dispose(self, key: SandboxKey) -> DisposalFailure | None:
                return DisposalFailure("refused", "still there")

        async def scenario() -> None:
            router = self._router(_NothingGoes())
            acquiring = asyncio.create_task(router.acquire(_KEY, _SPEC))
            await creating.wait()
            await router.dispose_unclean(_KEY, timeout=5)
            release.set()
            with pytest.raises(SandboxUnclean, match="could not be disposed either"):
                await acquiring

        asyncio.run(scenario())

    def test_a_bound_that_expires_does_not_refuse_a_key_another_disposal_cleaned(self):
        """A landed disposal clears the key — including the mark a second one wrote before
        queueing behind it. If that second one then times out, the ledger has no entry for the
        key, and `get` flattens absent to marked-with-no-reason: the timeout is written and a
        key whose sandbox is gone stays refused for ever."""
        first = asyncio.Event()
        release = asyncio.Event()

        class _LandsThenHangs(InProcessSandboxBackend):
            async def dispose(self, key: SandboxKey) -> DisposalFailure | None:
                if first.is_set():
                    await asyncio.Event().wait()  # the second disposal runs into its bound
                first.set()
                await release.wait()
                return None

        async def scenario() -> bool:
            router = self._router(_LandsThenHangs())
            lands = asyncio.create_task(router.dispose(_KEY))
            await first.wait()
            times_out = asyncio.create_task(router.dispose_unclean(_KEY, timeout=0.1))
            await asyncio.sleep(0)  # it marks the key, then queues behind the lock
            release.set()  # the first lands and pops the mark with it
            assert await lands is None, "the first disposal has to land, or nothing pops"
            assert await times_out is False
            return _KEY in router._unclean  # noqa: SLF001

        assert asyncio.run(scenario()) is False, "a key another disposal cleaned was refused"

    def test_the_mid_create_cleanup_waits_for_the_disposal_that_refused_the_key(self):
        """Two deletes for one key at once are what the lock exists to prevent, and this
        cleanup would otherwise be the exception to it — running beside the very disposal whose
        mark sent it here."""
        creating = asyncio.Event()
        created = asyncio.Event()
        holding = asyncio.Event()
        release = asyncio.Event()
        calls: list[str] = []

        class _Backend(InProcessSandboxBackend):
            async def acquire(self, key: SandboxKey, spec: SandboxSpec):
                creating.set()
                await created.wait()
                return await super().acquire(key, spec)

            async def dispose(self, key: SandboxKey) -> DisposalFailure | None:
                calls.append("holder" if not holding.is_set() else "cleanup")
                if not holding.is_set():
                    holding.set()
                    await release.wait()
                    return DisposalFailure("refused", "still there")
                return None

        async def scenario() -> list[str]:
            router = self._router(_Backend())
            acquiring = asyncio.create_task(router.acquire(_KEY, _SPEC))
            await creating.wait()  # in flight before the key is marked, or it refuses at the top
            disposing = asyncio.create_task(router.dispose_unclean(_KEY, timeout=5))
            await holding.wait()  # the disposal has marked the key and holds the lock
            created.set()
            await asyncio.sleep(0.05)  # the acquire sees the mark and reaches for the lock
            during = list(calls)
            release.set()
            with pytest.raises(SandboxUnclean):
                await acquiring
            assert await disposing is False, "the holder refused, so the key stays closed"
            return during

        assert asyncio.run(scenario()) == ["holder"], "the cleanup deleted beside the disposal"

    def test_the_lock_does_not_outlive_the_loop_it_was_made_on(self):
        """A lock binds to the loop that first *waits* on it, so one cached across `asyncio.run`
        calls raises on the second. Contention on each run is what makes that true: an
        uncontended acquire never binds, and a test that only disposes twice passes with a
        single lock shared by every loop."""
        backend = _BlocksUntilReleased()
        router = self._router(backend)

        async def contend() -> None:
            backend.reset()
            held = asyncio.create_task(router.dispose(_KEY))
            await backend.entered.wait()
            waiting = asyncio.create_task(router.dispose(_KEY))
            await asyncio.sleep(0)  # `waiting` reaches the lock and blocks on it
            backend.release.set()
            await asyncio.gather(held, waiting)

        asyncio.run(contend())
        asyncio.run(contend())

    def test_an_idle_key_keeps_no_lock_and_no_loop(self):
        """One lock per key held for ever would grow without bound, and a contended lock
        references its loop — through a strong value that would pin the loop in the weak-keyed
        table too. Both go when the disposals holding it do.

        Counted from inside the loop: `asyncio.run` drops it on the way out, which empties the
        weak-keyed table and makes the same assertion true of any implementation.
        """
        router = self._router(InProcessSandboxBackend())

        async def scenario() -> tuple[int, int]:
            await router.dispose(_KEY)
            gc.collect()
            tables = list(router._disposal_locks.values())  # noqa: SLF001
            return len(tables), sum(len(per_loop) for per_loop in tables)

        tables, locks = asyncio.run(scenario())
        assert tables == 1, "the loop's table went before the assertion could see it"
        assert locks == 0, "the lock outlived the disposal that needed it"


class TestTheRefusalNamesWhy:
    """A host reading `SandboxUnclean` should learn the code without going to the logs."""

    def _router(self, *backends):
        return SandboxRouter(list(backends), min_isolation=Isolation.NONE)

    def test_the_inherited_constructor_survives_the_new_keyword(self):
        """An `OSError` subclass carries a flexible constructor, and a host builds one — in a
        test double, say. Adding `code` must not take the inherited forms away with it."""
        assert SandboxUnclean().code is None
        assert str(SandboxUnclean(2, "no such file")) == "[Errno 2] no such file"
        assert str(SandboxUnclean("plain message")) == "plain message"
        assert SandboxUnclean("m", code="refused").code == "refused"

    def test_a_bare_mark_still_refuses_without_a_code(self):
        router = self._router(InProcessSandboxBackend())
        router.mark_unclean(_KEY)
        with pytest.raises(SandboxUnclean) as refusal:
            asyncio.run(router.acquire(_KEY, _SPEC))
        assert refusal.value.code is None, "nothing reported a code, so there is none to give"

    def test_a_marked_code_outside_the_vocabulary_is_normalised(self):
        """`mark_unclean` is public, so it is the other way an unrecognised code could reach the
        field a kind branches on."""
        router = self._router(InProcessSandboxBackend())
        router.mark_unclean(_KEY, DisposalFailure("wierd", "a typo"))  # type: ignore[arg-type]
        with pytest.raises(SandboxUnclean) as refusal:
            asyncio.run(router.acquire(_KEY, _SPEC))
        assert refusal.value.code == "unknown"

    def test_a_marked_failure_keeps_its_code(self):
        router = self._router(InProcessSandboxBackend())
        router.mark_unclean(_KEY, DisposalFailure("timeout", "the cleanup was cancelled"))
        with pytest.raises(SandboxUnclean) as refusal:
            asyncio.run(router.acquire(_KEY, _SPEC))
        assert refusal.value.code == "timeout"

    def test_a_mark_does_not_overwrite_what_a_disposal_reported(self):
        """What a backend said about the sandbox outranks a cleanup that was cut short."""
        router = self._router(
            InProcessSandboxBackend(dispose_failure=DisposalFailure("refused", "container 7"))
        )
        asyncio.run(router.dispose_unclean(_KEY, timeout=1.0))
        router.mark_unclean(_KEY, DisposalFailure("unknown", "the cleanup was cancelled"))
        with pytest.raises(SandboxUnclean) as refusal:
            asyncio.run(router.acquire(_KEY, _SPEC))
        assert refusal.value.code == "refused"


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
        """`None`, not `Isolation.NONE` — the second would be an opinion, and the weakest one."""
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
            "egress",
            "requires_os_family",
            "host_tools",
        ]
        assert names[: len(settled)] == settled

    @pytest.mark.parametrize("direction", ["files_in", "files_out"])
    def test_both_transfer_directions_ask_for_the_shared_default(self, direction: str):
        """The same object the silent backend ceiling is, so the two cannot drift apart."""
        assert getattr(SandboxSpec(kind="test"), direction) is DEFAULT_TRANSFER_LIMITS

    def test_work_dir_defaults_to_a_conventional_root(self):
        """A default, not a requirement — see `TestWorkDirIsPlatformNeutral` for why it is only that."""
        assert SandboxSpec(kind="test").work_dir == "/maf-sandbox/work"


class TestWorkDirStaysGuestNative:
    """A path is not a platform claim — which matters more now that something else is one.

    The door #111 asked to keep open is now used: `SandboxSpec.requires_os_family` states the
    guest shape a workload needs, and a backend answers with `os_families`. The invariant that
    made that addition possible is the one still pinned here — nothing infers a guest OS from
    `work_dir`, or rejects one for not looking like a guest it expected. The host typed that
    string to suit the image it configured, and reading a platform out of it would invent a
    fact the field never carried.
    """

    @pytest.mark.parametrize(
        "work_dir",
        [
            "C:/agent/maf-sandbox/work",  # a Windows guest's drive-rooted path
            "/opt/somewhere/else",  # a different POSIX root
            r"D:\agent\work",  # a Windows guest's own separator — raw, or `\a` is a bell
        ],
    )
    def test_the_spec_imposes_no_platform_constraint_on_work_dir(self, work_dir: str):
        """`SandboxSpec` accepts any `work_dir`: it neither requires a POSIX-absolute path nor
        infers a guest OS from it, so a future non-Linux-guest backend needs no protocol change."""
        assert SandboxSpec(kind="test", work_dir=work_dir).work_dir == work_dir

    def test_a_spec_asks_for_a_family_and_never_implies_one(self):
        """The ask is its own field, and it defaults to asking nothing. A spec that says
        nothing about its guest is what every spec written before the axis is."""
        fields = {f.name for f in dataclasses.fields(SandboxSpec)}
        assert "requires_os_family" in fields
        assert SandboxSpec(kind="test").requires_os_family is None

    @pytest.mark.parametrize("work_dir", ["C:/agent/work", r"D:\agent\work"])
    def test_a_windows_shaped_work_dir_is_not_read_as_asking_for_a_windows_guest(
        self, work_dir: str
    ):
        """The inference the protocol refuses to make, pinned now that it would have somewhere
        to land: a drive-rooted `work_dir` against a POSIX-only backend is served, because the
        path was never the ask. A router that guessed here would refuse a working deployment
        on the strength of a string the host chose for its own reasons."""
        backend = InProcessSandboxBackend(
            isolation=Isolation.MICROVM, os_families=frozenset({OsFamily.POSIX})
        )
        SandboxRouter([backend]).ensure_can_serve(SandboxSpec(kind="test", work_dir=work_dir))

    def test_a_backend_declaring_no_family_still_serves_a_spec_that_does_not_ask(self):
        """What makes the axis a non-breaking addition, and the reason it is stated as a
        refusal rather than a default: a backend written before `os_families` existed declares
        nothing, and every spec that asks nothing is served by it exactly as before."""
        backend = InProcessSandboxBackend(isolation=Isolation.MICROVM)
        assert backend.os_families == frozenset()
        SandboxRouter([backend]).ensure_can_serve(SandboxSpec(kind="test"))


class TestTheGuestShapeMatch:
    """A spec states the guest shape its commands are written for; the router refuses the rest.

    The axis answers one question and must not be read as answering the other: it says what
    *shape* a guest is — path grammar, argv quoting — and says nothing about what is installed
    in it. A spec asking for POSIX and getting it can still meet an image with no shell.
    """

    def test_a_backend_serving_the_asked_family_serves_the_workload(self):
        backend = InProcessSandboxBackend(
            isolation=Isolation.MICROVM, os_families=frozenset({OsFamily.POSIX})
        )
        SandboxRouter([backend]).ensure_can_serve(
            SandboxSpec(kind="test", requires_os_family=OsFamily.POSIX)
        )

    def test_a_backend_serving_another_family_is_refused(self):
        backend = InProcessSandboxBackend(
            isolation=Isolation.MICROVM, os_families=frozenset({OsFamily.WINDOWS})
        )
        with pytest.raises(SandboxOsFamilyNotSupported) as caught:
            SandboxRouter([backend]).ensure_can_serve(
                SandboxSpec(kind="test", requires_os_family=OsFamily.POSIX)
            )
        assert "windows" in str(caught.value)
        assert "posix" in str(caught.value)

    def test_a_backend_serving_several_families_serves_each_of_them(self):
        """The reason the declaration is a set: one local-hypervisor backend boots more than
        one guest family, and a scalar could not say so without a later redefinition."""
        backend = InProcessSandboxBackend(
            isolation=Isolation.MICROVM,
            os_families=frozenset({OsFamily.POSIX, OsFamily.WINDOWS}),
        )
        router = SandboxRouter([backend])
        for family in (OsFamily.POSIX, OsFamily.WINDOWS):
            router.ensure_can_serve(SandboxSpec(kind="test", requires_os_family=family))

    def test_a_backend_that_declares_nothing_is_refused_only_when_the_spec_asks(self):
        """Silence is the absence of an answer, not a permissive default. A backend with no
        guest in the OS sense — a language runtime, a data-plane API — has nothing to say, and
        a workload that needs a shape must not be served on the strength of that silence."""
        backend = InProcessSandboxBackend(isolation=Isolation.MICROVM)
        with pytest.raises(SandboxOsFamilyNotSupported) as caught:
            SandboxRouter([backend]).ensure_can_serve(
                SandboxSpec(kind="test", requires_os_family=OsFamily.POSIX)
            )
        assert "no guest whose shape it states" in str(caught.value)

    def test_a_declaration_of_the_wrong_shape_refuses_rather_than_admits(self):
        """A mis-shaped declaration cannot widen anything: it is read as empty, so the ask is
        refused. The opposite reading would let a typo serve a workload on any guest at all."""
        backend = InProcessSandboxBackend(isolation=Isolation.MICROVM)
        backend._os_families = "posix"  # pyright: ignore[reportAttributeAccessIssue]
        with pytest.raises(SandboxOsFamilyNotSupported):
            SandboxRouter([backend]).ensure_can_serve(
                SandboxSpec(kind="test", requires_os_family=OsFamily.POSIX)
            )

    def test_the_refusal_happens_at_acquire_too_not_only_at_ensure_can_serve(self):
        """`acquire` runs the same checks, so a caller that skipped the attach gate is refused
        rather than served behind a guest its commands cannot run on."""
        backend = InProcessSandboxBackend(
            isolation=Isolation.MICROVM, os_families=frozenset({OsFamily.WINDOWS})
        )
        router = SandboxRouter([backend])
        with pytest.raises(SandboxOsFamilyNotSupported):
            asyncio.run(
                router.acquire(_KEY, SandboxSpec(kind="test", requires_os_family=OsFamily.POSIX))
            )


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
            "OsFamily",
            "SandboxCapabilityDenied",
            "SandboxCapabilityNotSupported",
            "SandboxIdentityDenied",
            "SandboxOsFamilyNotSupported",
            "SandboxProgramTimeout",
            "SandboxQueuedTimeout",
            "WORK_DIRECTORY",
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
        "_host_tools_over_exec",
        "_outputs",
        "_protocol",
        "_purger",
        "_reclaim",
        "_router",
        "_shim",
        "_shim_wire_contract",
        "conformance",
        "paths",
        "testing",
        # `_guest/` is the source maf-sandbox copies into a guest, kept as real Python. It is
        # stdlib-only by necessity — a guest carries no third-party packages — so the claim this
        # set makes is exactly the one that code has to keep.
        "_guest.__init__",
        "_guest.maf_host_tools",
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
    """Every module in the installed `maf_sandbox`, as `{dotted_name: path}`.

    Keyed by the name relative to the package root — `_guest.maf_host_tools`, not bare
    `maf_host_tools` — so a file in a subpackage cannot collide with a top-level one. Two
    `__init__.py` (the package's own and `_guest/`'s) would otherwise collapse to one `__init__`
    key, hiding one of them and pointing `_package_modules()["__init__"]` at whichever `rglob`
    happened to yield last.
    """
    import pathlib

    import maf_sandbox

    root = pathlib.Path(maf_sandbox.__file__).parent  # type: ignore[arg-type]
    return {
        ".".join(path.relative_to(root).with_suffix("").parts): path for path in root.rglob("*.py")
    }


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


class _CountingBackend(InProcessSandboxBackend):
    """Reports a fixed reclaim count and records every scope it was asked to purge."""

    def __init__(self, *, reclaims: int = 2) -> None:
        super().__init__()
        self._reclaims = reclaims
        self.purged: list[tuple[str, str]] = []

    async def dispose_scope(self, scope: str, thread_id: str) -> ScopePurge:
        self.purged.append((scope, thread_id))
        return ScopePurge(self._reclaims)


class TestScope:
    """`router.scope()` — disposal that does not depend on the host remembering it.

    Every sample wrote the same try/finally around `dispose_scope`, and the reason it is worth
    packaging is in that method's own docstring: a sandbox nobody reclaims is a sandbox
    somebody pays for.
    """

    def test_it_disposes_when_the_block_ends(self):
        backend = _CountingBackend()
        router = SandboxRouter([backend], min_isolation=Isolation.NONE)

        async def scenario():
            async with router.scope("scope-a", "thread-1") as disposal:
                assert disposal.disposed == 0, "the count means nothing until the block ends"
            return disposal

        disposal = asyncio.run(scenario())
        assert backend.purged == [("scope-a", "thread-1")]
        assert disposal.disposed == 2

    def test_it_disposes_when_the_block_raises_and_does_not_swallow_the_error(self):
        """A teardown that hid the application's exception would be worse than none."""
        backend = _CountingBackend()
        router = SandboxRouter([backend], min_isolation=Isolation.NONE)

        async def scenario():
            async with router.scope("scope-a", "thread-1"):
                raise ValueError("the workload failed")

        with pytest.raises(ValueError, match="the workload failed"):
            asyncio.run(scenario())
        assert backend.purged == [("scope-a", "thread-1")]

    def test_a_backend_that_cannot_purge_does_not_break_the_block(self):
        """`dispose_scope` already swallows and logs per-backend failures. That is what makes
        it safe in a `finally`, so it is pinned here rather than assumed."""
        router = SandboxRouter([_ExplodingBackend()], min_isolation=Isolation.NONE)

        async def scenario():
            async with router.scope("scope-a", "thread-1") as disposal:
                pass
            return disposal

        assert asyncio.run(scenario()).disposed == 0


class TestASandboxThatCannotBeReclaimed:
    """What gates `reclaim`, since no capability does.

    Without this a stale backend acquires cleanly and the loss is reported once per call, for
    the life of the process, as a removal that failed.
    """

    class _Stale(InProcessSandbox):
        reclaim = None  # type: ignore[assignment]

    def _router(self) -> SandboxRouter:
        return SandboxRouter([InProcessSandboxBackend(self._Stale())], min_isolation=Isolation.NONE)

    def test_acquire_refuses_and_names_the_member(self):
        with pytest.raises(TypeError, match="does not implement `Sandbox.reclaim`"):
            asyncio.run(self._router().acquire(_KEY, _SPEC))

    def test_the_refused_sandbox_is_disposed_not_left_running(self):
        """The backend acquired before the check could refuse, and a refused acquire must
        not leave a billable sandbox running — nothing else would ever clean it."""
        backend = InProcessSandboxBackend(self._Stale())
        with pytest.raises(TypeError):
            asyncio.run(SandboxRouter([backend], min_isolation=Isolation.NONE).acquire(_KEY, _SPEC))
        assert backend.disposed == [_KEY]

    def test_a_disposal_that_fails_does_not_replace_the_refusal(self):
        class _KeepsItsSandboxes(InProcessSandboxBackend):
            async def dispose(self, key):
                await super().dispose(key)
                raise RuntimeError("the control plane is down")

        backend = _KeepsItsSandboxes(self._Stale())
        with pytest.raises(TypeError, match="does not implement"):
            asyncio.run(SandboxRouter([backend], min_isolation=Isolation.NONE).acquire(_KEY, _SPEC))
        assert backend.disposed == [_KEY], "the disposal was not even attempted"

    def test_the_refusal_says_what_proves_an_implementation(self):
        with pytest.raises(TypeError, match="assert_reclaim_conformance"):
            asyncio.run(self._router().acquire(_KEY, _SPEC))

    def test_an_ordinary_sandbox_still_acquires(self):
        """A guard that refuses everything is an outage, not a guard."""
        router = SandboxRouter([InProcessSandboxBackend()], min_isolation=Isolation.NONE)
        assert asyncio.run(router.acquire(_KEY, _SPEC)) is not None
