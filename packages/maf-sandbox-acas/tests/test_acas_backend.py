"""Offline tests for the ACA Sandboxes backend.

No live sandbox group and no host application: the group client is replaced by a fake, and
the disk-image tests build the **real** SDK dataclasses so the shape they assert is the
SDK's rather than one the code and the fake happen to agree on.
"""

from __future__ import annotations

import asyncio
import logging
import posixpath
import shlex
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from types import SimpleNamespace

import pytest
from maf_sandbox import (
    Capability,
    Cleanup,
    DisposalFailure,
    Egress,
    EgressRule,
    Isolation,
    IsolationScope,
    OsFamily,
    SandboxBackend,
    SandboxCapabilityNotSupported,
    SandboxEgressNotEnforced,
    SandboxKey,
    SandboxOsFamilyNotSupported,
    SandboxRouter,
    SandboxSpec,
    ScopePurge,
)

from maf_sandbox_acas import (
    BACKEND_NAME,
    AcasEgressPolicyConflict,
    AcasEntryPayloadIncomplete,
    AcasSandboxBackend,
    AcasSandboxConfig,
    disk_image_base,
    resolve_disk_image_id,
)
from maf_sandbox_acas._backend import _Held, _RemovalHint, _sandbox_labels

_ENDPOINT = "https://management.example.azuredevcompute.io"


def _config(**overrides) -> AcasSandboxConfig:
    return AcasSandboxConfig(endpoint=_ENDPOINT, **overrides)


def _disk_image(image_id: str, reference: str):
    """A listed disk image, built from the real SDK dataclasses.

    Deliberately not hand-rolled: ``DiskImage.image`` is a ``DiskImageSpec`` whose ``base``
    carries the reference, and an earlier fake that set ``.image`` to a plain string made
    every resolution test pass against a resolver that could never match a real listing.
    """
    from azure.containerapps.sandbox import DiskImage, DiskImageSpec

    return DiskImage(id=image_id, image=DiskImageSpec(base=reference))


def test_acquire_creates_and_repairs_the_base_through_the_data_plane():
    async def scenario():
        client = _GuestGroupClient(_guest_removing(True))
        backend = _backend_with(client)
        key = SandboxKey("work-dir", "thread", "agent")
        spec = _spec_requiring(Capability.EXEC)
        first = await backend.acquire(key, spec)
        assert spec.work_dir is not None
        assert client.created_directories == ["/maf-sandbox", "/maf-sandbox/work"]
        client.files[first.instance_id][spec.work_dir + "/keep"] = b"keep"
        second = await backend.acquire(key, spec)
        assert first.instance_id == second.instance_id
        assert len(client.created_directories) == 2
        assert client.files[first.instance_id][spec.work_dir + "/keep"] == b"keep"
        del client.files[first.instance_id][spec.work_dir]
        await backend.acquire(key, spec)
        assert client.created_directories == [
            "/maf-sandbox",
            "/maf-sandbox/work",
            "/maf-sandbox/work",
        ]

    asyncio.run(scenario())


class _FakePager:
    """Stands in for AsyncItemPaged."""

    def __init__(self, items: list, on_iter=None) -> None:
        self._items = items
        self._on_iter = on_iter

    def __aiter__(self):
        if self._on_iter is not None:
            self._on_iter()

        async def _gen():
            for item in self._items:
                yield item

        return _gen()


class _CompletedDeletion:
    async def result(self) -> None:
        return None


class _FakeSandboxClient:
    def __init__(self, sandbox_id: str) -> None:
        self.sandbox_id = sandbox_id
        self.deleted = False
        self.resumed = False
        self._sbx_path = "/sandboxes/" + sandbox_id
        self._api_version = "test"

    async def _dp_get(self, route, *, params):
        return {"isDir": True, "isSymlink": False}

    async def begin_delete(self) -> _CompletedDeletion:
        self.deleted = True
        return _CompletedDeletion()

    async def exec(self, command: str, *, working_directory: str):
        return SimpleNamespace(exit_code=0, stdout="", stderr="")

    async def ensure_running(self, timeout: float | None = None) -> None:
        """The resume path `acquire` takes when it finds a registered sandbox."""
        self.resumed = True


class _FakeGroupClient:
    def __init__(self, images: list | None = None, sandboxes: list | None = None) -> None:
        self._images = images or []
        self._sandboxes = sandboxes or []
        self.list_calls = 0
        self.last_labels: dict | None = None
        self.deleted: list[str] = []

    def list_disk_images(self):
        return _FakePager(self._images, on_iter=self._count)

    def list_sandboxes(self, *, labels=None):
        self.last_labels = labels
        return _FakePager(self._sandboxes)

    def get_sandbox_client(self, sandbox_id: str):
        self.deleted.append(sandbox_id)
        return _FakeSandboxClient(sandbox_id)

    def _count(self):
        self.list_calls += 1


class _FakeSandbox:
    def __init__(self, sandbox_id: str) -> None:
        self.id = sandbox_id


class _ExplodingGroupClient:
    def list_sandboxes(self, *, labels=None):
        raise RuntimeError("service unavailable")

    def get_sandbox_client(self, sandbox_id: str):
        raise RuntimeError("service unavailable")


def _backend_with(group_client, config: AcasSandboxConfig | None = None) -> AcasSandboxBackend:
    """A backend whose group client is the given fake.

    Injected by overriding the one protected accessor rather than by patching
    ``sys.modules``: the seam exists precisely so the backend can be exercised without
    Azure, and using it here is what proves it is a real seam.
    """
    backend = AcasSandboxBackend(config or _config())
    backend._group_client = lambda: group_client  # type: ignore[method-assign]
    return backend


# ---------------------------------------------------------------------------
# Backend identity — read by the router's floor check
# ---------------------------------------------------------------------------


class TestBackendIdentity:
    def test_satisfies_the_backend_protocol(self):
        assert isinstance(AcasSandboxBackend(_config()), SandboxBackend)

    def test_declares_microvm_isolation(self):
        """Supersedes `vm` from the three-rung ladder: `microvm` is the truthful rung, and the default floor."""
        assert AcasSandboxBackend(_config()).isolation == Isolation.MICROVM

    def test_declares_allowlist_egress(self):
        """A workload's tool attaches because of this; `TestEgressPolicy` pins that it is true."""
        assert AcasSandboxBackend(_config()).declarations.egress_modes == frozenset(
            {Egress.ALLOWLIST, Egress.CLOSED}
        )

    def test_declares_exec_files_in_the_whole_pull_surface_and_host_tools(self):
        """Declares only what it implements today — no ATTACHED_IDENTITY and no SNAPSHOT."""
        assert AcasSandboxBackend(_config()).declarations.capabilities == frozenset(
            {
                Capability.EXEC,
                Capability.FILES_IN,
                Capability.FILES_OUT,
                Capability.FILES_LIST,
                Capability.FILES_DELETE,
                Capability.HOST_TOOLS,
            }
        )

    def test_declares_files_delete(self):
        assert Capability.FILES_DELETE in AcasSandboxBackend(_config()).declarations.capabilities

    def test_is_the_only_backend_that_can_declare_files_list(self):
        """Native enumeration is the split's own test — name the backend that lacks it."""
        assert Capability.FILES_LIST in AcasSandboxBackend(_config()).declarations.capabilities

    def test_declares_transfer_ceilings_that_admit_a_spec_saying_nothing(self):
        """The spec-side default must stay within them, or every existing spec fails at attach."""
        from maf_sandbox import DEFAULT_TRANSFER_LIMITS

        limits = AcasSandboxBackend(_config()).declarations.limits
        assert DEFAULT_TRANSFER_LIMITS.within(limits.files_in)
        assert DEFAULT_TRANSFER_LIMITS.within(limits.files_out)

    def test_a_spec_requiring_the_pull_surface_is_admitted(self):
        from maf_sandbox import SandboxSpec

        router = SandboxRouter([AcasSandboxBackend(_config())])
        router.ensure_can_serve(
            SandboxSpec(
                kind="k",
                requires=frozenset({Capability.EXEC, Capability.FILES_OUT, Capability.FILES_LIST}),
            )
        )  # does not raise

    def test_a_codeact_style_spec_wiring_host_tools_is_admitted(self):
        """The whole point of declaring it: the spec a wired registry produces now attaches.

        Asserted through `ensure_can_serve` rather than by re-reading the frozenset, because the
        set agreeing with itself is not the property that changed — a spec being admitted is.
        This is the exact `requires` `codeact_sandbox_spec` builds for a non-empty registry, so
        it fails if either side of that pair drifts.
        """
        from maf_sandbox import SandboxSpec

        router = SandboxRouter([AcasSandboxBackend(_config())])
        spec = SandboxSpec(
            kind="codeact",
            requires=frozenset(
                {
                    Capability.EXEC,
                    Capability.FILES_IN,
                    Capability.FILES_OUT,
                    Capability.HOST_TOOLS,
                }
            ),
        )

        router.ensure_can_serve(spec)  # does not raise

    def test_a_spec_asking_above_the_transfer_ceiling_is_refused(self):
        from maf_sandbox import SandboxSpec, SandboxTransferLimitsNotPermitted, TransferLimits

        backend = AcasSandboxBackend(_config())
        ceiling = backend.declarations.limits.files_out
        spec = SandboxSpec(
            kind="k",
            requires=frozenset({Capability.EXEC, Capability.FILES_OUT}),
            files_out=TransferLimits(
                max_bytes_per_file=ceiling.max_bytes_per_file + 1,
                max_total_bytes=ceiling.max_total_bytes,
                max_files=ceiling.max_files,
            ),
        )
        with pytest.raises(SandboxTransferLimitsNotPermitted):
            SandboxRouter([backend]).ensure_can_serve(spec)

    def test_meets_the_default_floor(self):
        """Migration guarantee: a host that used `deployed=True` behaves identically."""
        assert SandboxRouter([AcasSandboxBackend(_config())]).enabled

    def test_is_named_aca(self):
        # The literal, on purpose. `name == BACKEND_NAME` below pins them to each other and
        # would stay green if both moved together — and both moving together is precisely the
        # change that silently breaks every host with `selected="acas"` in its configuration.
        assert AcasSandboxBackend(_config()).name == "acas"

    def test_the_exported_constant_is_the_name_the_backend_answers_to(self):
        """#411: the value exists without building a backend, and cannot drift from it.

        Worth more here than for the other two: constructing this backend means a
        subscription, a credential and a resource group, which is a great deal of setup to
        reach a fixed string a host needs while it is still reading configuration.
        """
        assert BACKEND_NAME == AcasSandboxBackend(_config()).name

    def test_selecting_by_the_constant_resolves_to_this_backend(self):
        """What the constant is for, exercised rather than asserted.

        `selected=` is a string match against `.name`, so this is the only test that would fail
        if the constant were right and the property were reading something else.
        """
        backend = AcasSandboxBackend(_config())
        assert SandboxRouter([backend], selected=BACKEND_NAME).backend is backend


# ---------------------------------------------------------------------------
# The guest family — a constant here, matched by the router at attach (#588)
# ---------------------------------------------------------------------------


class TestGuestFamilyDeclaration:
    """`os_families` is stated rather than read: every sandbox the service boots is Linux.

    That constant is what `exec`'s `shlex.join` quoting and this backend's `posixpath` path
    arithmetic are written against, so the router matches it rather than taking it on trust.
    """

    def test_declares_posix(self):
        assert AcasSandboxBackend(_config()).declarations.os_families == frozenset({OsFamily.POSIX})

    def test_no_configuration_moves_it(self):
        """A property of the service, so nothing a host sets may reach it — including `image`.

        The registry and the images are the settings closest to the guest, and neither can
        make the service boot something that is not a Linux microVM.
        """
        for config in (
            _config(),
            _config(registry="other.azurecr.io"),
            _config(subscription_id="sub-2", resource_group="rg-2"),
        ):
            assert AcasSandboxBackend(config).declarations.os_families == frozenset(
                {OsFamily.POSIX}
            )


class TestTheRouterMatchesTheDeclaredFamily:
    """The point of the declaration: an axis that refuses something, at attach."""

    @staticmethod
    def _router() -> SandboxRouter:
        return SandboxRouter([AcasSandboxBackend(_config())])

    def test_a_posix_workload_is_served(self):
        self._router().ensure_can_serve(
            SandboxSpec(kind="bicep", requires_os_family=OsFamily.POSIX)
        )

    def test_a_windows_workload_is_refused(self):
        with pytest.raises(SandboxOsFamilyNotSupported):
            self._router().ensure_can_serve(
                SandboxSpec(kind="bicep", requires_os_family=OsFamily.WINDOWS)
            )

    def test_a_spec_naming_no_family_is_served(self):
        """A spec that names no family is refused by nothing, which keeps the axis additive."""
        self._router().ensure_can_serve(SandboxSpec(kind="bicep"))


# ---------------------------------------------------------------------------
# disk_image_base — the accessor that reads the OCI reference off a listed image
# ---------------------------------------------------------------------------


class TestDiskImageBase:
    def test_reads_the_reference_out_of_the_spec(self):
        assert disk_image_base(_disk_image("img-1", "acr.io/x:1")) == "acr.io/x:1"

    def test_the_spec_object_is_not_itself_the_reference(self):
        """Pins the exact confusion this accessor exists to prevent."""
        assert _disk_image("img-1", "acr.io/x:1").image != "acr.io/x:1"

    def test_tolerates_a_flattened_string_field(self):
        class _Flattened:
            image = "acr.io/x:1"

        assert disk_image_base(_Flattened()) == "acr.io/x:1"

    def test_returns_none_when_absent_or_empty(self):
        from azure.containerapps.sandbox import DiskImage, DiskImageSpec

        assert disk_image_base(DiskImage(id="i")) is None
        assert disk_image_base(DiskImage(id="i", image=DiskImageSpec(base=""))) is None
        assert disk_image_base(object()) is None


class TestQualifyImageReference:
    """A kind declares `repository:tag`; the backend knows which registry holds it."""

    def test_prefixes_a_bare_repository_and_tag(self):
        from maf_sandbox_acas._images import qualify_image_reference

        assert qualify_image_reference("acr.azurecr.io", "bicep-sandbox:0.46.1") == (
            "acr.azurecr.io/bicep-sandbox:0.46.1"
        )

    def test_a_tag_colon_is_not_mistaken_for_a_port(self):
        """`bicep-sandbox:0.46.1` has a colon but no registry — the trap in this rule."""
        from maf_sandbox_acas._images import qualify_image_reference

        # Whole-string equality rather than a prefix check: `startswith` on something that
        # looks like a URL is the shape of an incomplete-sanitization bug, and a scanner
        # cannot tell an assertion from a security check. The full form is stricter anyway.
        assert qualify_image_reference("acr.azurecr.io", "img:1.2.3") == "acr.azurecr.io/img:1.2.3"

    def test_leaves_an_already_qualified_reference_alone(self):
        """Double-prefixing surfaces only as "no disk image was built from …", far away."""
        from maf_sandbox_acas._images import qualify_image_reference

        assert qualify_image_reference("acr.azurecr.io", "other.azurecr.io/img:1") == (
            "other.azurecr.io/img:1"
        )

    def test_a_repository_path_is_not_a_registry(self):
        from maf_sandbox_acas._images import qualify_image_reference

        assert qualify_image_reference("acr.azurecr.io", "library/ubuntu:22.04") == (
            "acr.azurecr.io/library/ubuntu:22.04"
        )

    def test_localhost_and_ports_count_as_registries(self):
        from maf_sandbox_acas._images import qualify_image_reference

        assert qualify_image_reference("acr.io", "localhost/img:1") == "localhost/img:1"
        assert qualify_image_reference("acr.io", "reg:5000/img:1") == "reg:5000/img:1"

    def test_no_registry_configured_leaves_the_image_untouched(self):
        from maf_sandbox_acas._images import qualify_image_reference

        assert qualify_image_reference("", "img:1") == "img:1"

    def test_a_trailing_slash_on_the_registry_does_not_double_up(self):
        from maf_sandbox_acas._images import qualify_image_reference

        assert qualify_image_reference("acr.azurecr.io/", "img:1") == "acr.azurecr.io/img:1"


class TestResolveDiskImageId:
    def setup_method(self):
        from maf_sandbox_acas._images import _disk_image_cache

        _disk_image_cache.clear()

    def test_explicit_id_wins_without_listing(self):
        client = _FakeGroupClient(images=[_disk_image("img-1", "acr.io/x:1")])
        assert asyncio.run(resolve_disk_image_id(client, "explicit-id", "acr.io/x:1")) == (
            "explicit-id"
        )
        assert client.list_calls == 0

    def test_resolves_reference_from_the_group(self):
        client = _FakeGroupClient(
            images=[_disk_image("img-other", "acr.io/y:1"), _disk_image("img-1", "acr.io/x:1")]
        )
        assert asyncio.run(resolve_disk_image_id(client, None, "acr.io/x:1")) == "img-1"

    def test_resolution_is_cached(self):
        client = _FakeGroupClient(images=[_disk_image("img-1", "acr.io/x:1")])
        asyncio.run(resolve_disk_image_id(client, None, "acr.io/x:1"))
        asyncio.run(resolve_disk_image_id(client, None, "acr.io/x:1"))
        assert client.list_calls == 1

    @pytest.mark.parametrize("ids", [("img-old", "img-new"), ("img-new", "img-old")])
    def test_multiple_snapshots_require_an_explicit_id(self, ids):
        client = _FakeGroupClient(images=[_disk_image(image_id, "acr.io/x:1") for image_id in ids])
        with pytest.raises(ValueError, match="Multiple disk images") as raised:
            asyncio.run(resolve_disk_image_id(client, None, "acr.io/x:1"))
        assert "img-new, img-old" in str(raised.value)
        assert "Pin a disk-image id" in str(raised.value)
        assert asyncio.run(resolve_disk_image_id(client, "img-new", "acr.io/x:1")) == "img-new"
        assert client.list_calls == 1

    def test_ambiguous_resolution_is_not_cached(self):
        client = _FakeGroupClient(
            images=[_disk_image("img-old", "acr.io/x:1"), _disk_image("img-new", "acr.io/x:1")]
        )
        with pytest.raises(ValueError, match="Multiple disk images"):
            asyncio.run(resolve_disk_image_id(client, None, "acr.io/x:1"))
        client._images = [_disk_image("img-new", "acr.io/x:1")]
        assert asyncio.run(resolve_disk_image_id(client, None, "acr.io/x:1")) == "img-new"
        assert client.list_calls == 2

    def test_repeated_listing_of_one_id_is_not_ambiguous(self):
        client = _FakeGroupClient(images=[_disk_image("img-1", "acr.io/x:1")] * 2)
        assert asyncio.run(resolve_disk_image_id(client, None, "acr.io/x:1")) == "img-1"

    def test_raises_when_nothing_configured(self):
        with pytest.raises(ValueError, match="No sandbox image is configured"):
            asyncio.run(resolve_disk_image_id(_FakeGroupClient(), None, None))

    def test_raises_when_reference_not_imported(self):
        client = _FakeGroupClient(images=[_disk_image("img-1", "acr.io/other:1")])
        with pytest.raises(ValueError, match="import_disk_image"):
            asyncio.run(resolve_disk_image_id(client, None, "acr.io/x:1"))

    def test_nothing_configured_offers_both_namespaces(self):
        """The message is where a new deployment learns it need not import anything."""
        with pytest.raises(ValueError, match="python-3.13") as raised:
            asyncio.run(resolve_disk_image_id(_FakeGroupClient(), None, None))
        assert "import_disk_image" in str(raised.value)


# ---------------------------------------------------------------------------
# The two image namespaces — one the service prebuilt, one this deployment imported
# ---------------------------------------------------------------------------


def _catalogue_entry(name: str):
    from azure.containerapps.sandbox import PublicDiskImage

    return PublicDiskImage(name=name)


class _CataloguedGroupClient:
    """Records how the create call named its source, which is the whole question here.

    ``begin_create_sandbox`` takes the two namespaces as two different keywords — ``disk``
    for a prebuilt name, ``disk_id`` for an imported disk image — and the SDK refuses them
    together. Capturing ``**source`` rather than a fixed signature is what lets a test say
    which keyword was used *and* that the other was absent.
    """

    def __init__(self, catalogue: list[str] | None = None, images: list | None = None) -> None:
        self._catalogue = catalogue or []
        self._images = images or []
        self.public_list_calls = 0
        self.source: dict | None = None
        self.create_calls = 0

    def list_public_disk_images(self):
        entries = [_catalogue_entry(name) for name in self._catalogue]
        return _FakePager(entries, on_iter=self._count_public)

    def list_disk_images(self):
        return _FakePager(self._images)

    def get_sandbox_client(self, sandbox_id: str):
        return _FakeSandboxClient(sandbox_id)

    async def begin_create_sandbox(self, *, labels, egress_policy, **source):
        self.create_calls += 1
        self.source = source

        class _Poller:
            async def result(self):
                return _CreatedSandbox("sbx-1")

        return _Poller()

    def _count_public(self):
        self.public_list_calls += 1


class TestNamesAPrebuiltImage:
    """No registry and no tag. The tag is what carries the distinction."""

    def test_a_bare_name_is_one_the_service_provides(self):
        from maf_sandbox_acas._images import names_a_prebuilt_image

        assert names_a_prebuilt_image("python-3.13")
        assert names_a_prebuilt_image("ubuntu")
        assert names_a_prebuilt_image("node-22")

    def test_a_tagged_repository_is_not(self):
        """The trap. `bicep-sandbox:0.46.1` has no registry either, and sample 01 ships it."""
        from maf_sandbox_acas._images import names_a_prebuilt_image

        assert not names_a_prebuilt_image("bicep-sandbox:0.46.1")
        assert not names_a_prebuilt_image("bicep-sandbox:0.46.1-1")

    def test_a_qualified_reference_is_not(self):
        from maf_sandbox_acas._images import names_a_prebuilt_image

        assert not names_a_prebuilt_image("mcr.microsoft.com/devcontainers/python:3.13-bookworm")

    def test_a_repository_path_without_a_tag_is_not(self):
        """A slash means a repository path or a registry, and neither is a catalogue name."""
        from maf_sandbox_acas._images import names_a_prebuilt_image

        assert not names_a_prebuilt_image("library/ubuntu")

    def test_a_digest_reference_is_not(self):
        from maf_sandbox_acas._images import names_a_prebuilt_image

        assert not names_a_prebuilt_image("ubuntu@sha256:0123456789abcdef")

    def test_no_image_at_all_is_not(self):
        from maf_sandbox_acas._images import names_a_prebuilt_image

        assert not names_a_prebuilt_image("")


class TestResolvePrebuiltImageName:
    def setup_method(self):
        from maf_sandbox_acas._images import _prebuilt_name_cache

        _prebuilt_name_cache.clear()

    def test_returns_the_name_the_catalogue_holds(self):
        from maf_sandbox_acas._images import resolve_prebuilt_image_name

        client = _CataloguedGroupClient(catalogue=["ubuntu", "python-3.13"])
        assert asyncio.run(resolve_prebuilt_image_name(client, "python-3.13")) == "python-3.13"

    def test_a_hit_is_cached(self):
        from maf_sandbox_acas._images import resolve_prebuilt_image_name

        client = _CataloguedGroupClient(catalogue=["python-3.13"])
        asyncio.run(resolve_prebuilt_image_name(client, "python-3.13"))
        asyncio.run(resolve_prebuilt_image_name(client, "python-3.13"))
        assert client.public_list_calls == 1

    def test_a_miss_is_not_cached(self):
        """An image the service adds later must be found, not refused for the process's life."""
        from maf_sandbox_acas._images import resolve_prebuilt_image_name

        client = _CataloguedGroupClient(catalogue=[])
        with pytest.raises(ValueError):
            asyncio.run(resolve_prebuilt_image_name(client, "python-3.14"))
        client._catalogue = ["python-3.14"]
        assert asyncio.run(resolve_prebuilt_image_name(client, "python-3.14")) == "python-3.14"
        assert client.public_list_calls == 2

    def test_refuses_an_unknown_name_and_says_what_there_is(self):
        from maf_sandbox_acas._images import resolve_prebuilt_image_name

        client = _CataloguedGroupClient(catalogue=["python-3.13", "ubuntu"])
        with pytest.raises(ValueError, match="provides no image named 'bicep-sandbox'") as raised:
            asyncio.run(resolve_prebuilt_image_name(client, "bicep-sandbox"))
        # The catalogue, so a forgotten tag is diagnosable from the message alone.
        assert "python-3.13, ubuntu" in str(raised.value)
        assert "repository:tag" in str(raised.value)

    def test_says_so_rather_than_listing_nothing(self):
        from maf_sandbox_acas._images import resolve_prebuilt_image_name

        client = _CataloguedGroupClient(catalogue=[])
        with pytest.raises(ValueError, match="catalogue is empty"):
            asyncio.run(resolve_prebuilt_image_name(client, "python-3.13"))

    def test_a_nameless_entry_does_not_become_a_blank_in_the_offer(self):
        """Otherwise the catalogue reads `It provides: , python-3.13` and looks corrupt."""
        from maf_sandbox_acas._images import resolve_prebuilt_image_name

        client = _CataloguedGroupClient(catalogue=["", "python-3.13"])
        with pytest.raises(ValueError) as raised:
            asyncio.run(resolve_prebuilt_image_name(client, "node-22"))
        assert "It provides: python-3.13." in str(raised.value)


class TestWhichNamespaceASpecBootsFrom:
    """`acquire` reads `image` and picks a namespace; these pin which keyword it creates with."""

    def setup_method(self):
        from maf_sandbox_acas._images import _disk_image_cache, _prebuilt_name_cache

        _disk_image_cache.clear()
        _prebuilt_name_cache.clear()

    @staticmethod
    def _key():
        return SandboxKey(scope="scope-a", thread_id="thread-1", agent_dir="devops-engineer")

    def test_a_bare_name_boots_from_the_catalogue(self):
        from maf_sandbox import SandboxSpec

        client = _CataloguedGroupClient(catalogue=["python-3.13"])
        backend = _backend_with(client)
        asyncio.run(backend.acquire(self._key(), SandboxSpec(kind="codeact", image="python-3.13")))
        assert client.source == {"disk": "python-3.13"}

    def test_a_tagged_reference_still_boots_from_an_imported_disk_image(self):
        """Byte-for-byte the old path: qualified by the registry, resolved to an id."""
        from maf_sandbox import SandboxSpec

        client = _CataloguedGroupClient(
            images=[_disk_image("img-1", "acr.azurecr.io/bicep-sandbox:0.46.1")]
        )
        backend = _backend_with(client, _config(registry="acr.azurecr.io"))
        asyncio.run(
            backend.acquire(self._key(), SandboxSpec(kind="bicep", image="bicep-sandbox:0.46.1"))
        )
        assert client.source == {"disk_id": "img-1"}
        assert client.public_list_calls == 0

    def test_a_pinned_id_skips_the_catalogue_too(self):
        """`image_id` promises resolution is skipped, and the catalogue is resolution."""
        from maf_sandbox import SandboxSpec

        client = _CataloguedGroupClient(catalogue=["python-3.13"])
        backend = _backend_with(client)
        asyncio.run(
            backend.acquire(
                self._key(),
                SandboxSpec(kind="codeact", image="python-3.13", image_id="pinned-id"),
            )
        )
        assert client.source == {"disk_id": "pinned-id"}
        assert client.public_list_calls == 0

    def test_the_created_log_names_the_image_it_booted(self, caplog):
        """An operator reading this line is usually asking which namespace it came from."""
        from maf_sandbox import SandboxSpec

        client = _CataloguedGroupClient(catalogue=["python-3.13"])
        backend = _backend_with(client)
        with caplog.at_level(logging.INFO, logger="maf_sandbox_acas"):
            asyncio.run(
                backend.acquire(self._key(), SandboxSpec(kind="codeact", image="python-3.13"))
            )
        created = [r for r in caplog.records if "sandbox created" in r.getMessage()]
        assert len(created) == 1
        assert "disk_image=python-3.13" in created[0].getMessage()

    def test_an_unknown_bare_name_refuses_before_anything_is_created(self):
        """A billable sandbox must not exist by the time the name turns out to be wrong."""
        from maf_sandbox import SandboxSpec

        client = _CataloguedGroupClient(catalogue=["python-3.13"])
        backend = _backend_with(client)
        with pytest.raises(ValueError, match="provides no image named 'bicep-sandbox'"):
            asyncio.run(
                backend.acquire(self._key(), SandboxSpec(kind="bicep", image="bicep-sandbox"))
            )
        assert client.create_calls == 0
        assert backend._registry == {}


# ---------------------------------------------------------------------------
# The non-root gate — what an image whose guest cannot write is refused
# ---------------------------------------------------------------------------


class _GuestAnswer:
    """One `exec` result, in the shape the backend reads the SDK's return value for."""

    def __init__(self, stdout: str = "", stderr: str = "", exit_code: int = 0) -> None:
        self.removes = False
        self.stdout = stdout
        self.stderr = stderr
        self.exit_code = exit_code


class _GuestSandboxClient(_FakeSandboxClient):
    """A file plane and a guest whose removal permission is independent of its stdout."""

    def __init__(self, sandbox_id: str, answer, owner) -> None:
        super().__init__(sandbox_id)
        self._answer = answer
        self._owner = owner
        self.execs: list[tuple[str, str]] = []
        self._sbx_path = "/sandboxes/" + sandbox_id
        self._api_version = "test"
        self.files = owner.files.setdefault(sandbox_id, {})
        self.cleanups = owner.cleanups

    async def mkdir(self, path):
        self._owner.created_directories.append(path)
        self.files[path] = None

    async def write_file(self, path, content, *, create_dirs):
        assert create_dirs
        self.files[posixpath.dirname(path)] = None
        self.files[path] = content

    async def _dp_get(self, route, *, params):
        from azure.core.exceptions import ResourceNotFoundError

        path = params["path"]
        if path == "/":
            return {"isDir": True, "isSymlink": False}
        if route.endswith("/list"):
            return {
                "path": path,
                "entries": [
                    {
                        "path": child,
                        "name": posixpath.basename(child),
                        "isDir": value is None,
                        "isSymlink": False,
                        "size": len(value or b""),
                    }
                    for child, value in self.files.items()
                    if posixpath.dirname(child) == path
                ],
            }
        if path not in self.files:
            raise ResourceNotFoundError("missing")
        return {
            "isDir": self.files[path] is None,
            "isSymlink": False,
            "size": len(self.files[path] or b""),
        }

    async def read_file(self, path):
        return self.files[path]

    async def delete_file(self, path, *, recursive):
        assert recursive
        self.cleanups.append(path)
        for entry in list(self.files):
            if entry == path or entry.startswith(path + "/"):
                del self.files[entry]

    async def begin_delete(self) -> _CompletedDeletion:
        poller = await super().begin_delete()
        if self._owner.delete_fails:
            raise RuntimeError("the principal may not delete this sandbox")
        self._owner.deleted.append(self.sandbox_id)
        return poller

    async def set_lifecycle_policy(self, policy) -> None:
        return None

    async def exec(self, command: str, *, working_directory: str):
        if shlex.split(command)[:2] == ["sh", "-c"]:
            return SimpleNamespace(exit_code=0, stdout="", stderr="")
        self.execs.append((command, working_directory))
        if isinstance(self._answer, Exception):
            raise self._answer
        if self._answer.removes:
            self.files.pop(shlex.split(command)[-1], None)
        return self._answer


class _GuestGroupClient:
    """Hands out sandboxes that answer the probe, on the create path and the reuse path alike."""

    def list_sandboxes(self, *, labels=None):
        return _FakePager([])

    def __init__(self, answer, delete_fails: bool = False) -> None:
        self._answer = answer
        self.delete_fails = delete_fails
        self.create_calls = 0
        self.deleted: list[str] = []
        self.clients: list[_GuestSandboxClient] = []
        self.files: dict[str, dict[str, bytes | None]] = {}
        self.cleanups: list[str] = []
        self.created_directories: list[str] = []

    def get_sandbox_client(self, sandbox_id: str) -> _GuestSandboxClient:
        return self._client(sandbox_id)

    async def begin_create_sandbox(self, *, labels, egress_policy, **source):
        self.create_calls += 1
        created = self._client(f"sbx-{self.create_calls}")

        class _Poller:
            async def result(self):
                return created

        return _Poller()

    def _client(self, sandbox_id: str) -> _GuestSandboxClient:
        client = _GuestSandboxClient(sandbox_id, self._answer, self)
        self.clients.append(client)
        return client

    @property
    def probes(self) -> list[tuple[str, str]]:
        """Every command every sandbox this client handed out was asked to run."""
        return [ran for client in self.clients for ran in client.execs]


def _guest_removing(allowed: bool) -> _GuestAnswer:
    answer = _GuestAnswer(exit_code=0 if allowed else 1)
    answer.removes = allowed
    return answer


def _spec_requiring(*capabilities):
    """A spec on a non-root-looking image, requiring exactly what a test is about.

    Both image fields, because the memo is keyed on the pair: `image_id` skips resolution, so
    two specs sharing an `image` can still boot different artefacts.
    """
    from maf_sandbox import SandboxSpec

    return SandboxSpec(
        kind="codeact",
        image="python-nonroot:3.13",
        image_id="pinned-id",
        requires=frozenset(capabilities),
    )


class TestImageCommandProbes:
    @pytest.mark.parametrize("warm", [False, True])
    def test_missing_shell_is_refused_even_when_removal_works(self, monkeypatch, warm):
        original = _GuestSandboxClient.exec
        shell_status = [0]

        async def exec_command(client, command, *, working_directory):
            if shlex.split(command)[:2] == ["sh", "-c"]:
                return SimpleNamespace(exit_code=shell_status[0], stdout="", stderr="")
            return await original(client, command, working_directory=working_directory)

        monkeypatch.setattr(_GuestSandboxClient, "exec", exec_command)
        client = _GuestGroupClient(_guest_removing(True))
        backend = _backend_with(client)
        key = SandboxKey(scope="command-probes", thread_id="thread", agent_dir="agent")

        async def scenario():
            if warm:
                await backend.acquire(key, _spec_requiring(Capability.FILES_IN))
            shell_status[0] = 127
            with pytest.raises(SandboxCapabilityNotSupported, match="exec.*sh"):
                await backend.acquire(key, _spec_requiring(Capability.EXEC))
            assert bool(backend._registry) is warm
            assert bool(client.deleted) is (not warm)

        asyncio.run(scenario())

    def test_successful_command_probe_is_kept_with_its_sandbox(self, monkeypatch):
        original = _GuestSandboxClient.exec
        shells = []

        async def exec_command(client, command, *, working_directory):
            if shlex.split(command)[:2] == ["sh", "-c"]:
                shells.append(client.sandbox_id)
            return await original(client, command, working_directory=working_directory)

        monkeypatch.setattr(_GuestSandboxClient, "exec", exec_command)
        client = _GuestGroupClient(_guest_removing(True))
        backend = _backend_with(client)
        key = SandboxKey(scope="command-probes", thread_id="thread", agent_dir="agent")

        async def scenario():
            spec = _spec_requiring(Capability.EXEC)
            first = await backend.acquire(key, spec)
            await backend.acquire(key, spec)
            assert shells == [first.instance_id, first.instance_id]
            await backend.dispose(key)
            second = await backend.acquire(key, spec)
            assert second.instance_id != first.instance_id
            assert shells == [
                first.instance_id,
                first.instance_id,
                second.instance_id,
                second.instance_id,
            ]

        asyncio.run(scenario())


class TestAnImageWhoseGuestIsNotRoot:
    """Acquisition checks guest removal compatibility before serving writing workloads."""

    @staticmethod
    def _key(scope: str = "scope-a") -> SandboxKey:
        return SandboxKey(scope=scope, thread_id="thread-1", agent_dir="devops-engineer")

    @pytest.mark.parametrize(
        "capability,answer",
        [
            (Capability.FILES_OUT, _guest_removing(False)),
            (Capability.HOST_TOOLS, _guest_removing(False)),
            (Capability.FILES_DELETE, _guest_removing(False)),
            (Capability.FILES_DELETE, _GuestAnswer(exit_code=127)),
        ],
    )
    def test_a_repaired_catalogue_image_recovers_when_the_hint_expires(
        self, monkeypatch, capability, answer
    ):
        from maf_sandbox import SandboxCapabilityNotSupported

        now = 100.0
        monkeypatch.setattr("maf_sandbox_acas._backend.monotonic", lambda: now)

        async def resolve(gc, name):
            return name

        monkeypatch.setattr("maf_sandbox_acas._backend.resolve_prebuilt_image_name", resolve)
        client = _GuestGroupClient(answer)
        backend = _backend_with(client)
        spec = SandboxSpec(kind="codeact", image="python-3.13", requires=frozenset({capability}))
        with pytest.raises(SandboxCapabilityNotSupported):
            asyncio.run(backend.acquire(self._key(), spec))
        assert client.deleted == ["sbx-1"]
        assert backend._registry == {}

        client._answer = _guest_removing(True)
        for offset in range(1, 60):
            now = 100.0 + offset
            with pytest.raises(SandboxCapabilityNotSupported):
                asyncio.run(backend.acquire(self._key(f"scope-{offset}"), spec))
        assert client.create_calls == len(client.probes) == 1

        now = 160.0
        sandbox = asyncio.run(backend.acquire(self._key(), spec))
        assert sandbox.sandbox_id == "sbx-2"
        assert client.create_calls == len(client.probes) == 2
        assert client.deleted == ["sbx-1"]

    def test_expiry_never_overrides_a_warm_sandbox_verdict(self, monkeypatch):
        from maf_sandbox import SandboxCapabilityNotSupported

        now = 100.0
        monkeypatch.setattr("maf_sandbox_acas._backend.monotonic", lambda: now)
        client = _GuestGroupClient(_guest_removing(False))
        backend = _backend_with(client)
        asyncio.run(backend.acquire(self._key(), _spec_requiring(Capability.EXEC)))

        now = 160.0
        client._answer = _guest_removing(True)
        with pytest.raises(SandboxCapabilityNotSupported, match="Dispose it before acquiring"):
            asyncio.run(backend.acquire(self._key(), _spec_requiring(Capability.FILES_DELETE)))
        assert client.create_calls == len(client.probes) == 1

        asyncio.run(backend.dispose(self._key()))
        assert asyncio.run(backend.acquire(self._key(), _spec_requiring(Capability.FILES_DELETE)))
        assert client.create_calls == len(client.probes) == 2

    def test_an_unchanged_image_renews_its_refusal_only_after_a_fresh_probe(self, monkeypatch):
        from maf_sandbox import SandboxCapabilityNotSupported

        now = 100.0
        monkeypatch.setattr("maf_sandbox_acas._backend.monotonic", lambda: now)
        client = _GuestGroupClient(_guest_removing(False))
        backend = _backend_with(client)
        spec = _spec_requiring(Capability.FILES_DELETE)

        for probe_time in (100.0, 159.0, 160.0, 219.0):
            now = probe_time
            with pytest.raises(SandboxCapabilityNotSupported):
                asyncio.run(backend.acquire(self._key(), spec))
        assert client.create_calls == len(client.probes) == 2
        assert client.deleted == ["sbx-1", "sbx-2"]
        assert backend._registry == {}

    def test_a_warm_unprobed_sandbox_does_not_name_another_sandbox_hint_as_an_obstacle(self):
        from maf_sandbox import SandboxCapabilityNotSupported

        client = _GuestGroupClient(RuntimeError("transport dropped"))
        backend = _backend_with(client)
        execing = _spec_requiring(Capability.EXEC)
        asyncio.run(backend.acquire(self._key(), execing))
        client._answer = _guest_removing(False)
        asyncio.run(backend.acquire(self._key("other"), execing))

        client._answer = RuntimeError("transport dropped")
        deleting = _spec_requiring(Capability.FILES_DELETE)
        with pytest.raises(SandboxCapabilityNotSupported) as refusal:
            asyncio.run(backend.acquire(self._key(), deleting))
        assert "next acquire probes this warm sandbox again" in str(refusal.value)
        assert "expires in" not in str(refusal.value)

        client._answer = _guest_removing(True)
        assert asyncio.run(backend.acquire(self._key(), deleting)).sandbox_id == "sbx-1"
        assert client.create_calls == 2
        assert len(client.probes) == 4

    @pytest.mark.parametrize("completed", [False, True])
    def test_only_a_completed_probe_replaces_the_hint_deadline(self, monkeypatch, completed):
        from maf_sandbox import SandboxCapabilityNotSupported

        now = 100.0
        monkeypatch.setattr("maf_sandbox_acas._backend.monotonic", lambda: now)
        client = _GuestGroupClient(_guest_removing(False))
        backend = _backend_with(client)
        deleting = _spec_requiring(Capability.FILES_DELETE)
        with pytest.raises(SandboxCapabilityNotSupported):
            asyncio.run(backend.acquire(self._key(), deleting))

        now = 159.0
        client._answer = (
            _GuestAnswer(exit_code=127) if completed else RuntimeError("transport dropped")
        )
        asyncio.run(backend.acquire(self._key("exec-only"), _spec_requiring(Capability.EXEC)))
        now = 160.0
        if completed:
            with pytest.raises(SandboxCapabilityNotSupported):
                asyncio.run(backend.acquire(self._key(), deleting))
            assert client.create_calls == 2
            now = 219.0
        client._answer = _guest_removing(True)
        assert asyncio.run(backend.acquire(self._key(), deleting))
        assert client.create_calls == len(client.probes) == 3

    def test_an_expired_concrete_hint_can_be_replaced_by_an_inconclusive_one(self, monkeypatch):
        from maf_sandbox import SandboxCapabilityNotSupported

        now = 100.0
        monkeypatch.setattr("maf_sandbox_acas._backend.monotonic", lambda: now)
        client = _GuestGroupClient(_guest_removing(False))
        backend = _backend_with(client)
        deleting = _spec_requiring(Capability.FILES_DELETE)
        with pytest.raises(SandboxCapabilityNotSupported):
            asyncio.run(backend.acquire(self._key(), deleting))

        now = 160.0
        client._answer = _GuestAnswer(exit_code=127)
        for probe_time in (160.0, 219.0):
            now = probe_time
            with pytest.raises(SandboxCapabilityNotSupported):
                asyncio.run(backend.acquire(self._key(), deleting))
        assert client.create_calls == len(client.probes) == 2

        now = 220.0
        client._answer = _guest_removing(True)
        assert asyncio.run(backend.acquire(self._key(), deleting))
        assert client.create_calls == len(client.probes) == 3

    @pytest.mark.parametrize("exit_code", [0, 1, 127])
    def test_announcing_root_without_removing_the_file_never_enables_delete(self, exit_code):
        from maf_sandbox import SandboxCapabilityNotSupported

        client = _GuestGroupClient(_GuestAnswer(stdout="0\n", exit_code=exit_code))
        backend = _backend_with(client)

        with pytest.raises(SandboxCapabilityNotSupported, match="files_delete"):
            asyncio.run(backend.acquire(self._key(), _spec_requiring(Capability.FILES_DELETE)))

        assert all("id -u" not in command for command, _ in client.probes)
        assert len(client.cleanups) == 1
        assert all(
            set(files) <= {"/maf-sandbox", "/maf-sandbox/work"} for files in client.files.values()
        )
        assert client.deleted == ["sbx-1"]

    @pytest.mark.parametrize("exit_code", [0, 1])
    def test_a_probe_only_privileged_wrapper_never_unlocks_a_host_delete(
        self, monkeypatch, exit_code
    ):
        original = _GuestSandboxClient.exec

        async def selective_rm(sc, command, *, working_directory):
            if command.startswith("rm -- /.maf-authority-"):
                return await original(sc, command, working_directory=working_directory)
            return _GuestAnswer(exit_code=exit_code)

        monkeypatch.setattr(_GuestSandboxClient, "exec", selective_rm)
        client = _GuestGroupClient(_guest_removing(True))
        backend = _backend_with(client)
        sandbox = asyncio.run(
            backend.acquire(self._key(), _spec_requiring(Capability.FILES_DELETE))
        )
        guest_file = "/work/protected/file"
        asyncio.run(sandbox.write_file(guest_file, "keep", working_directory="/"))

        with pytest.raises(OSError):
            asyncio.run(sandbox.remove("protected", working_directory="/work", recursive=True))

        assert asyncio.run(sandbox.stat_file(guest_file, working_directory="/")) is not None
        assert all(path.startswith("/.maf-authority-") for path in client.cleanups)

    def test_a_removal_is_observed_independently_of_stdout(self):
        answer = _guest_removing(True)
        answer.stdout = "this is not a uid"
        client = _GuestGroupClient(answer)
        backend = _backend_with(client)

        assert asyncio.run(backend.acquire(self._key(), _spec_requiring(Capability.FILES_DELETE)))
        assert backend._guest_removals[("pinned-id", "python-nonroot:3.13")].removal is True
        assert len(client.cleanups) == 1
        assert all(
            set(files) <= {"/maf-sandbox", "/maf-sandbox/work"} for files in client.files.values()
        )

    def test_a_missing_scratch_directory_is_not_evidence_of_a_guest_removal(self, monkeypatch):
        from maf_sandbox import SandboxCapabilityNotSupported

        async def lose_directory(sc, command, *, working_directory):
            sc.files.clear()
            return _GuestAnswer()

        monkeypatch.setattr(_GuestSandboxClient, "exec", lose_directory)
        backend = _backend_with(_GuestGroupClient(_guest_removing(True)))

        with pytest.raises(SandboxCapabilityNotSupported, match="files_delete"):
            asyncio.run(backend.acquire(self._key(), _spec_requiring(Capability.FILES_DELETE)))

    @pytest.mark.parametrize("phase", ["write_file", "_dp_get", "exec", "delete_file"])
    @pytest.mark.parametrize("hang", [False, True])
    def test_an_incomplete_probe_refuses_delete_and_is_retried(self, monkeypatch, phase, hang):
        from maf_sandbox import SandboxCapabilityNotSupported

        async def fail(sc, *args, **kwargs):
            if hang:
                await asyncio.Event().wait()
            raise OSError("probe request failed")

        monkeypatch.setattr("maf_sandbox_acas._backend._PROBE_TIMEOUT_S", 0.01)
        monkeypatch.setattr(_GuestSandboxClient, phase, fail)
        client = _GuestGroupClient(_guest_removing(True))
        backend = _backend_with(client)

        for _ in range(2):
            with pytest.raises(SandboxCapabilityNotSupported, match="files_delete"):
                asyncio.run(backend.acquire(self._key(), _spec_requiring(Capability.FILES_DELETE)))
        assert backend._guest_removals == {}
        assert backend._registry == {}
        assert client.create_calls == 2
        if phase != "delete_file":
            assert len(client.cleanups) == 2
            assert all(
                set(files) <= {"/maf-sandbox", "/maf-sandbox/work"}
                for files in client.files.values()
            )

    @pytest.mark.parametrize("cleanup_fails", [False, True])
    def test_cancellation_attempts_cleanup_and_never_records_a_verdict(
        self, monkeypatch, cleanup_fails
    ):
        async def cancel(sc, command, *, working_directory):
            raise asyncio.CancelledError

        async def fail_cleanup(sc, path, *, recursive):
            sc.cleanups.append(path)
            raise OSError("cleanup failed")

        monkeypatch.setattr(_GuestSandboxClient, "exec", cancel)
        if cleanup_fails:
            monkeypatch.setattr(_GuestSandboxClient, "delete_file", fail_cleanup)
        client = _GuestGroupClient(_guest_removing(True))
        backend = _backend_with(client)

        with pytest.raises(asyncio.CancelledError):
            asyncio.run(backend.acquire(self._key(), _spec_requiring(Capability.FILES_DELETE)))

        assert len(client.cleanups) == 1
        assert backend._guest_removals == {}
        assert not next(iter(backend._registry.values())).probed

    def test_a_workload_collecting_outputs_is_refused(self):
        from maf_sandbox import SandboxCapabilityNotSupported

        client = _GuestGroupClient(_guest_removing(False))
        backend = _backend_with(client)

        with pytest.raises(SandboxCapabilityNotSupported) as refusal:
            asyncio.run(
                backend.acquire(self._key(), _spec_requiring(Capability.EXEC, Capability.FILES_OUT))
            )

        message = str(refusal.value)
        assert "files_out" in message
        assert "could not remove the file plane's probe file" in message
        assert "python-nonroot:3.13" in message
        assert "USER is root" in message

    def test_a_refusal_names_the_artefact_that_booted(self):
        """`image_id` wins at create, so a message naming `image` alone sends an operator to
        an artefact that was never run."""
        from maf_sandbox import SandboxCapabilityNotSupported

        client = _GuestGroupClient(_guest_removing(False))
        backend = _backend_with(client)

        with pytest.raises(SandboxCapabilityNotSupported) as refusal:
            asyncio.run(
                backend.acquire(self._key(), _spec_requiring(Capability.EXEC, Capability.FILES_OUT))
            )

        assert "pinned-id" in str(refusal.value), str(refusal.value)

    def test_only_the_configured_field_is_named_when_one_is_set(self):
        """The common shape: no id pinned, so there is nothing to prefer over the reference."""
        from maf_sandbox import SandboxSpec

        from maf_sandbox_acas._backend import _image_label

        spec = SandboxSpec(kind="codeact", image="python-nonroot:3.13")

        assert _image_label(spec) == "python-nonroot:3.13"
        assert _image_label(SandboxSpec(kind="codeact", image_id="only-an-id")) == "only-an-id"

    def test_host_tools_are_refused_on_their_own(self):
        """The transport's launcher writes its markers into a directory the file plane made,
        so the pair is refused together even where a kind asks for only one of them."""
        from maf_sandbox import SandboxCapabilityNotSupported

        client = _GuestGroupClient(_guest_removing(False))
        backend = _backend_with(client)

        with pytest.raises(SandboxCapabilityNotSupported, match="host_tools"):
            asyncio.run(
                backend.acquire(
                    self._key(), _spec_requiring(Capability.EXEC, Capability.HOST_TOOLS)
                )
            )

    def test_deleting_requires_observed_removal_compatibility(self):
        """Deletion requires the guest command to have removed a file-plane entry."""
        from maf_sandbox import SandboxCapabilityNotSupported

        client = _GuestGroupClient(_guest_removing(False))
        backend = _backend_with(client)

        with pytest.raises(SandboxCapabilityNotSupported) as refusal:
            asyncio.run(
                backend.acquire(
                    self._key(), _spec_requiring(Capability.EXEC, Capability.FILES_DELETE)
                )
            )

        message = str(refusal.value)
        assert "files_delete" in message
        assert "could not remove the file plane's probe file" in message
        assert "Each requested removal runs as the guest" in message, message
        assert "did not demonstrate removal" in message, message
        # The other reason is about creating files, and this spec asks for nothing that needs it.
        assert "Permission denied" not in message, message

    def test_deleting_alone_is_enough_to_make_the_removal_worth_reading(self):
        """The probe gate and the refusal set are two constants, and a capability added to one
        and not the other is refused by nothing at all."""
        from maf_sandbox import SandboxCapabilityNotSupported

        client = _GuestGroupClient(_guest_removing(False))
        backend = _backend_with(client)

        with pytest.raises(SandboxCapabilityNotSupported, match="files_delete"):
            asyncio.run(backend.acquire(self._key(), _spec_requiring(Capability.FILES_DELETE)))

        assert len(client.probes) == 1 and client.probes[0][0].startswith(
            "rm -- /.maf-authority-"
        ), "a spec requiring only FILES_DELETE never probed"

    def test_both_reasons_are_named_when_a_spec_asks_for_both(self):
        """One message rather than two acquires: a caller that drops `FILES_OUT` on the strength
        of the first refusal would otherwise meet the second on the next call."""
        from maf_sandbox import SandboxCapabilityNotSupported

        client = _GuestGroupClient(_guest_removing(False))
        backend = _backend_with(client)

        with pytest.raises(SandboxCapabilityNotSupported) as refusal:
            asyncio.run(
                backend.acquire(
                    self._key(),
                    _spec_requiring(Capability.EXEC, Capability.FILES_OUT, Capability.FILES_DELETE),
                )
            )

        message = str(refusal.value)
        assert "files_delete" in message and "files_out" in message, message
        assert "Permission denied" in message, message
        assert "did not demonstrate removal" in message, message

    def test_a_root_guest_is_served(self):
        client = _GuestGroupClient(_guest_removing(True))
        backend = _backend_with(client)

        sandbox = asyncio.run(
            backend.acquire(self._key(), _spec_requiring(Capability.EXEC, Capability.FILES_OUT))
        )

        assert sandbox.sandbox_id == "sbx-1"
        assert len(client.probes) == 1 and client.probes[0][0].startswith("rm -- /.maf-authority-")

    def test_a_root_guest_is_still_served_a_delete(self):
        """A guest that passes the removal check is served."""
        client = _GuestGroupClient(_guest_removing(True))
        backend = _backend_with(client)

        sandbox = asyncio.run(
            backend.acquire(self._key(), _spec_requiring(Capability.EXEC, Capability.FILES_DELETE))
        )

        assert sandbox.sandbox_id == "sbx-1"

    def test_the_probe_never_runs_in_the_working_directory(self):
        """Nothing has created `work_dir` at acquire, so a probe run there would fail for a
        reason that has nothing to do with the removal."""
        client = _GuestGroupClient(_guest_removing(True))
        backend = _backend_with(client)

        asyncio.run(backend.acquire(self._key(), _spec_requiring(Capability.EXEC)))

        assert [directory for _, directory in client.probes] == ["/"]

    def test_a_workload_that_only_execs_is_served_with_a_warning(self, caplog):
        client = _GuestGroupClient(_guest_removing(False))
        backend = _backend_with(client)

        with caplog.at_level(logging.WARNING, logger="maf_sandbox_acas"):
            sandbox = asyncio.run(backend.acquire(self._key(), _spec_requiring(Capability.EXEC)))

        assert sandbox.sandbox_id == "sbx-1"
        warned = [
            r.getMessage()
            for r in caplog.records
            if "could not remove the file plane's probe file" in r.getMessage()
        ]
        assert len(warned) == 1, caplog.text
        assert "write_file" in warned[0]

    def test_the_warning_is_said_once_rather_than_on_every_call(self, caplog):
        """`acquire` runs on every tool call, so a warning per acquire would be noise."""
        client = _GuestGroupClient(_guest_removing(False))
        backend = _backend_with(client)
        spec = _spec_requiring(Capability.EXEC)

        with caplog.at_level(logging.WARNING, logger="maf_sandbox_acas"):
            asyncio.run(backend.acquire(self._key("scope-a"), spec))
            asyncio.run(backend.acquire(self._key("scope-b"), spec))

        assert (
            len(
                [
                    r
                    for r in caplog.records
                    if "could not remove the file plane's probe file" in r.getMessage()
                ]
            )
            == 1
        )

    def test_a_root_guest_execing_is_not_warned_about(self, caplog):
        client = _GuestGroupClient(_guest_removing(True))
        backend = _backend_with(client)

        with caplog.at_level(logging.WARNING, logger="maf_sandbox_acas"):
            asyncio.run(backend.acquire(self._key(), _spec_requiring(Capability.EXEC)))

        assert caplog.records == []

    def test_a_guest_that_cannot_be_asked_is_served(self):
        """Fails open, and deliberately: refusing on an unreadable probe would take a working
        root image off a deployment, where serving it costs no more than today's failure."""
        client = _GuestGroupClient(RuntimeError("no shell in this image"))
        backend = _backend_with(client)

        sandbox = asyncio.run(
            backend.acquire(self._key(), _spec_requiring(Capability.EXEC, Capability.FILES_OUT))
        )

        assert sandbox.sandbox_id == "sbx-1"

    def test_each_branch_names_the_remedy_that_would_actually_lift_it(self):
        """An inconclusive probe may need working commands or a reachable file plane.
        Setting USER alone does not fix either."""
        from maf_sandbox import SandboxCapabilityNotSupported

        spec = _spec_requiring(Capability.EXEC, Capability.FILES_DELETE)

        unread_backend = _backend_with(_GuestGroupClient(RuntimeError("no rm in this image")))
        known_backend = _backend_with(_GuestGroupClient(_guest_removing(False)))

        with pytest.raises(SandboxCapabilityNotSupported) as unread:
            asyncio.run(unread_backend.acquire(self._key(), spec))
        with pytest.raises(SandboxCapabilityNotSupported) as known:
            asyncio.run(known_backend.acquire(self._key(), spec))

        unread_message, known_message = str(unread.value), str(known.value)
        # The unread branch must not send an operator to the Dockerfile: that is the fix that
        # looks right, changes the removal nothing can read, and is refused identically.
        assert "file plane confirms the removal" in unread_message, unread_message
        assert "a root USER alone does not" in unread_message, unread_message
        # The known branch keeps the remedy that does work there.
        assert "an image whose USER is root" in known_message, known_message
        assert "id -u" not in known_message, known_message

    def test_a_guest_that_cannot_be_asked_is_still_refused_a_delete(self):
        """Deletion keeps its conservative refusal on an unreadable compatibility probe."""
        from maf_sandbox import SandboxCapabilityNotSupported

        client = _GuestGroupClient(RuntimeError("no shell in this image"))
        backend = _backend_with(client)

        with pytest.raises(SandboxCapabilityNotSupported) as refusal:
            asyncio.run(
                backend.acquire(
                    self._key(), _spec_requiring(Capability.EXEC, Capability.FILES_DELETE)
                )
            )

        message = str(refusal.value)
        assert "files_delete" in message
        assert "removal probe was inconclusive" in message, message
        # It has no removal to name, and naming `None` as one would send a reader looking for it.
        assert "removal None" not in message, message

    def test_an_unreadable_probe_refuses_only_deletion(self):
        """One refusal names both sets, and an unknown removal still splits them: the spec keeps
        `FILES_OUT`, which is served on no evidence, and loses only `FILES_DELETE`."""
        from maf_sandbox import SandboxCapabilityNotSupported

        client = _GuestGroupClient(RuntimeError("no shell in this image"))
        backend = _backend_with(client)

        with pytest.raises(SandboxCapabilityNotSupported) as refusal:
            asyncio.run(
                backend.acquire(
                    self._key(),
                    _spec_requiring(Capability.EXEC, Capability.FILES_OUT, Capability.FILES_DELETE),
                )
            )

        message = str(refusal.value)
        assert "files_delete" in message
        assert "files_out" not in message, message
        assert "Permission denied" not in message, message

    def test_an_unreadable_probe_warns_about_nothing(self, caplog):
        """The warning describes a wall an unread image may not have, and one issued on every
        unreadable probe would train a reader to ignore it."""
        client = _GuestGroupClient(RuntimeError("no shell in this image"))
        backend = _backend_with(client)

        with caplog.at_level(logging.WARNING, logger="maf_sandbox_acas"):
            asyncio.run(backend.acquire(self._key(), _spec_requiring(Capability.EXEC)))

        assert caplog.records == []

    def test_a_failed_command_is_not_read_as_a_removal(self):
        client = _GuestGroupClient(_GuestAnswer(stdout="", stderr="not found", exit_code=127))
        backend = _backend_with(client)

        assert asyncio.run(
            backend.acquire(self._key(), _spec_requiring(Capability.EXEC, Capability.FILES_OUT))
        )

    def test_stdout_without_a_removal_is_inconclusive(self):
        client = _GuestGroupClient(_GuestAnswer(stdout="root\n"))
        backend = _backend_with(client)

        assert asyncio.run(
            backend.acquire(self._key(), _spec_requiring(Capability.EXEC, Capability.FILES_OUT))
        )

    def test_a_completed_inconclusive_probe_is_asked_only_once(self):
        """A guest that answered and said something that is not a removal is a fact about the
        artefact, so it is remembered: re-asking would put a round trip, and its timeout, in
        front of every tool call.
        Measured on the **warm** path, which is where memoising is observable at all: a cold
        acquire re-probes whatever the memo holds, so counting across two creates would pass
        without the memo doing anything.
        """
        client = _GuestGroupClient(_GuestAnswer(stdout="", stderr="not found", exit_code=127))
        backend = _backend_with(client)
        spec = _spec_requiring(Capability.EXEC, Capability.FILES_OUT)

        asyncio.run(backend.acquire(self._key(), spec))  # cold: probes and records
        asyncio.run(backend.acquire(self._key(), spec))  # warm: the memo answers

        assert len(client.probes) == 1
        assert backend._guest_removals[("pinned-id", "python-nonroot:3.13")].removal is None

    def test_a_probe_that_never_landed_is_asked_again(self):
        """A transport failure records no verdict, so a warm acquire retries immediately."""
        client = _GuestGroupClient(RuntimeError("transport dropped"))
        backend = _backend_with(client)
        spec = _spec_requiring(Capability.EXEC, Capability.FILES_OUT)

        asyncio.run(backend.acquire(self._key(), spec))
        asyncio.run(backend.acquire(self._key(), spec))

        assert len(client.probes) == 2, "a dropped probe was remembered as an answer"
        assert backend._guest_removals == {}, "a transient failure recorded a verdict"

    def test_the_removal_is_read_once_per_sandbox_rather_than_once_per_image(self):
        """The memo answers before a create and on a warm reuse; it does not answer *for* a
        create. A second key boots a second artefact from the same reference, and an image
        reference is the service's to repoint, so the memo describes the previous one."""
        client = _GuestGroupClient(_guest_removing(True))
        backend = _backend_with(client)
        spec = _spec_requiring(Capability.EXEC, Capability.FILES_OUT)

        asyncio.run(backend.acquire(self._key("scope-a"), spec))
        asyncio.run(backend.acquire(self._key("scope-a"), spec))  # warm: the memo answers
        asyncio.run(backend.acquire(self._key("scope-b"), spec))  # cold: a new artefact

        assert len(client.probes) == 2, "a create trusted the memo, or a warm reuse re-probed"

    def test_one_sandbox_cannot_license_another_that_shares_its_image_name(self):
        """The verdict belongs to the sandbox, not to the reference it booted from.

        Two keys boot two sandboxes from one mutable name. The second is root and moves the
        image-level hint to `True`; a warm reacquire of the **first**, whose guest is not root,
        must not read that `True` and skip its own removal compatibility check.
        """
        from maf_sandbox import SandboxCapabilityNotSupported

        client = _GuestGroupClient(_guest_removing(False))
        backend = _backend_with(client)
        deleting = _spec_requiring(Capability.EXEC, Capability.FILES_DELETE)
        execing = _spec_requiring(Capability.EXEC)

        # The non-root sandbox, kept warm under its own key.
        asyncio.run(backend.acquire(self._key("scope-a"), execing))
        # A second sandbox from the same name, after the reference was repointed to root. Set
        # on the group, so every client it hands out from here answers `True` — including the one
        # the warm reacquire below builds for the *first* sandbox, which is the trap: reading
        # the answer instead of the recorded verdict would pass this test for the wrong reason.
        client._answer = _guest_removing(True)
        asyncio.run(backend.acquire(self._key("scope-b"), execing))
        assert backend._guest_removals[("pinned-id", "python-nonroot:3.13")].removal is True, (
            "the second sandbox did not move the image-level hint, so this proves nothing"
        )

        # Warm reuse of the first — the guest that is still 10001.
        with pytest.raises(SandboxCapabilityNotSupported, match="files_delete"):
            asyncio.run(backend.acquire(self._key("scope-a"), deleting))

    def _unverified_beside_a_root_sandbox(self, retry_answer):
        """A sandbox whose own probe never landed, kept warm beside a root one on the same
        image name — so the hint says `True` and nothing has ever read *this* guest.

        `retry_answer` is what its warm re-probe meets, which is the branch under test.
        """
        client = _GuestGroupClient(RuntimeError("transport dropped"))
        backend = _backend_with(client)

        # Its create-time probe drops, so it records no verdict of its own.
        asyncio.run(backend.acquire(self._key("scope-a"), _spec_requiring(Capability.EXEC)))
        assert not any(h.probed for h in backend._registry.values()), (
            "the dropped probe recorded a verdict after all"
        )

        # A second sandbox, root, moves the image-level hint.
        client._answer = _guest_removing(True)
        asyncio.run(backend.acquire(self._key("scope-b"), _spec_requiring(Capability.EXEC)))
        assert backend._guest_removals[("pinned-id", "python-nonroot:3.13")].removal is True

        client._answer = retry_answer
        return backend

    def test_an_unverified_sandbox_does_not_inherit_a_root_hint_when_its_retry_drops(self):
        """The fallback must be this sandbox's own verdict or nothing — never the hint, which
        is another guest's answer. Inheriting it serves the delete to a guest nobody read."""
        from maf_sandbox import SandboxCapabilityNotSupported

        backend = self._unverified_beside_a_root_sandbox(RuntimeError("dropped again"))

        with pytest.raises(SandboxCapabilityNotSupported, match="files_delete"):
            asyncio.run(
                backend.acquire(
                    self._key("scope-a"), _spec_requiring(Capability.EXEC, Capability.FILES_DELETE)
                )
            )

    def test_an_unverified_sandbox_does_not_inherit_a_root_hint_on_an_inconclusive_answer(self):
        """The same inheritance down the definitive branch, which additionally *records* the
        borrowed `True` as this sandbox's verdict and would license every acquire after it."""
        from maf_sandbox import SandboxCapabilityNotSupported

        backend = self._unverified_beside_a_root_sandbox(
            _GuestAnswer(stdout="", stderr="not found", exit_code=127)
        )

        with pytest.raises(SandboxCapabilityNotSupported, match="files_delete"):
            asyncio.run(
                backend.acquire(
                    self._key("scope-a"), _spec_requiring(Capability.EXEC, Capability.FILES_DELETE)
                )
            )

        unverified = backend._registry[("scope-a", "thread-1", "devops-engineer", "", "codeact")]
        assert unverified.removal is None, "it recorded another guest's removal"
        assert backend._guest_removals[("pinned-id", "python-nonroot:3.13")].removal is None

    def test_a_verdict_cannot_outlive_the_sandbox_it_describes(self):
        """It lives on the registry entry, so disposal takes it with no sweeping to get wrong.

        Both answer kinds, because they are recorded on different lines: a removal and a definitive
        inconclusive reply. And a probe that lands after its entry is dropped writes to an object
        nobody holds, which is what a map beside the registry could not arrange.
        """
        for answer in (
            _guest_removing(True),
            _GuestAnswer(stdout="", stderr="no rm", exit_code=127),
        ):
            client = _GuestGroupClient(answer)
            backend = _backend_with(client)
            key = self._key("scope-a")

            asyncio.run(backend.acquire(key, _spec_requiring(Capability.EXEC)))
            held = next(iter(backend._registry.values()))
            assert held.probed, f"{answer} recorded no verdict, so this proves nothing"

            asyncio.run(backend.dispose(key))

            assert backend._registry == {}, f"the entry outlived its sandbox for {answer}"
            # The detached entry is what a late probe would write to, and nothing reads it.
            held.removal, held.probed = True, True
            assert backend._registry == {}, "a late write reached the backend"

    def test_a_warm_root_sandbox_is_not_refused_by_another_sandbox_s_hint(self):
        """The hint must not answer for a sandbox that has a verdict of its own.

        A sandbox with a successful removal probe kept warm, then a non-root one from the same
        name, which moves the hint to `False`. The next `FILES_DELETE` acquire for the warm
        root sandbox must read *its* `True`, not the hint — and it only can if the registry is
        consulted before the hint is.
        """
        client = _GuestGroupClient(_guest_removing(True))
        backend = _backend_with(client)

        asyncio.run(backend.acquire(self._key("scope-a"), _spec_requiring(Capability.EXEC)))
        client._answer = _guest_removing(False)
        asyncio.run(backend.acquire(self._key("scope-b"), _spec_requiring(Capability.EXEC)))
        assert backend._guest_removals[("pinned-id", "python-nonroot:3.13")].removal is False, (
            "the non-root sandbox did not move the hint, so this proves nothing"
        )

        # Warm reuse of the root sandbox. Its own verdict clears it; the hint would not.
        warm = asyncio.run(
            backend.acquire(self._key("scope-a"), _spec_requiring(Capability.FILES_DELETE))
        )

        assert warm.sandbox_id == "sbx-1"

    def test_the_refusal_names_the_bounded_retry(self):
        from maf_sandbox import SandboxCapabilityNotSupported

        client = _GuestGroupClient(_guest_removing(False))
        backend = _backend_with(client)

        with pytest.raises(SandboxCapabilityNotSupported) as refusal:
            asyncio.run(backend.acquire(self._key(), _spec_requiring(Capability.FILES_DELETE)))

        message = str(refusal.value)
        assert "cached refusal expires in" in message
        assert "Repeated refusals do not extend the deadline" in message
        assert "no process restart is needed" in message

    def test_a_refused_fresh_sandbox_is_gone_so_the_hint_decides_the_advice(self, monkeypatch):
        """A completed inconclusive result stops repeated creates after a reference changes."""
        from maf_sandbox import SandboxCapabilityNotSupported

        now = 100.0
        monkeypatch.setattr("maf_sandbox_acas._backend.monotonic", lambda: now)
        client = _GuestGroupClient(_guest_removing(True))
        backend = _backend_with(client)
        asyncio.run(backend.acquire(self._key("scope-a"), _spec_requiring(Capability.EXEC)))
        assert backend._guest_removals[("pinned-id", "python-nonroot:3.13")].removal is True

        # A definitive inconclusive answer: the verdict is recorded, then the refusal deletes it.
        client._answer = _GuestAnswer(stdout="", stderr="not found", exit_code=127)
        for scope in ("scope-b", "scope-b", "scope-c"):
            with pytest.raises(SandboxCapabilityNotSupported) as refusal:
                asyncio.run(
                    backend.acquire(
                        self._key(scope), _spec_requiring(Capability.EXEC, Capability.FILES_DELETE)
                    )
                )
            assert "cached refusal expires in" in str(refusal.value)
        assert backend._guest_removals[("pinned-id", "python-nonroot:3.13")] == _RemovalHint(
            None, 160.0, 2
        )
        assert client.create_calls == 2
        assert client.deleted == ["sbx-2"]
        assert len(client.probes) == 2
        assert len(backend._registry) == 1
        warm = asyncio.run(
            backend.acquire(self._key("scope-a"), _spec_requiring(Capability.FILES_DELETE))
        )
        assert warm.sandbox_id == "sbx-1"
        now = 160.0
        client._answer = _guest_removing(True)
        refreshed = asyncio.run(
            backend.acquire(self._key("scope-b"), _spec_requiring(Capability.FILES_DELETE))
        )
        assert refreshed.sandbox_id == "sbx-3"
        assert client.create_calls == 3

    def test_a_permissive_hint_is_not_an_obstacle_the_refusal_should_name(self):
        """Hint *presence* is the wrong test; whether anything will answer next time is right.

        A `True` hint permits the create, that sandbox's probe then drops, and nothing is
        recorded — so the next identical acquire creates and probes again. Advising a new
        reference or a restart there names an obstacle that is not in the way.
        """
        from maf_sandbox import SandboxCapabilityNotSupported

        client = _GuestGroupClient(_guest_removing(True))
        backend = _backend_with(client)
        # A permissive hint, from an acquire that measured root.
        asyncio.run(backend.acquire(self._key("scope-a"), _spec_requiring(Capability.EXEC)))
        assert backend._guest_removals[("pinned-id", "python-nonroot:3.13")].removal is True

        client._answer = RuntimeError("transport dropped")
        with pytest.raises(SandboxCapabilityNotSupported) as refusal:
            asyncio.run(
                backend.acquire(
                    self._key("scope-b"), _spec_requiring(Capability.EXEC, Capability.FILES_DELETE)
                )
            )

        message = str(refusal.value)
        assert "No cached refusal blocks a retry" in message, message
        assert "restart of this process" not in message, message

    def test_an_unknown_removal_does_not_claim_the_guest_lacks_the_reach(self):
        """An unreadable probe is distinguished from a completed removal failure."""
        from maf_sandbox import SandboxCapabilityNotSupported

        client = _GuestGroupClient(RuntimeError("no rm in this image"))
        backend = _backend_with(client)

        with pytest.raises(SandboxCapabilityNotSupported) as unread:
            asyncio.run(
                backend.acquire(
                    self._key(), _spec_requiring(Capability.EXEC, Capability.FILES_DELETE)
                )
            )
        with pytest.raises(SandboxCapabilityNotSupported) as known:
            asyncio.run(
                _backend_with(_GuestGroupClient(_guest_removing(False))).acquire(
                    self._key(), _spec_requiring(Capability.EXEC, Capability.FILES_DELETE)
                )
            )

        assert "removal probe was inconclusive" in str(unread.value), str(unread.value)
        # The completed failure names the observation.
        assert "did not demonstrate removal" in str(known.value), str(known.value)

    def test_the_pre_create_hint_never_warns_about_the_guest(self, caplog):
        """A warning there describes whatever the reference last resolved to, and marks the
        pair warned — silencing the accurate one the post-create probe could have made."""
        client = _GuestGroupClient(_guest_removing(False))
        backend = _backend_with(client)
        execing = _spec_requiring(Capability.EXEC)

        with caplog.at_level(logging.WARNING, logger="maf_sandbox_acas"):
            asyncio.run(backend.acquire(self._key("scope-a"), execing))
        assert caplog.records, "the first acquire warned about nothing, so this proves nothing"

        # A second key reads the hint before any sandbox exists, and the reference now resolves
        # to root. A warning there would be about the old artefact, and would consume the
        # once-per-pair budget the accurate one needs.
        backend._warned_about_the_guest.clear()
        client._answer = _guest_removing(True)
        caplog.clear()

        with caplog.at_level(logging.WARNING, logger="maf_sandbox_acas"):
            asyncio.run(backend.acquire(self._key("scope-b"), execing))

        assert caplog.records == [], f"warned from the hint: {caplog.text}"

    def test_a_refusal_from_a_dropped_probe_says_the_next_acquire_asks_again(self):
        """The recovery advice is only true where something was remembered.

        A probe that never landed records nothing, so an identical acquire re-probes and may
        well succeed. Telling that operator to find a new reference or restart would hide the
        simplest recovery there is — try again.
        """
        from maf_sandbox import SandboxCapabilityNotSupported

        client = _GuestGroupClient(RuntimeError("transport dropped"))
        backend = _backend_with(client)

        with pytest.raises(SandboxCapabilityNotSupported) as refusal:
            asyncio.run(
                backend.acquire(
                    self._key(), _spec_requiring(Capability.EXEC, Capability.FILES_DELETE)
                )
            )

        message = str(refusal.value)
        assert "No cached refusal blocks a retry" in message, message
        assert "asks again" in message, message
        # The blocking advice belongs to the other branch and would be false here.
        assert "restart of this process" not in message, message
        assert backend._guest_removals == {}, "something was remembered after all"

    @pytest.mark.parametrize("seeded", [False, True])
    @pytest.mark.parametrize("measured", [False, True])
    @pytest.mark.parametrize("unknown_finishes_last", [False, True])
    def test_overlapping_cold_probes_preserve_a_measurement(
        self, monkeypatch, seeded, measured, unknown_finishes_last
    ):
        """An overlapping unknown cannot displace even an unchanged measurement."""
        from maf_sandbox import SandboxCapabilityNotSupported

        monkeypatch.setattr("maf_sandbox_acas._backend.monotonic", lambda: 100.0)

        def race():
            client = _GuestGroupClient(_guest_removing(measured))
            backend = _backend_with(client)
            identity = ("pinned-id", "python-nonroot:3.13")
            execing = _spec_requiring(Capability.EXEC)
            if seeded:
                asyncio.run(backend.acquire(self._key("seed"), execing))
            started = [threading.Event(), threading.Event()]
            finish = [threading.Event(), threading.Event()]
            original = _GuestSandboxClient.exec
            first_id = client.create_calls + 1

            async def ordered_exec(sc, command, *, working_directory):
                index = int(sc.sandbox_id.removeprefix("sbx-")) - first_id
                if index in (0, 1):
                    started[index].set()
                    assert finish[index].wait(5)
                return await original(sc, command, working_directory=working_directory)

            monkeypatch.setattr(_GuestSandboxClient, "exec", ordered_exec)
            with ThreadPoolExecutor(max_workers=2) as pool:
                client._answer = _GuestAnswer(exit_code=127)
                unknown = pool.submit(asyncio.run, backend.acquire(self._key("unknown"), execing))
                assert started[0].wait(5)
                client._answer = _guest_removing(measured)
                measurement = pool.submit(
                    asyncio.run, backend.acquire(self._key("measured"), execing)
                )
                assert started[1].wait(5)

                order = (1, 0) if unknown_finishes_last else (0, 1)
                for index in order:
                    finish[index].set()
                    (unknown, measurement)[index].result(timeout=5)

            assert backend._guest_removals[identity] == _RemovalHint(
                measured, 160.0, int(seeded) + (1 if unknown_finishes_last else 2)
            )
            held = backend._registry[("unknown", "thread-1", "devops-engineer", "", "codeact")]
            assert held.probed and held.removal is None
            deleting = _spec_requiring(Capability.FILES_DELETE)
            with pytest.raises(SandboxCapabilityNotSupported, match="files_delete"):
                asyncio.run(backend.acquire(self._key("unknown"), deleting))
            if measured:
                assert asyncio.run(backend.acquire(self._key("fresh"), deleting))
            else:
                creates = client.create_calls
                with pytest.raises(SandboxCapabilityNotSupported, match="files_delete"):
                    asyncio.run(backend.acquire(self._key("fresh"), deleting))
                assert client.create_calls == creates

        race()

    @pytest.mark.parametrize("measured", [False, True])
    @pytest.mark.parametrize("unknown_first", [False, True])
    def test_cross_loop_hint_updates_are_atomic(self, monkeypatch, measured, unknown_first):
        """A hint read and its replacement exclude a competing loop's update."""
        from maf_sandbox_acas._backend import _AcasSandbox

        monkeypatch.setattr("maf_sandbox_acas._backend.monotonic", lambda: 100.0)
        client = _GuestGroupClient(_guest_removing(measured))
        backend = _backend_with(client)
        spec = _spec_requiring(Capability.EXEC)
        asyncio.run(backend.acquire(self._key("seed"), spec))
        identity = ("pinned-id", "python-nonroot:3.13")
        first_probing, other_probing, entered, progressed = (threading.Event() for _ in range(4))
        local = threading.local()

        class _Guard:
            def __init__(self):
                self.lock = threading.Lock()

            def __enter__(self):
                if not self.lock.acquire(blocking=False):
                    progressed.set()
                    assert self.lock.acquire(timeout=5)

            def __exit__(self, *args):
                self.lock.release()

        class _Hints(dict):
            def get(self, key, default=None):
                hint = super().get(key, default)
                if getattr(local, "pause", False):
                    local.pause = False
                    entered.set()
                    assert progressed.wait(5)
                return hint

        monkeypatch.setattr(backend, "_guest_removals_guard", _Guard())
        backend._guest_removals = _Hints(backend._guest_removals)
        original = _AcasSandbox.probe_guest_removal

        async def ordered_probe(sandbox):
            if local.first:
                first_probing.set()
                assert other_probing.wait(5)
            else:
                other_probing.set()
                assert entered.wait(5)
            answer = await original(sandbox)
            local.pause = local.first
            return answer

        monkeypatch.setattr(_AcasSandbox, "probe_guest_removal", ordered_probe)

        def acquire(first):
            local.first = first
            try:
                return asyncio.run(backend.acquire(self._key("first" if first else "other"), spec))
            finally:
                if not first:
                    progressed.set()

        with ThreadPoolExecutor(max_workers=2) as pool:
            client._answer = (
                _GuestAnswer(exit_code=127) if unknown_first else _guest_removing(measured)
            )
            first = pool.submit(acquire, True)
            assert first_probing.wait(5)
            client._answer = (
                _guest_removing(measured) if unknown_first else _GuestAnswer(exit_code=127)
            )
            other = pool.submit(acquire, False)
            first.result(timeout=5)
            other.result(timeout=5)

        assert backend._guest_removals[identity] == _RemovalHint(
            measured, 160.0, 3 if unknown_first else 2
        )

    def test_a_remembered_successful_removal_does_not_license_a_newly_booted_image(self):
        """A create probes the sandbox it booted rather than trusting a remembered removal.

        An image reference is mutable, so a hint saying root describes whatever it last
        resolved to, and only a fresh probe can decide the sandbox in hand.
        """
        from maf_sandbox import SandboxCapabilityNotSupported

        client = _GuestGroupClient(_guest_removing(False))
        backend = _backend_with(client)
        spec = _spec_requiring(Capability.EXEC, Capability.FILES_DELETE)
        # What an earlier acquire measured, before the reference was repointed.
        backend._guest_removals[("pinned-id", "python-nonroot:3.13")] = _RemovalHint(
            True, float("inf")
        )

        with pytest.raises(SandboxCapabilityNotSupported, match="files_delete"):
            asyncio.run(backend.acquire(self._key(), spec))

        assert backend._guest_removals[("pinned-id", "python-nonroot:3.13")].removal is False, (
            "the fresh probe did not correct the memo, so the next acquire repeats the mistake"
        )

    def test_a_dropped_probe_does_not_displace_a_removal_another_one_measured(self):
        """A second probe of an already-measured sandbox drops, and answers what was measured.

        Driven by calling the probe directly on the entry a real acquire filled, which is the
        losing half of two overlapping probes without scheduling the fake to produce one. The
        rule is that neither the entry nor the hint may lose a removal to a failure: an entry
        demoted to `None` would refuse deletion for as long as that sandbox lives, and a
        hint demoted would refuse the next key before it could create anything.
        """
        from maf_sandbox import SandboxCapabilityNotSupported

        client = _GuestGroupClient(_guest_removing(False))
        backend = _backend_with(client)
        execing = _spec_requiring(Capability.EXEC)
        identity = ("pinned-id", "python-nonroot:3.13")

        sandbox = asyncio.run(backend.acquire(self._key("scope-a"), execing))
        for handed_out in client.clients:
            handed_out._answer = RuntimeError("transport dropped")

        held = backend._registry[("scope-a", "thread-1", "devops-engineer", "", "codeact")]
        answered = asyncio.run(backend._probe_guest_removal(sandbox, execing, held))

        assert answered is False, "the losing probe reported its failure over the measurement"
        assert backend._guest_removals[identity].removal is False
        with pytest.raises(SandboxCapabilityNotSupported, match="files_out"):
            asyncio.run(
                backend.acquire(
                    self._key("scope-b"), _spec_requiring(Capability.EXEC, Capability.FILES_OUT)
                )
            )

    def test_an_unreadable_answer_does_not_displace_one_either(self):
        """The same, down the other failure path: a guest that answers with something that is
        not a removal. Its entry is marked probed and keeps the removal it already had."""
        client = _GuestGroupClient(_guest_removing(False))
        backend = _backend_with(client)
        execing = _spec_requiring(Capability.EXEC)
        identity = ("pinned-id", "python-nonroot:3.13")

        sandbox = asyncio.run(backend.acquire(self._key("scope-a"), execing))
        for handed_out in client.clients:
            handed_out._answer = _GuestAnswer(stdout="", stderr="not found", exit_code=127)

        held = backend._registry[("scope-a", "thread-1", "devops-engineer", "", "codeact")]
        answered = asyncio.run(backend._probe_guest_removal(sandbox, execing, held))

        assert answered is False
        assert backend._guest_removals[identity].removal is False

    def test_the_second_workload_is_refused_without_a_sandbox_of_its_own(self):
        """What the memo buys: the first acquire pays a create to learn the removal, and another acquire
        within the hint lifetime does not."""
        from maf_sandbox import SandboxCapabilityNotSupported

        client = _GuestGroupClient(_guest_removing(False))
        backend = _backend_with(client)
        spec = _spec_requiring(Capability.EXEC, Capability.FILES_OUT)

        for scope in ("scope-a", "scope-b"):
            with pytest.raises(SandboxCapabilityNotSupported):
                asyncio.run(backend.acquire(self._key(scope), spec))

        assert client.create_calls == 1

    def test_a_warm_sandbox_is_gated_too(self):
        """The reuse path returns through the gate, not around it — and the refusal is not
        swallowed by the handler that replaces a sandbox which failed to resume."""
        from maf_sandbox import SandboxCapabilityNotSupported

        client = _GuestGroupClient(_guest_removing(False))
        backend = _backend_with(client)
        key = self._key()
        backend._registry[(key.scope, key.thread_id, key.agent_dir, key.call_id, "codeact")] = (
            _Held("sbx-warm", egress=(Egress.CLOSED, frozenset()))
        )

        with pytest.raises(SandboxCapabilityNotSupported):
            asyncio.run(
                backend.acquire(key, _spec_requiring(Capability.EXEC, Capability.FILES_OUT))
            )

        assert client.create_calls == 0

    def test_a_refused_create_is_deleted_rather_than_left_running(self):
        """The create is what learned the removal, and an acquire that raises is handed to nobody —
        so the framework's per-call cleanup never sees this sandbox. It is billable until
        something deletes it, and this is the only place that can."""
        from maf_sandbox import SandboxCapabilityNotSupported

        client = _GuestGroupClient(_guest_removing(False))
        backend = _backend_with(client)

        with pytest.raises(SandboxCapabilityNotSupported):
            asyncio.run(
                backend.acquire(self._key(), _spec_requiring(Capability.EXEC, Capability.FILES_OUT))
            )

        assert client.deleted == ["sbx-1"]
        assert backend._registry == {}
        assert backend._undeleted == {}

    def test_a_refused_create_whose_delete_fails_is_kept_for_the_retry(self):
        """Same record `dispose` keeps: the registry no longer holds the id, so nothing else
        in this process could ask for it again."""
        from maf_sandbox import SandboxCapabilityNotSupported

        client = _GuestGroupClient(_guest_removing(False), delete_fails=True)
        backend = _backend_with(client)
        key = self._key()

        with pytest.raises(SandboxCapabilityNotSupported):
            asyncio.run(
                backend.acquire(key, _spec_requiring(Capability.EXEC, Capability.FILES_OUT))
            )

        assert client.deleted == []
        assert backend._undeleted == {
            (key.scope, key.thread_id, key.agent_dir, key.call_id): {"sbx-1"}
        }

        client.delete_fails = False
        asyncio.run(backend.dispose(key, kind="codeact"))
        assert client.deleted == ["sbx-1"]
        assert not backend._undeleted
        assert not backend._undeleted_kinds

    def test_a_refused_create_does_not_restore_a_completed_disposal(self):
        from maf_sandbox import SandboxCapabilityNotSupported

        from maf_sandbox_acas._backend import _Deletion

        client = _GuestGroupClient(_guest_removing(False))
        backend = _backend_with(client)
        key = self._key()
        original = backend._delete
        entered, release = asyncio.Event(), asyncio.Event()
        attempts = 0

        async def delete(group_client, sandbox_id):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                entered.set()
                await release.wait()
                return _Deletion(False, DisposalFailure("refused", "delete refused"))
            return _Deletion(True)

        backend._delete = delete

        async def scenario():
            acquire = asyncio.create_task(
                backend.acquire(key, _spec_requiring(Capability.EXEC, Capability.FILES_OUT))
            )
            await entered.wait()
            assert await backend.dispose(key) is None
            assert not backend._undeleted_kinds
            release.set()
            with pytest.raises(SandboxCapabilityNotSupported):
                await acquire
            backend._delete = original
            assert await backend.dispose(key, kind="codeact") is None

        asyncio.run(asyncio.wait_for(scenario(), timeout=5))
        assert client.deleted == []
        assert not backend._undeleted
        assert not backend._undeleted_kinds

    def test_a_refused_reuse_keeps_the_sandbox_for_the_key_to_dispose(self):
        """The other half of the split: a warm sandbox predates this acquire, so it belongs to
        the key's own disposal rather than to the acquire that was refused."""
        from maf_sandbox import SandboxCapabilityNotSupported

        client = _GuestGroupClient(_guest_removing(False))
        backend = _backend_with(client)
        key = self._key()
        registry_key = (key.scope, key.thread_id, key.agent_dir, key.call_id, "codeact")
        backend._registry[registry_key] = _Held("sbx-warm", egress=(Egress.CLOSED, frozenset()))

        with pytest.raises(SandboxCapabilityNotSupported):
            asyncio.run(
                backend.acquire(key, _spec_requiring(Capability.EXEC, Capability.FILES_OUT))
            )

        assert backend._registry == {
            registry_key: _Held(
                "sbx-warm", egress=(Egress.CLOSED, frozenset()), removal=False, probed=True
            )
        }
        assert client.deleted == []

    def test_a_refused_reuse_is_not_logged_as_a_reuse(self, caplog):
        """`acquire` promises to name which of three things happened. A refused call did none
        of them."""
        from maf_sandbox import SandboxCapabilityNotSupported

        client = _GuestGroupClient(_guest_removing(False))
        backend = _backend_with(client)
        key = self._key()
        backend._registry[(key.scope, key.thread_id, key.agent_dir, key.call_id, "codeact")] = (
            _Held("sbx-warm", egress=(Egress.CLOSED, frozenset()))
        )

        with caplog.at_level(logging.INFO, logger="maf_sandbox_acas"):
            with pytest.raises(SandboxCapabilityNotSupported):
                asyncio.run(
                    backend.acquire(key, _spec_requiring(Capability.EXEC, Capability.FILES_OUT))
                )

        assert not [r for r in caplog.records if "sandbox reused" in r.getMessage()], caplog.text

    def test_a_workload_that_neither_execs_nor_collects_is_never_probed(self):
        """A spec asking for nothing the removal could refuse pays nothing to find it out."""
        client = _GuestGroupClient(_guest_removing(False))
        backend = _backend_with(client)

        asyncio.run(backend.acquire(self._key(), _spec_requiring(Capability.FILES_IN)))

        assert client.probes == []


# ---------------------------------------------------------------------------
# dispose_scope — cross-replica purge
# ---------------------------------------------------------------------------


class TestDisposeScope:
    def test_a_dispose_landing_mid_purge_neither_crashes_nor_is_clobbered(self):
        """Teardown for one key is not serialized, so the purge reconciles against the live
        record: it must not index a prefix a `dispose` removed, nor drop an id it added."""
        release = asyncio.Event()
        prefix = ("scope-a", "thread-1", "devops-engineer", "")
        backend = _backend_with(_FakeGroupClient())
        backend._undeleted[prefix] = {"sbx-1"}
        original = backend._delete

        async def slow_delete(group_client, sandbox_id):
            await release.wait()
            return await original(group_client, sandbox_id)

        backend._delete = slow_delete  # type: ignore[method-assign]

        async def drive() -> None:
            purge = asyncio.create_task(backend.dispose_scope("scope-a", "thread-1"))
            await asyncio.sleep(0)
            backend._undeleted.pop(prefix, None)
            backend._undeleted[prefix] = {"sbx-2"}
            release.set()
            await purge

        asyncio.run(drive())
        assert backend._undeleted == {prefix: {"sbx-2"}}, "the newer record survives"

    def test_reaches_sandboxes_this_process_never_created(self):
        """The registry is a fast path; the service is the source of truth."""
        client = _FakeGroupClient(sandboxes=[_FakeSandbox("sbx-remote")])
        backend = _backend_with(client)

        assert asyncio.run(backend.dispose_scope("scope-a", "thread-1")).disposed == 1
        assert client.deleted == ["sbx-remote"]
        assert client.last_labels == {"scope": "scope-a", "thread": "thread-1"}

    def test_unions_the_registry_with_the_service_listing(self):
        client = _FakeGroupClient(sandboxes=[_FakeSandbox("sbx-remote")])
        backend = _backend_with(client)
        backend._registry[("scope-a", "thread-1", "devops-engineer", "", "bicep")] = _Held(
            "sbx-local", egress=(Egress.CLOSED, frozenset())
        )

        assert asyncio.run(backend.dispose_scope("scope-a", "thread-1")).disposed == 2
        assert sorted(client.deleted) == ["sbx-local", "sbx-remote"]

    def test_does_not_delete_another_scopes_sandbox(self):
        client = _FakeGroupClient()
        backend = _backend_with(client)
        backend._registry[("scope-b", "thread-1", "devops-engineer", "", "bicep")] = _Held(
            "sbx-other", egress=(Egress.CLOSED, frozenset())
        )

        assert asyncio.run(backend.dispose_scope("scope-a", "thread-1")).disposed == 0
        assert client.deleted == []
        assert ("scope-b", "thread-1", "devops-engineer", "", "bicep") in backend._registry

    def test_registry_entries_are_dropped_even_when_the_delete_fails(self):
        """A stale entry is worse than none — the next acquire would try to resume it."""
        backend = _backend_with(_ExplodingGroupClient())
        backend._registry[("scope-a", "thread-1", "devops-engineer", "", "bicep")] = _Held(
            "sbx-local", egress=(Egress.CLOSED, frozenset())
        )

        asyncio.run(backend.dispose_scope("scope-a", "thread-1"))
        assert backend._registry == {}

    def test_a_failing_listing_says_the_sweep_may_be_partial(self):
        """The registry still names what this process created, but a purge that could not read
        the labels cannot claim to have reached another replica's sandbox."""
        backend = _backend_with(_ExplodingGroupClient())
        purge = asyncio.run(backend.dispose_scope("scope-a", "thread-1"))
        assert purge.undisposed is not None
        assert purge.undisposed.code == "unlisted"
        assert "partial" in purge.undisposed.detail

    def test_a_group_client_that_cannot_be_built_is_reported_and_keeps_the_ids(self):
        """Both, or the purge is dishonest one way or the other: it has to say it never reached
        the service, and keep the ids — the registry is popped before the client is built, so
        without the record they are in neither place and the next `dispose` reports them gone."""
        backend = AcasSandboxBackend(_config())
        backend._registry[("scope-a", "thread-1", "devops-engineer", "", "bicep")] = _Held(
            "sbx-1", egress=(Egress.CLOSED, frozenset())
        )

        def _unreachable():
            raise RuntimeError("no credential")

        backend._group_client = _unreachable  # type: ignore[method-assign]
        purge = asyncio.run(backend.dispose_scope("scope-a", "thread-1"))
        assert purge.disposed == 0
        assert purge.undisposed is not None
        assert purge.undisposed.code == "unreachable"
        assert "no credential" in purge.undisposed.detail
        assert backend._undeleted == {("scope-a", "thread-1", "devops-engineer", ""): {"sbx-1"}}

        key = SandboxKey(scope="scope-a", thread_id="thread-1", agent_dir="devops-engineer")
        assert asyncio.run(backend.dispose(key)) is not None, "the retry still reports"

    def test_a_service_failure_degrades_to_zero_rather_than_raising(self):
        """Purge must not fail a conversation delete."""
        backend = _backend_with(_ExplodingGroupClient())
        assert asyncio.run(backend.dispose_scope("scope-a", "thread-1")).disposed == 0


class TestFileWrites:
    """A workload may hand the backend a nested path, so parents must be created.

    `infra/main.bicep` is the example in the bicep tool's own description, and the file API
    docs say nothing about whether a write creates missing parents — only the SDK signature
    does (`create_dirs: bool = True`). Since that is a `0.1.0bN` default doing load-bearing
    work, the backend passes it explicitly and this pins that it does.
    """

    def test_requests_parent_directory_creation(self):
        from maf_sandbox_acas._backend import _AcasSandbox

        class _RecordingClient:
            def __init__(self) -> None:
                self.calls: list[tuple] = []
                self._sbx_path = ""
                self._api_version = ""

            async def write_file(self, path, content, **kwargs):
                self.calls.append((path, content, kwargs))

            async def _dp_get(self, path, *, params):
                return {"isSymlink": False, "isDir": True}

        client = _RecordingClient()
        asyncio.run(
            _AcasSandbox(client, 30.0).write_file(
                "/maf-sandbox/work/infra/main.bicep",
                "param x string",
                working_directory="/maf-sandbox/work",
            )
        )

        assert client.calls == [
            ("/maf-sandbox/work/infra/main.bicep", "param x string", {"create_dirs": True})
        ]

    def test_a_refused_path_never_reaches_the_sdk(self):
        from maf_sandbox_acas._backend import _AcasSandbox

        class _RecordingClient:
            def __init__(self) -> None:
                self.calls: list[tuple] = []

            async def write_file(self, path, content, **kwargs):
                self.calls.append((path, content, kwargs))

        client = _RecordingClient()
        with pytest.raises(ValueError):
            asyncio.run(
                _AcasSandbox(client, 30.0).write_file(
                    "../escape", "x", working_directory="/maf-sandbox/work"
                )
            )
        assert client.calls == []


class _WriteRoadClient:
    """The data plane the write-road probe reaches, answering whatever the guest was told to.

    ``said`` is what the one guest command prints — the probe reads its words and nothing else,
    so a test states the guest's answer rather than simulating a shell to produce it.
    """

    def __init__(self, said: str = "", *, exec_failure: Exception | None = None) -> None:
        self.sandbox_id = "sbx-1"
        self._sbx_path = ""
        self._api_version = ""
        self.written: list[tuple[str, object, dict]] = []
        self.deleted: list[tuple[str, bool]] = []
        self.commands: list[str] = []
        self._said = said
        self._exec_failure = exec_failure

    async def write_file(self, path, content, **kwargs) -> None:
        self.written.append((path, content, kwargs))

    async def delete_file(self, path, *, recursive: bool = False) -> None:
        self.deleted.append((path, recursive))

    async def exec(self, command: str, *, working_directory: str):
        self.commands.append(command)
        if self._exec_failure is not None:
            raise self._exec_failure
        return SimpleNamespace(exit_code=0, stdout=self._said, stderr="")

    async def _dp_get(self, path, *, params=None):
        return {"isSymlink": False, "isDir": True}


def _road_sandbox(client, held: _Held | None = None):
    from maf_sandbox_acas._backend import _AcasSandbox

    return _AcasSandbox(
        client,
        30.0,
        held=held if held is not None else _Held("sbx-1", egress=(Egress.CLOSED, frozenset())),
    )


class _GuestRoadSandbox:
    """An ``_AcasSandbox`` whose guest commands are recorded rather than run.

    Built by patching ``exec_bounded`` on a real instance: what is under test is which road
    ``write_file`` takes and how the shell road's vocabulary comes back, not the transport the
    road runs over — which ``test_acas_e2e.py`` exercises against the service.
    """

    def __init__(self, client, answers=None) -> None:
        from maf_sandbox import ExecResult

        self.sandbox = _road_sandbox(
            client, _Held("sbx-1", egress=(Egress.CLOSED, frozenset()), write_road=True)
        )
        self.commands: list[str] = []
        self._answers = list(answers or [])
        self._default = ExecResult(stdout="", stderr="", exit_code=0)

        async def exec_bounded(command, *, working_directory, timeout, max_output_bytes):
            self.commands.append(command)
            return self._answers.pop(0) if self._answers else self._default

        self.sandbox.exec_bounded = exec_bounded  # type: ignore[method-assign]


class TestTheWriteRoad:
    """Which principal a write runs as, and the acquire-time probe that decides it (#1131).

    The data plane lands ``0:0`` whatever the image's ``USER`` is, so on an image whose guest
    is not root it acts above the guest and a component swapped after the confinement check
    sends host-authority bytes wherever the link points. Writing as the guest bounds that by
    construction; the probe below is what says whether it is needed and whether it would land.
    """

    def test_the_script_asks_its_three_questions_in_one_command(self):
        from maf_sandbox_acas._backend import _write_road_script

        script = _write_road_script("/base/.maf-write-probe-1", "/base/.maf-write-probe-1.guest")

        lines = script.splitlines()
        assert len(lines) == 4, script
        assert "> /base/.maf-write-probe-1 " in lines[0] and "echo reach" in lines[0]
        assert "/base/.maf-write-probe-1.guest" in lines[1] and "echo write" in lines[1]
        assert "mkdir mv base64" in lines[2] and "exit 0" in lines[2]
        assert lines[3] == "echo utilities"

    def test_a_refused_redirection_answers_rather_than_ending_the_script(self):
        """`:` is a POSIX special builtin: a redirection it cannot make exits the shell.

        That is the guest this probe exists to describe, so the natural `: > file` spelling
        would end the script on its first line and every non-root image would read as having
        no road at all. `true` is a regular builtin, and its `2>/dev/null` comes before the
        open so the guest's own diagnostic never reaches the answer.
        """
        from maf_sandbox_acas._backend import _write_road_script

        lines = _write_road_script("/base/probe", "/base/probe.guest").splitlines()

        for line in lines[:2]:
            assert line.startswith("true 2>/dev/null > "), line
            assert not line.startswith(":"), line

    def test_a_base_holding_a_path_the_shell_would_read_is_quoted(self):
        from maf_sandbox_acas._backend import _write_road_script

        script = _write_road_script("/base; rm -rf /", "/base; rm -rf /.guest")

        assert "'/base; rm -rf /'" in script
        assert shlex.split(script.splitlines()[0])[:3] == ["true", "2>/dev/null", ">"]
        assert shlex.split(script.splitlines()[0])[3] == "/base; rm -rf /"

    def test_a_plane_already_within_the_guests_reach_keeps_the_plane(self):
        """The root image, and the common case: the shell road would bound nothing there."""
        client = _WriteRoadClient("reach\nwrite\nutilities\n")

        assert asyncio.run(_road_sandbox(client).probe_write_road()) is False

    def test_a_plane_above_the_guest_takes_the_shell_road(self):
        client = _WriteRoadClient("write\nutilities\n")

        assert asyncio.run(_road_sandbox(client).probe_write_road()) is True

    def test_a_base_the_guest_cannot_write_keeps_the_plane(self):
        """The default base on a non-root image: the shell road would refuse every write."""
        client = _WriteRoadClient("")

        assert asyncio.run(_road_sandbox(client).probe_write_road()) is False

    def test_an_image_missing_a_utility_the_road_runs_keeps_the_plane(self):
        client = _WriteRoadClient("write\n")

        assert asyncio.run(_road_sandbox(client).probe_write_road()) is False

    def test_the_probe_plants_in_the_base_and_takes_its_file_back(self):
        client = _WriteRoadClient("reach\nwrite\nutilities\n")

        asyncio.run(_road_sandbox(client).probe_write_road())

        planted = client.written[0][0]
        assert planted.startswith("/maf-sandbox/work/.maf-write-probe-")
        assert client.deleted == [(planted, False)]

    def test_a_probe_the_guest_refused_still_takes_its_file_back(self):
        client = _WriteRoadClient(exec_failure=RuntimeError("the service refused"))

        with pytest.raises(RuntimeError):
            asyncio.run(_road_sandbox(client).probe_write_road())

        assert [path for path, _ in client.deleted] == [client.written[0][0]]

    def test_an_unreachable_probe_leaves_the_road_unchosen(self):
        """The plane serves meanwhile, so a probe that cannot complete withholds no in-door."""
        held = _Held("sbx-1", egress=(Egress.CLOSED, frozenset()))
        client = _WriteRoadClient(exec_failure=RuntimeError("the service refused"))
        spec = SandboxSpec(kind="k", requires=frozenset({Capability.FILES_IN}))

        asyncio.run(_road_sandbox(client, held).choose_write_road(spec))

        assert held.write_road is None

    def test_the_road_is_settled_once_per_sandbox(self):
        held = _Held("sbx-1", egress=(Egress.CLOSED, frozenset()))
        client = _WriteRoadClient("write\nutilities\n")
        spec = SandboxSpec(kind="k", requires=frozenset({Capability.FILES_IN}))
        sandbox = _road_sandbox(client, held)

        asyncio.run(sandbox.choose_write_road(spec))
        asyncio.run(sandbox.choose_write_road(spec))

        assert held.write_road is True
        assert len(client.commands) == 1

    def test_a_workload_that_writes_nothing_never_probes(self):
        held = _Held("sbx-1", egress=(Egress.CLOSED, frozenset()))
        client = _WriteRoadClient("write\nutilities\n")
        spec = SandboxSpec(kind="k", requires=frozenset({Capability.EXEC}))

        asyncio.run(_road_sandbox(client, held).choose_write_road(spec))

        assert held.write_road is None
        assert client.written == [] and client.commands == []

    def test_the_shell_road_puts_the_bytes_there_rather_than_the_plane(self):
        client = _WriteRoadClient()
        guest = _GuestRoadSandbox(client)

        asyncio.run(
            guest.sandbox.write_file(
                "infra/main.bicep", "param x string", working_directory="/maf-sandbox/work"
            )
        )

        assert client.written == [], "the data plane wrote where the guest was supposed to"
        assert any("mkdir -p -- /maf-sandbox/work/infra" in c for c in guest.commands)
        assert any("base64 -d" in c for c in guest.commands)
        moved = guest.commands[-1]
        assert moved.endswith("/maf-sandbox/work/infra/main.bicep")

    def test_both_roads_encode_a_string_as_utf8(self):
        import base64

        client = _WriteRoadClient()
        guest = _GuestRoadSandbox(client)

        asyncio.run(
            guest.sandbox.write_file("note.txt", "héllo", working_directory="/maf-sandbox/work")
        )

        chunk = next(c for c in guest.commands if "base64 -d" in c)
        encoded = chunk.split("printf %s ", 1)[1].split()[0]
        assert base64.b64decode(encoded) == "héllo".encode()

    def test_a_path_outside_the_base_reaches_neither_road(self):
        client = _WriteRoadClient()
        guest = _GuestRoadSandbox(client)

        with pytest.raises(ValueError):
            asyncio.run(
                guest.sandbox.write_file("../escape", "x", working_directory="/maf-sandbox/work")
            )

        assert guest.commands == [] and client.written == []

    def test_the_guests_refusal_arrives_as_the_error_the_plane_would_raise(self):
        from maf_sandbox import ExecResult

        for stderr, expected in (
            ("sh: 1: cannot create /base/f: Permission denied", PermissionError),
            ("sh: 1: cannot create /base/f: Is a directory", IsADirectoryError),
            ("sh: 1: cannot create /base/f: No such file or directory", FileNotFoundError),
        ):
            client = _WriteRoadClient()
            guest = _GuestRoadSandbox(
                client, answers=[ExecResult(stdout="", stderr=stderr, exit_code=1)]
            )
            with pytest.raises(expected):
                asyncio.run(
                    guest.sandbox.write_file("f", "x", working_directory="/maf-sandbox/work")
                )

    def test_every_refusal_the_core_can_raise_has_an_error_of_its_own(self):
        """The mapping is read with a fallback, so a refusal it lost would degrade silently.

        A member added to the core's `FileRefusal` would arrive here as a plain `OSError`
        rather than as the `PermissionError` or `IsADirectoryError` a caller reads. That is the
        right runtime behaviour and the wrong thing to discover at runtime, so it is red here.
        """
        from maf_sandbox import FileRefusal

        from maf_sandbox_acas._backend import _REFUSAL_ERRORS

        assert set(_REFUSAL_ERRORS) == set(FileRefusal)

    def test_a_transfer_that_failed_with_no_refusal_is_an_oserror(self):
        from maf_sandbox import ExecResult

        client = _WriteRoadClient()
        guest = _GuestRoadSandbox(
            client, answers=[ExecResult(stdout="", stderr="the guest blew up", exit_code=9)]
        )

        with pytest.raises(OSError, match="the guest blew up"):
            asyncio.run(guest.sandbox.write_file("f", "x", working_directory="/maf-sandbox/work"))


class TestExecArgv:
    """`_AcasSandbox.exec` accepts a sequence and quotes it before the SDK's string-only exec.

    The SDK's ``exec`` takes one string; a caller handing this an argv sequence must be able
    to trust that no element — however it is shaped — can be re-interpreted as more than one
    token or a second command once the sandbox's shell sees it.  ``shlex.split`` of what was
    actually sent to the SDK recovering the exact original argv is that proof.
    """

    def _command_tokens(self, script):
        import shlex

        before, start, rest = script.partition('(umask "$old_umask"; exec ')
        command, end, after = rest.rpartition(') > "$d/outpipe" 2> "$d/errpipe"\nrc=$?\n')
        assert before and start and end and after
        return shlex.split(command)

    class _RecordingClient:
        sandbox_id = "recording"

        def __init__(self) -> None:
            self.calls: list[str] = []

        async def exec(self, command: str, *, working_directory: str):
            self.calls.append(command)

            class _Result:
                stdout = ""
                stderr = ""
                exit_code = 0

            if command.startswith("for tool"):
                token = next(
                    line.split("=", 1)[1].removeprefix("/tmp/")
                    for line in command.splitlines()
                    if line.startswith("d=")
                )
                _Result.stdout = f"{token} 0 0 0\n"
            return _Result()

    def test_a_string_command_passes_through_unchanged(self):
        from maf_sandbox_acas._backend import _AcasSandbox

        client = self._RecordingClient()
        asyncio.run(
            _AcasSandbox(client, 30.0).exec(
                "echo hi", working_directory="/maf-sandbox/work", timeout=5
            )
        )
        assert self._command_tokens(client.calls[0]) == ["sh", "-c", "echo hi"]
        assert len(client.calls) == 2

    def test_a_sequence_is_quoted_with_shlex_join(self):
        import shlex

        from maf_sandbox_acas._backend import _AcasSandbox

        client = self._RecordingClient()
        argv = ["echo", "a; rm -rf /", "$(id)", "`id`", "it's mine", 'say "hi"', "one\ntwo"]
        asyncio.run(
            _AcasSandbox(client, 30.0).exec(argv, working_directory="/maf-sandbox/work", timeout=5)
        )

        tokens = self._command_tokens(client.calls[0])
        assert tokens == ["sh", "-c", shlex.join(argv)]
        assert shlex.split(tokens[2]) == argv

    def test_a_bare_space_separated_argv_stays_one_command(self):
        import shlex

        from maf_sandbox_acas._backend import _AcasSandbox

        client = self._RecordingClient()
        argv = [
            "bicep",
            "build",
            "/maf-sandbox/work/r1/main.bicep",
            "--diagnostics-format",
            "sarif",
        ]
        asyncio.run(
            _AcasSandbox(client, 30.0).exec(argv, working_directory="/maf-sandbox/work", timeout=5)
        )

        tokens = self._command_tokens(client.calls[0])
        assert tokens == ["sh", "-c", shlex.join(argv)]
        assert shlex.split(tokens[2]) == argv


class TestNarrowedDisposal:
    @pytest.mark.parametrize("first", ["kind", "scope", "refused"])
    @pytest.mark.parametrize("second", ["kind", "scope"])
    @pytest.mark.parametrize("outcome", ["failure", "cancel", "unreachable"])
    def test_cross_loop_success_preserves_a_newer_retry(self, first, second, outcome, monkeypatch):
        from maf_sandbox_acas._backend import _Deletion

        client = _FakeGroupClient()
        backend = _backend_with(client)
        key = SandboxKey(scope="scope-a", thread_id="thread-1", agent_dir="agent")
        prefix = (key.scope, key.thread_id, key.agent_dir, key.call_id)
        if first != "refused":
            backend._registry[(*prefix, "a")] = _Held(
                "selected", egress=(Egress.CLOSED, frozenset())
            )
        entered, progressed = threading.Event(), threading.Event()
        failure = DisposalFailure("refused", "delete refused")
        attempts = 0

        class _Guard:
            def __init__(self):
                self.lock = threading.Lock()

            def __enter__(self):
                if not self.lock.acquire(blocking=False):
                    progressed.set()
                    assert self.lock.acquire(timeout=5)

            def __exit__(self, *args):
                self.lock.release()

        class _Ledger(dict):
            armed = True

            def pop(self, at, default=None):
                if self.armed and at == prefix:
                    self.armed = False
                    entered.set()
                    assert progressed.wait(5)
                return super().pop(at, default)

        monkeypatch.setattr(backend, "_disposal_guard", _Guard(), raising=False)
        backend._undeleted = _Ledger()
        original = backend._delete

        async def delete(group_client, sandbox_id):
            nonlocal attempts
            attempts += 1
            assert sandbox_id == "selected"
            if attempts == 1:
                return _Deletion(True)
            if outcome == "cancel":
                raise asyncio.CancelledError
            return _Deletion(False, failure)

        def unavailable():
            raise RuntimeError("group unavailable")

        monkeypatch.setattr(backend, "_delete", delete)

        async def cleanup(operation):
            if operation == "refused":
                return await backend._release_the_refused(client, key, "selected", kind="a")
            if operation == "scope":
                return await backend.dispose_scope(key.scope, key.thread_id)
            return await backend.dispose(key, kind="a")

        def newer_loop():
            assert entered.wait(5)
            backend._registry[(*prefix, "a")] = _Held(
                "selected", egress=(Egress.CLOSED, frozenset())
            )
            try:
                with monkeypatch.context() as pending:
                    if outcome == "unreachable":
                        pending.setattr(backend, "_group_client", unavailable)
                    if outcome == "cancel":
                        with pytest.raises(asyncio.CancelledError):
                            asyncio.run(cleanup(second))
                    else:
                        result = asyncio.run(cleanup(second))
                        assert (
                            result.undisposed if isinstance(result, ScopePurge) else result
                        ) is not None
            finally:
                progressed.set()

        with ThreadPoolExecutor(max_workers=1) as pool:
            newer = pool.submit(newer_loop)
            asyncio.run(cleanup(first))
            newer.result(timeout=5)

        assert backend._undeleted == {prefix: {"selected"}}
        assert backend._undeleted_kinds == {prefix: {"selected": "a"}}
        monkeypatch.setattr(backend, "_delete", original)
        assert asyncio.run(backend.dispose(key, kind="a")) is None
        assert client.deleted == ["selected"]
        assert not backend._undeleted and not backend._undeleted_kinds
        assert not backend._disposal_tokens

    @pytest.mark.parametrize("first", ["kind", "whole", "scope", "refused"])
    @pytest.mark.parametrize("second", ["kind", "scope"])
    @pytest.mark.parametrize("outcome", ["failure", "cancel", "unreachable"])
    def test_stale_success_preserves_a_newer_retry(self, first, second, outcome, monkeypatch):
        from maf_sandbox_acas._backend import _Deletion

        client = _FakeGroupClient()
        backend = _backend_with(client)
        key = SandboxKey(scope="scope-a", thread_id="thread-1", agent_dir="agent")
        prefix = (key.scope, key.thread_id, key.agent_dir, key.call_id)
        if first != "refused":
            backend._registry[(*prefix, "a")] = _Held(
                "selected", egress=(Egress.CLOSED, frozenset())
            )
        original = backend._delete
        entered, release = asyncio.Event(), asyncio.Event()
        attempts = 0
        failure = DisposalFailure("refused", "delete refused")

        async def delete(group_client, sandbox_id):
            nonlocal attempts
            attempts += 1
            assert sandbox_id == "selected"
            if attempts == 1:
                entered.set()
                await release.wait()
                return _Deletion(True)
            if outcome == "cancel":
                raise asyncio.CancelledError
            return _Deletion(False, failure)

        def unavailable():
            raise RuntimeError("group unavailable")

        async def cleanup(operation):
            if operation == "refused":
                return await backend._release_the_refused(client, key, "selected", kind="a")
            if operation == "scope":
                return await backend.dispose_scope(key.scope, key.thread_id)
            return await backend.dispose(key, kind="a" if operation == "kind" else None)

        monkeypatch.setattr(backend, "_delete", delete)

        async def scenario():
            older = asyncio.create_task(cleanup(first))
            await entered.wait()
            with monkeypatch.context() as pending:
                if outcome == "unreachable":
                    pending.setattr(backend, "_group_client", unavailable)
                if outcome == "cancel":
                    with pytest.raises(asyncio.CancelledError):
                        await cleanup(second)
                else:
                    result = await cleanup(second)
                    assert (
                        result.undisposed if isinstance(result, ScopePurge) else result
                    ) is not None
            release.set()
            await older
            assert older.done()
            assert backend._undeleted == {prefix: {"selected"}}
            assert backend._undeleted_kinds == {prefix: {"selected": "a"}}
            monkeypatch.setattr(backend, "_delete", original)
            assert await backend.dispose(key, kind="a") is None
            assert client.deleted == ["selected"]
            assert not backend._undeleted and not backend._undeleted_kinds
            assert not getattr(backend, "_disposal_tokens", {})

        asyncio.run(asyncio.wait_for(scenario(), timeout=5))

    @pytest.mark.parametrize("operation", ["kind", "whole", "scope"])
    @pytest.mark.parametrize("retained", [False, True])
    @pytest.mark.parametrize("new_ledger", [False, True])
    def test_stale_failure_does_not_restore_a_completed_retry(
        self, operation, retained, new_ledger
    ):
        from maf_sandbox_acas._backend import _Deletion

        client = _FakeGroupClient()
        backend = _backend_with(client)
        key = SandboxKey(scope="scope-a", thread_id="thread-1", agent_dir="agent")
        prefix = (key.scope, key.thread_id, key.agent_dir, key.call_id)
        if retained:
            backend._undeleted[prefix] = {"selected"}
            backend._undeleted_kinds[prefix] = {"selected": "a"}
        else:
            backend._registry[(*prefix, "a")] = _Held(
                "selected", egress=(Egress.CLOSED, frozenset())
            )
        original = backend._delete
        entered, release = asyncio.Event(), asyncio.Event()
        attempts = 0
        failure = DisposalFailure("refused", "delete refused")

        async def delete(group_client, sandbox_id):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                entered.set()
                await release.wait()
                return _Deletion(False, failure)
            return _Deletion(attempts == 2, None if attempts == 2 else failure)

        backend._delete = delete

        async def scenario():
            cleanup = (
                backend.dispose_scope(key.scope, key.thread_id)
                if operation == "scope"
                else backend.dispose(key, kind="a" if operation == "kind" else None)
            )
            first = asyncio.create_task(cleanup)
            await entered.wait()
            assert await backend.dispose(key, kind="a") is None
            assert attempts == 2
            assert prefix not in backend._undeleted_kinds
            if new_ledger:
                backend._registry[(*prefix, "b")] = _Held(
                    "sibling", egress=(Egress.CLOSED, frozenset())
                )
                assert await backend.dispose(key, kind="b") is not None
            release.set()
            result = await first
            assert (result.undisposed if isinstance(result, ScopePurge) else result) is not None
            backend._delete = original
            assert await backend.dispose(key, kind="a") is None

        asyncio.run(asyncio.wait_for(scenario(), timeout=5))
        assert client.deleted == []
        assert backend._undeleted == ({prefix: {"sibling"}} if new_ledger else {})
        assert backend._undeleted_kinds == ({prefix: {"sibling": "b"}} if new_ledger else {})

    @pytest.mark.parametrize(
        "kind,expected", [("a", ["selected"]), (None, ["selected", "sibling"])]
    )
    def test_only_the_requested_kinds_are_deleted(self, kind, expected):
        client = _FakeGroupClient()
        backend = _backend_with(client)
        key = SandboxKey(scope="scope-a", thread_id="thread-1", agent_dir="agent")
        prefix = (key.scope, key.thread_id, key.agent_dir, key.call_id)
        backend._registry[(*prefix, "a")] = _Held("selected", egress=(Egress.CLOSED, frozenset()))
        backend._registry[(*prefix, "b")] = _Held("sibling", egress=(Egress.CLOSED, frozenset()))
        assert asyncio.run(backend.dispose(key, kind=kind)) is None
        assert client.deleted == expected
        assert bool(backend._registry) is (kind is not None)

    def test_narrowed_retries_preserve_kind_attribution(self):
        backend = _backend_with(_ExplodingGroupClient())
        key = SandboxKey(scope="scope-a", thread_id="thread-1", agent_dir="agent")
        prefix = (key.scope, key.thread_id, key.agent_dir, key.call_id)
        backend._registry[(*prefix, "a")] = _Held("selected", egress=(Egress.CLOSED, frozenset()))
        backend._registry[(*prefix, "b")] = _Held("sibling", egress=(Egress.CLOSED, frozenset()))
        for kind in ("a", "b", "a"):
            assert asyncio.run(backend.dispose(key, kind=kind)) is not None
        client = _FakeGroupClient()
        backend._group_client = lambda: client
        assert asyncio.run(backend.dispose(key, kind="a")) is None
        assert client.deleted == ["selected"]
        assert backend._undeleted == {prefix: {"sibling"}}
        assert asyncio.run(backend.dispose(key)) is None
        assert client.deleted == ["selected", "sibling"]
        assert not backend._undeleted
        assert not backend._undeleted_kinds

    def test_a_failed_scope_purge_keeps_the_registry_kinds_for_retry(self):
        backend = _backend_with(_ExplodingGroupClient())
        key = SandboxKey(scope="scope-a", thread_id="thread-1", agent_dir="agent")
        prefix = (key.scope, key.thread_id, key.agent_dir, key.call_id)
        backend._registry[(*prefix, "a")] = _Held("selected", egress=(Egress.CLOSED, frozenset()))
        backend._registry[(*prefix, "b")] = _Held("sibling", egress=(Egress.CLOSED, frozenset()))
        asyncio.run(backend.dispose_scope(key.scope, key.thread_id))
        client = _FakeGroupClient()
        backend._group_client = lambda: client
        assert asyncio.run(backend.dispose(key, kind="a")) is None
        assert client.deleted == ["selected"]
        assert backend._undeleted == {prefix: {"sibling"}}


class TestDispose:
    def test_deletes_the_keyed_sandbox_and_forgets_it(self):
        client = _FakeGroupClient()
        backend = _backend_with(client)
        key = SandboxKey(scope="scope-a", thread_id="thread-1", agent_dir="devops-engineer")
        backend._registry[(key.scope, key.thread_id, key.agent_dir, key.call_id, "bicep")] = _Held(
            "sbx-1", egress=(Egress.CLOSED, frozenset())
        )

        asyncio.run(backend.dispose(key))
        assert client.deleted == ["sbx-1"]
        assert backend._registry == {}

    def test_is_a_no_op_for_an_unknown_key(self):
        client = _FakeGroupClient()
        backend = _backend_with(client)
        key = SandboxKey(scope="scope-a", thread_id="thread-1", agent_dir="devops-engineer")

        assert asyncio.run(backend.dispose(key)) is None
        assert client.deleted == []

    def test_a_delete_that_lands_reports_nothing(self):
        backend = _backend_with(_FakeGroupClient())
        key = SandboxKey(scope="scope-a", thread_id="thread-1", agent_dir="devops-engineer")
        backend._registry[(key.scope, key.thread_id, key.agent_dir, key.call_id, "bicep")] = _Held(
            "sbx-1", egress=(Egress.CLOSED, frozenset())
        )

        assert asyncio.run(backend.dispose(key)) is None

    def test_a_failed_delete_comes_back_as_the_reason(self):
        """Never raising is the contract, so the reason is the only way the router hears (#641)."""
        backend = _backend_with(_ExplodingGroupClient())
        key = SandboxKey(scope="scope-a", thread_id="thread-1", agent_dir="devops-engineer")
        backend._registry[(key.scope, key.thread_id, key.agent_dir, key.call_id, "bicep")] = _Held(
            "sbx-1", egress=(Egress.CLOSED, frozenset())
        )

        reason = asyncio.run(backend.dispose(key))
        assert reason is not None
        assert reason.code == "refused", "the service answered and the sandbox stayed"
        assert "sbx-1" in reason.detail
        assert backend._registry == {}

    def test_a_second_attempt_still_reports_what_the_first_could_not_delete(self):
        """An id a delete could not remove outlives the registry entry it came from."""
        backend = _backend_with(_ExplodingGroupClient())
        key = SandboxKey(scope="scope-a", thread_id="thread-1", agent_dir="devops-engineer")
        backend._registry[(key.scope, key.thread_id, key.agent_dir, key.call_id, "bicep")] = _Held(
            "sbx-1", egress=(Egress.CLOSED, frozenset())
        )

        assert asyncio.run(backend.dispose(key)) is not None
        second = asyncio.run(backend.dispose(key))
        assert second is not None
        assert "sbx-1" in second.detail

    def test_a_group_client_that_cannot_be_built_keeps_the_ids_for_a_retry(self):
        backend = AcasSandboxBackend(_config())
        key = SandboxKey(scope="scope-a", thread_id="thread-1", agent_dir="devops-engineer")
        backend._registry[(key.scope, key.thread_id, key.agent_dir, key.call_id, "bicep")] = _Held(
            "sbx-1", egress=(Egress.CLOSED, frozenset())
        )

        def _unreachable():
            raise RuntimeError("no credential")

        backend._group_client = _unreachable  # type: ignore[method-assign]
        assert asyncio.run(backend.dispose(key)) is not None
        assert asyncio.run(backend.dispose(key)) is not None

    def test_a_delete_cancelled_part_way_still_leaves_the_id_to_retry(self):
        """The record is written before the first await, so a bound that expires mid-delete
        does not take the only name of the sandbox with it."""

        class _Hanging:
            async def begin_delete(self) -> None:
                await asyncio.Event().wait()

        class _Hangs(_FakeGroupClient):
            def get_sandbox_client(self, sandbox_id: str):
                return _Hanging()

        backend = _backend_with(_Hangs())
        key = SandboxKey(scope="scope-a", thread_id="thread-1", agent_dir="devops-engineer")
        backend._registry[(key.scope, key.thread_id, key.agent_dir, key.call_id, "bicep")] = _Held(
            "sbx-1", egress=(Egress.CLOSED, frozenset())
        )

        async def cut_short() -> None:
            async with asyncio.timeout(0.05):
                await backend.dispose(key)

        with pytest.raises(TimeoutError):
            asyncio.run(cut_short())

        assert backend._registry == {}, "the registry entry is gone"
        assert backend._undeleted == {
            (key.scope, key.thread_id, key.agent_dir, key.call_id): {"sbx-1"}
        }

    def test_a_delete_that_lands_clears_the_retry_record(self):
        backend = _backend_with(_FakeGroupClient())
        key = SandboxKey(scope="scope-a", thread_id="thread-1", agent_dir="devops-engineer")
        backend._undeleted[(key.scope, key.thread_id, key.agent_dir, key.call_id)] = {"sbx-1"}

        assert asyncio.run(backend.dispose(key)) is None
        assert backend._undeleted == {}

    def test_a_scope_purge_that_lands_clears_the_retry_record(self):
        backend = _backend_with(_FakeGroupClient())
        backend._undeleted[("scope-a", "thread-1", "devops-engineer", "")] = {"sbx-1"}

        asyncio.run(backend.dispose_scope("scope-a", "thread-1"))
        assert backend._undeleted == {}

    def test_a_sandbox_the_service_no_longer_has_is_not_a_failure(self):
        """The auto-delete timer reclaiming one between rounds is the expected path — the same
        reading `acquire`'s resume takes. Reporting it would refuse the key over a sandbox that
        is already gone."""
        from azure.core.exceptions import ResourceNotFoundError

        class _Gone(_FakeGroupClient):
            def get_sandbox_client(self, sandbox_id: str):
                raise ResourceNotFoundError("sandbox not found")

        backend = _backend_with(_Gone())
        key = SandboxKey(scope="scope-a", thread_id="thread-1", agent_dir="devops-engineer")
        backend._registry[(key.scope, key.thread_id, key.agent_dir, key.call_id, "bicep")] = _Held(
            "sbx-1", egress=(Egress.CLOSED, frozenset())
        )

        assert asyncio.run(backend.dispose(key)) is None

    def test_a_group_client_that_cannot_be_built_is_reported_rather_than_raised(self):
        """The registry entries are already gone, so silence would strand a running sandbox
        with no record of it anywhere."""
        backend = AcasSandboxBackend(_config())
        key = SandboxKey(scope="scope-a", thread_id="thread-1", agent_dir="devops-engineer")
        backend._registry[(key.scope, key.thread_id, key.agent_dir, key.call_id, "bicep")] = _Held(
            "sbx-1", egress=(Egress.CLOSED, frozenset())
        )

        def _unreachable():
            raise RuntimeError("no credential")

        backend._group_client = _unreachable  # type: ignore[method-assign]
        reason = asyncio.run(backend.dispose(key))
        assert reason is not None
        assert reason.code == "unreachable", "no client was ever built"
        assert "no credential" in reason.detail

    def test_a_record_this_attempt_never_reported_on_is_not_read_as_landed(self):
        """A disposal still in flight writes its ids ahead of its own first await. Answering
        `None` here clears the router's refusal on the strength of a delete nobody confirmed."""
        release = asyncio.Event()
        prefix = ("scope-a", "thread-1", "devops-engineer", "")
        key = SandboxKey(scope="scope-a", thread_id="thread-1", agent_dir="devops-engineer")
        backend = _backend_with(_FakeGroupClient())
        backend._registry[("scope-a", "thread-1", "devops-engineer", "", "bicep")] = _Held(
            "sbx-1", egress=(Egress.CLOSED, frozenset())
        )
        original = backend._delete

        async def slow_delete(group_client, sandbox_id):
            await release.wait()
            return await original(group_client, sandbox_id)

        backend._delete = slow_delete  # type: ignore[method-assign]

        async def drive() -> DisposalFailure | None:
            disposal = asyncio.create_task(backend.dispose(key))
            await asyncio.sleep(0)
            backend._undeleted[prefix] = backend._undeleted.get(prefix, set()) | {"sbx-2"}
            release.set()
            return await disposal

        reported = asyncio.run(drive())
        assert backend._undeleted == {prefix: {"sbx-2"}}, "the newer record survives"
        assert reported is not None, "and the key stays refused until someone reports on it"
        assert reported.code == "unknown", "the other attempt's outcome is not ours to name"


# ---------------------------------------------------------------------------
# Lifecycle visibility — a sandbox started or reclaimed must leave a record
# ---------------------------------------------------------------------------


class TestLabelValues:
    """Label values must fit 63 characters, and mean the same thing on both sides.

    A live create failed with `400 … Label value for key 'scope' exceeds 63 characters`:
    an authenticated scope is `user-<base64url(provider:accountId)>`, which for an Entra id
    is 79. Anonymous scopes are short UUIDs, so the whole feature worked until someone
    signed in.
    """

    _LONG_SCOPE = "user-" + "bWljcm9zb2Z0LWVudHJhLWlkOjJiMWY5YTNjLTRkNWUtNGY2MC04YTcxLTlj"

    def test_short_values_are_left_readable(self):
        from maf_sandbox_acas._backend import _label_value

        assert _label_value("scope-a") == "scope-a"
        assert _label_value("x" * 63) == "x" * 63

    def test_long_values_are_digested_within_the_limit(self):
        from maf_sandbox_acas._backend import _LABEL_VALUE_MAX, _label_value

        out = _label_value("y" * 200)
        assert len(out) <= _LABEL_VALUE_MAX
        assert out.startswith("sha256-")

    def test_values_sharing_a_long_prefix_do_not_collide(self):
        """Truncation would map these together; these labels gate one user's purge."""
        from maf_sandbox_acas._backend import _label_value

        a = "user-" + "z" * 90 + "AAAA"
        b = "user-" + "z" * 90 + "BBBB"
        assert _label_value(a) != _label_value(b)

    def test_create_and_purge_agree_on_the_label(self):
        """The round trip: what acquire writes must be what dispose_scope queries.

        Applying the mapping on one side only would not raise — the listing would simply
        match nothing, and every sandbox for a deleted conversation would keep running.
        """
        from maf_sandbox import SandboxSpec

        from maf_sandbox_acas._backend import _LABEL_SCOPE, _LABEL_THREAD, _sandbox_labels

        key = SandboxKey(scope=self._LONG_SCOPE, thread_id="thread-1", agent_dir="devops")
        written = _sandbox_labels(key, SandboxSpec(kind="bicep", image="i:1"))

        client = _FakeGroupClient()
        backend = _backend_with(client)
        asyncio.run(backend.dispose_scope(self._LONG_SCOPE, "thread-1"))

        assert client.last_labels is not None
        assert client.last_labels[_LABEL_SCOPE] == written[_LABEL_SCOPE]
        assert client.last_labels[_LABEL_THREAD] == written[_LABEL_THREAD]

    def test_every_label_a_create_sends_is_within_the_limit(self):
        from maf_sandbox import SandboxSpec

        from maf_sandbox_acas._backend import _LABEL_VALUE_MAX, _sandbox_labels

        key = SandboxKey(scope=self._LONG_SCOPE, thread_id="t" * 120, agent_dir="a" * 90)
        labels = _sandbox_labels(key, SandboxSpec(kind="bicep", labels={"extra": "e" * 200}))

        oversized = {k: len(v) for k, v in labels.items() if len(v) > _LABEL_VALUE_MAX}
        assert oversized == {}, oversized


class TestLifecycleLogging:
    """Acquire and release must say what happened, at INFO.

    None of it is inferable from the tool's output: `bicep_validate` returns the same
    compiler diagnostics whether a warm sandbox was reused in a second or a cold sandbox was
    created in a minute, and a sandbox that is never released is billable but silent.
    The operator-facing question — was one created, was it used, was it released — has no
    other answer, so these lines are load-bearing rather than decoration.
    """

    def test_reuse_is_logged(self, caplog):
        client = _FakeGroupClient()
        backend = _backend_with(client)
        key = SandboxKey(scope="scope-a", thread_id="thread-1", agent_dir="devops-engineer")
        backend._registry[(key.scope, key.thread_id, key.agent_dir, key.call_id, "bicep")] = _Held(
            "sbx-warm", egress=(Egress.CLOSED, frozenset())
        )

        from maf_sandbox import SandboxSpec

        with caplog.at_level(logging.INFO, logger="maf_sandbox_acas"):
            asyncio.run(backend.acquire(key, SandboxSpec(kind="bicep", image="img:1")))

        assert any("sandbox reused" in r.getMessage() for r in caplog.records), caplog.text
        assert any("sbx-warm" in r.getMessage() for r in caplog.records)

    def test_release_is_logged(self, caplog):
        client = _FakeGroupClient()
        backend = _backend_with(client)
        key = SandboxKey(scope="scope-a", thread_id="thread-1", agent_dir="devops-engineer")
        backend._registry[(key.scope, key.thread_id, key.agent_dir, key.call_id, "bicep")] = _Held(
            "sbx-1", egress=(Egress.CLOSED, frozenset())
        )

        with caplog.at_level(logging.INFO, logger="maf_sandbox_acas"):
            asyncio.run(backend.dispose(key))

        assert any("sandbox released" in r.getMessage() for r in caplog.records), caplog.text

    def test_a_scope_purge_names_each_sandbox_it_deletes(self, caplog):
        client = _FakeGroupClient(sandboxes=[_FakeSandbox("sbx-a"), _FakeSandbox("sbx-b")])
        backend = _backend_with(client)

        with caplog.at_level(logging.INFO, logger="maf_sandbox_acas"):
            count = asyncio.run(backend.dispose_scope("scope-a", "thread-1")).disposed

        assert count == 2
        released = [r for r in caplog.records if "sandbox released" in r.getMessage()]
        assert len(released) == 2, caplog.text

    def test_failed_lifecycle_configuration_does_not_promise_auto_delete(self, caplog):
        class _CreatedSandbox(_FakeSandboxClient):
            async def set_lifecycle_policy(self, policy) -> None:
                raise RuntimeError("HTTP 400 invalid policy")

        class _Poller:
            async def result(self):
                return _CreatedSandbox("sbx-1")

        class _LifecycleFailsGroupClient:
            async def begin_create_sandbox(self, *, disk_id, labels, egress_policy):
                return _Poller()

        client = _LifecycleFailsGroupClient()
        backend = _backend_with(client)
        key = SandboxKey(scope="scope-a", thread_id="thread-1", agent_dir="devops-engineer")

        from maf_sandbox import SandboxSpec

        with caplog.at_level(logging.WARNING, logger="maf_sandbox_acas"):
            asyncio.run(backend.acquire(key, SandboxSpec(kind="bicep", image_id="pinned-id")))

        assert "no auto-delete timer was confirmed" in caplog.text
        assert "HTTP 400 invalid policy" in caplog.text
        assert "will be reclaimed by the auto-delete timer" not in caplog.text


# ---------------------------------------------------------------------------
# Concurrent acquire — two calls for one key
# ---------------------------------------------------------------------------


class _SlowCreateGroupClient:
    """A group client whose create yields, so two acquires really do interleave.

    ``peak_creates`` is how many creates were ever in flight together — the number that says
    whether the two calls were serialised or overlapped, which a create count alone cannot.
    """

    def __init__(self) -> None:
        self.create_calls = 0
        self.in_flight = 0
        self.peak_creates = 0
        self.resumed: list[str] = []

    def get_sandbox_client(self, sandbox_id: str):
        self.resumed.append(sandbox_id)
        return _ResumingSandboxClient(sandbox_id)

    async def begin_create_sandbox(self, *, disk_id, labels, egress_policy):
        self.create_calls += 1
        created = f"sbx-{self.create_calls}"
        client = self

        class _Poller:
            async def result(self):
                client.in_flight += 1
                client.peak_creates = max(client.peak_creates, client.in_flight)
                await asyncio.sleep(0)
                client.in_flight -= 1
                return _CreatedSandbox(created)

        return _Poller()


class _ResumingSandboxClient(_FakeSandboxClient):
    """Suspends while resuming, so a second acquire on a warm key waits on the lock."""

    async def ensure_running(self, timeout: float | None = None) -> None:
        await asyncio.sleep(0)
        await super().ensure_running(timeout)


class _CreatedSandbox(_FakeSandboxClient):
    async def set_lifecycle_policy(self, policy) -> None:
        await asyncio.sleep(0)


def _spec():
    from maf_sandbox import SandboxSpec

    return SandboxSpec(kind="bicep", image_id="pinned-id")


@pytest.mark.parametrize("listing_fails", [False, True])
@pytest.mark.parametrize("kind", [None, "bicep"])
def test_key_discovery_retains_cleanup_before_replacement(kind, listing_fails, monkeypatch):
    from maf_sandbox import SandboxOutputError

    client = _GuestGroupClient(_guest_removing(True), delete_fails=True)
    backend = _backend_with(client)
    key = SandboxKey("s", "t", "a")

    async def listed():
        yield _FakeSandbox("remote")
        if listing_fails:
            raise RuntimeError("listing interrupted")

    monkeypatch.setattr(client, "list_sandboxes", lambda **kwargs: listed())

    async def scenario():
        assert await backend.dispose(key, kind=kind) is not None
        for requested in ["bicep", "codeact"] if kind is None else [kind]:
            with pytest.raises(SandboxOutputError, match="retained"):
                await backend.acquire(key, SandboxSpec(kind=requested, image_id="pinned-id"))
        assert client.create_calls == 0
        assert backend._undeleted[("s", "t", "a", "")] == {"remote"}
        assert backend._undeleted_kinds.get(("s", "t", "a", ""), {}).get("remote") == kind
        client.delete_fails = False
        replacement = await backend.acquire(key, _spec())
        assert replacement.instance_id == "sbx-1" and client.deleted == ["remote"]
        assert not backend._undeleted

    asyncio.run(scenario())


@pytest.mark.parametrize("retry", ["acquire", "purge"])
@pytest.mark.parametrize("listing_fails", [False, True])
def test_scope_discovery_survives_failed_listing_and_blocks_its_scope(
    retry, listing_fails, monkeypatch
):
    from maf_sandbox import SandboxOutputError

    client = _GuestGroupClient(_guest_removing(True), delete_fails=True)
    backend = _backend_with(client)

    async def listed():
        yield _FakeSandbox("remote")
        if listing_fails:
            raise RuntimeError("listing interrupted")

    def offline(**kwargs):
        raise RuntimeError("listing offline")

    monkeypatch.setattr(client, "list_sandboxes", lambda **kwargs: listed())

    async def scenario():
        assert (await backend.dispose_scope("s", "t")).undisposed is not None
        monkeypatch.setattr(client, "list_sandboxes", offline)
        for agent, kind in [("a", "bicep"), ("b", "codeact")]:
            with pytest.raises(SandboxOutputError, match="retained scope"):
                await backend.acquire(
                    SandboxKey("s", "t", agent), SandboxSpec(kind=kind, image_id="pinned-id")
                )
        assert client.create_calls == 0
        for scope, thread in [("other", "t"), ("s", "other")]:
            await backend.acquire(SandboxKey(scope, thread, "a"), _spec())
        assert client.create_calls == 2
        client.delete_fails = False
        if retry == "purge":
            purged = await backend.dispose_scope("s", "t")
            assert purged.disposed == 1
            assert purged.undisposed is not None and purged.undisposed.code == "unlisted"
        replacement = await backend.acquire(SandboxKey("s", "t", "a"), _spec())
        assert replacement.instance_id == "sbx-3" and client.deleted == ["remote"]
        assert not backend._scope_disposals

    asyncio.run(scenario())


@pytest.mark.parametrize("scope_wide", [False, True])
def test_discovery_is_retained_when_listing_is_cancelled(scope_wide, monkeypatch):
    from maf_sandbox import SandboxOutputError

    client = _GuestGroupClient(_guest_removing(True), delete_fails=True)
    backend = _backend_with(client)
    key = SandboxKey("s", "t", "a")

    async def scenario():
        pending = asyncio.Event()

        async def listed():
            yield _FakeSandbox("remote")
            pending.set()
            await asyncio.Event().wait()

        monkeypatch.setattr(client, "list_sandboxes", lambda **kwargs: listed())
        task = asyncio.create_task(
            backend.dispose_scope("s", "t") if scope_wide else backend.dispose(key)
        )
        await pending.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        with pytest.raises(SandboxOutputError, match="retained"):
            await backend.acquire(key, _spec())
        assert client.create_calls == 0

    asyncio.run(scenario())


def test_older_scope_failure_cannot_restore_a_newer_completed_deletion(monkeypatch):
    client = _GuestGroupClient(_guest_removing(True))
    backend = _backend_with(client)
    monkeypatch.setattr(
        client, "list_sandboxes", lambda **kwargs: _FakePager([_FakeSandbox("remote")])
    )
    original_delete = _GuestSandboxClient.begin_delete

    async def scenario():
        started, release = asyncio.Event(), asyncio.Event()
        calls = 0

        async def delete(sandbox):
            nonlocal calls
            calls += 1
            if calls == 1:
                started.set()
                await release.wait()
                raise RuntimeError("old deletion failed")
            return await original_delete(sandbox)

        monkeypatch.setattr(_GuestSandboxClient, "begin_delete", delete)
        first = asyncio.create_task(backend.dispose_scope("s", "t"))
        await started.wait()
        assert (await backend.dispose_scope("s", "t")).undisposed is None
        release.set()
        assert (await first).undisposed is not None
        client.delete_fails = True
        result = await backend.acquire(SandboxKey("s", "t", "a"), _spec())
        assert result.instance_id == "sbx-1" and calls == 2
        assert not backend._scope_disposals

    asyncio.run(scenario())


def test_older_scope_success_cannot_clear_a_newer_failed_deletion(monkeypatch):
    from maf_sandbox import SandboxOutputError

    client = _GuestGroupClient(_guest_removing(True), delete_fails=True)
    backend = _backend_with(client)
    monkeypatch.setattr(
        client, "list_sandboxes", lambda **kwargs: _FakePager([_FakeSandbox("remote")])
    )
    original_delete = _GuestSandboxClient.begin_delete

    async def scenario():
        started, release = asyncio.Event(), asyncio.Event()
        calls = 0

        async def delete(sandbox):
            nonlocal calls
            calls += 1
            if calls == 1:
                started.set()
                await release.wait()
                return _CompletedDeletion()
            return await original_delete(sandbox)

        monkeypatch.setattr(_GuestSandboxClient, "begin_delete", delete)
        first = asyncio.create_task(backend.dispose_scope("s", "t"))
        await started.wait()
        second = await backend.dispose_scope("s", "t")
        assert second.undisposed is not None
        release.set()
        assert (await first).undisposed is not None
        with pytest.raises(SandboxOutputError, match="retained scope"):
            await backend.acquire(SandboxKey("s", "t", "a"), _spec())
        assert client.create_calls == 0
        client.delete_fails = False
        assert (await backend.acquire(SandboxKey("s", "t", "a"), _spec())).instance_id == "sbx-1"
        assert not backend._scope_disposals

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["exit", "exception"])
def test_fresh_capture_probe_failure_retries_deletion_before_replacement(failure, monkeypatch):
    from maf_sandbox import SandboxOutputError

    client = _GuestGroupClient(_guest_removing(True), delete_fails=True)
    backend = _backend_with(client)
    key = SandboxKey("s", "t", "a")
    spec = _spec()
    original_exec = _GuestSandboxClient.exec
    original_delete = _GuestSandboxClient.begin_delete
    attempts = []
    fail_probe = True

    async def exec_probe(self, command, *, working_directory):
        if fail_probe and "maf-exec-probe-" in command:
            if failure == "exception":
                raise OSError("capture unavailable")
            return SimpleNamespace(stdout="", stderr="capture unavailable", exit_code=125)
        return await original_exec(self, command, working_directory=working_directory)

    async def delete(self):
        attempts.append((self.sandbox_id, client.create_calls))
        return await original_delete(self)

    monkeypatch.setattr(_GuestSandboxClient, "exec", exec_probe)
    monkeypatch.setattr(_GuestSandboxClient, "begin_delete", delete)

    async def scenario():
        nonlocal fail_probe
        with pytest.raises(SandboxCapabilityNotSupported, match="exec-capture"):
            await backend.acquire(key, spec)
        assert not backend._registry
        assert backend._undeleted[("s", "t", "a", "")] == {"sbx-1"}
        assert attempts == [("sbx-1", 1), ("sbx-1", 1)]
        fail_probe = False
        with pytest.raises(SandboxOutputError, match="retained"):
            await backend.acquire(key, spec)
        assert client.create_calls == 1 and attempts[-1] == ("sbx-1", 1)
        client.delete_fails = False
        replacement = await backend.acquire(key, spec)
        assert replacement.instance_id == "sbx-2"
        assert attempts[-1] == ("sbx-1", 1)
        assert not backend._undeleted

    asyncio.run(scenario())


@pytest.mark.parametrize("disposal", ["key", "kind", "scope"])
@pytest.mark.parametrize("reuse", [False, True])
def test_exec_invalidation_after_failed_disposal_blocks_replacement(disposal, reuse, monkeypatch):
    from maf_sandbox import SandboxOutputError

    client = _GuestGroupClient(_guest_removing(True), delete_fails=True)
    backend = _backend_with(client)
    key = SandboxKey("s", "t", "a")
    spec = _spec()

    async def scenario():
        started, release = asyncio.Event(), asyncio.Event()

        async def capture(*args, **kwargs):
            started.set()
            await release.wait()
            raise SandboxOutputError("capture failed")

        monkeypatch.setattr(f"{_Held.__module__}.capture", capture)
        sandbox = await backend.acquire(key, spec)
        if reuse:
            sandbox = await backend.acquire(key, spec)
        task = asyncio.create_task(sandbox.exec("program", working_directory="/", timeout=10))
        await started.wait()
        if disposal == "scope":
            assert (await backend.dispose_scope(key.scope, key.thread_id)).undisposed is not None
        else:
            assert await backend.dispose(key, kind=spec.kind if disposal == "kind" else None)
        assert not backend._registry and not sandbox._held.unusable
        release.set()
        with pytest.raises(SandboxOutputError, match="capture failed"):
            await task
        with pytest.raises(SandboxOutputError, match="retained"):
            await backend.acquire(key, spec)
        assert client.create_calls == 1
        client.delete_fails = False
        replacement = await backend.acquire(key, spec)
        assert client.deleted[-1] == sandbox.instance_id
        assert replacement.instance_id != sandbox.instance_id
        assert not backend._undeleted

    asyncio.run(scenario())


@pytest.mark.parametrize("disposal", ["key", "kind", "scope"])
def test_reacquire_retries_invalidated_ids_retained_after_disposal(disposal, monkeypatch):
    from maf_sandbox import SandboxOutputError

    from maf_sandbox_acas._backend import _Deletion

    client = _SlowCreateGroupClient()
    backend = _backend_with(client)
    key = SandboxKey("s", "t", "a")
    spec = _spec()
    failed = True
    attempts = []

    async def delete(group, sandbox_id):
        attempts.append((sandbox_id, client.create_calls))
        return (
            _Deletion(False, DisposalFailure("unreachable", "offline"))
            if failed
            else _Deletion(True)
        )

    monkeypatch.setattr(client, "list_sandboxes", lambda **kwargs: _FakePager([]), raising=False)
    monkeypatch.setattr(backend, "_delete", delete)

    async def scenario():
        nonlocal failed
        first = await backend.acquire(key, spec)
        first._held.unusable = True
        if disposal == "scope":
            assert (await backend.dispose_scope(key.scope, key.thread_id)).undisposed is not None
        else:
            assert (
                await backend.dispose(key, kind=spec.kind if disposal == "kind" else None)
                is not None
            )
        assert not backend._registry
        assert (
            first.instance_id
            in backend._undeleted[(key.scope, key.thread_id, key.agent_dir, key.call_id)]
        )
        with pytest.raises(SandboxOutputError, match="retained"):
            await backend.acquire(key, spec)
        assert client.create_calls == 1
        assert len(attempts) == 2
        failed = False
        replacement = await backend.acquire(key, spec)
        assert replacement.instance_id != first.instance_id
        assert attempts[-1] == (first.instance_id, 1)
        assert client.create_calls == 2
        assert not backend._undeleted

    asyncio.run(scenario())


@pytest.mark.parametrize("stage", ["resume", "resume_failure", "probe", "prepare"])
def test_reacquire_refuses_invalidation_during_preparation(stage, monkeypatch):
    from maf_sandbox import SandboxOutputError

    from maf_sandbox_acas._backend import _AcasSandbox

    client = _SlowCreateGroupClient()
    backend = _backend_with(client)
    key = SandboxKey("s", "t", "a")
    spec = _spec()

    async def scenario():
        first = await backend.acquire(key, spec)
        ready, release = asyncio.Event(), asyncio.Event()
        owner, name = {
            "resume": (_ResumingSandboxClient, "ensure_running"),
            "resume_failure": (_ResumingSandboxClient, "ensure_running"),
            "probe": (backend, "_probe_commands"),
            "prepare": (_AcasSandbox, "prepare_work_dir"),
        }[stage]
        original = getattr(owner, name)

        async def pause(*args, **kwargs):
            result = await original(*args, **kwargs)
            ready.set()
            await release.wait()
            if stage == "resume_failure":
                raise RuntimeError("sandbox disappeared while resuming")
            return result

        with monkeypatch.context() as patch:
            patch.setattr(owner, name, pause)
            acquire = asyncio.create_task(backend.acquire(key, spec))
            await asyncio.wait_for(ready.wait(), 5)
            try:
                await first._invalidate_after_exec(SandboxOutputError("capture failed"))
            finally:
                release.set()
            with pytest.raises(SandboxOutputError, match="invalidated during acquire"):
                await acquire
        assert client.create_calls == 1
        replacement = await backend.acquire(key, spec)
        assert replacement.instance_id != first.instance_id

    asyncio.run(scenario())


def test_acquire_return_waits_for_invalidation_on_another_loop(monkeypatch):
    from maf_sandbox import SandboxOutputError

    from maf_sandbox_acas._backend import _AcasSandbox

    backend = _backend_with(_SlowCreateGroupClient())
    key, spec = SandboxKey("s", "t", "a"), _spec()
    first = asyncio.run(backend.acquire(key, spec))
    ready, finish_prepare = threading.Event(), threading.Event()
    invalidating, finish_invalidation = threading.Event(), threading.Event()
    result_waiting = threading.Event()
    role, lock = threading.local(), threading.Lock()

    class Guard:
        def __enter__(self):
            if role.name == "reader" and invalidating.is_set():
                result_waiting.set()
            assert lock.acquire(timeout=5)
            if role.name == "writer":
                invalidating.set()
                assert finish_invalidation.wait(5)

        def __exit__(self, *args):
            lock.release()

    original = _AcasSandbox.prepare_work_dir

    async def prepare(self, spec):
        await original(self, spec)
        ready.set()
        assert finish_prepare.wait(5)

    monkeypatch.setattr(_AcasSandbox, "prepare_work_dir", prepare)
    monkeypatch.setattr(first._held, "invalidation_guard", Guard())

    def acquire():
        role.name = "reader"
        return asyncio.run(backend.acquire(key, spec))

    def invalidate():
        role.name = "writer"
        asyncio.run(first._invalidate_after_exec(SandboxOutputError("capture failed")))

    with ThreadPoolExecutor(max_workers=2) as pool:
        result = pool.submit(acquire)
        assert ready.wait(5)
        cleanup = pool.submit(invalidate)
        try:
            assert invalidating.wait(5)
            finish_prepare.set()
            assert result_waiting.wait(3)
            assert not result.done()
        finally:
            finish_prepare.set()
            finish_invalidation.set()
        with pytest.raises(SandboxOutputError, match="invalidated during acquire"):
            result.result(timeout=5)
        assert cleanup.result(timeout=5) is None


@pytest.mark.parametrize("delete_failed", [False, True])
def test_capture_invalidation_allows_policy_change_after_deletion(delete_failed):
    from maf_sandbox import SandboxOutputError

    client = _GuestGroupClient(_guest_removing(True), delete_fails=delete_failed)
    backend = _backend_with(client)
    key = SandboxKey("s", "t", "a")
    original = _spec()
    changed = replace(original, egress=Egress.ALLOWLIST, egress_allow=("api.example",))

    async def scenario():
        first = await backend.acquire(key, original)
        await first._invalidate_after_exec(SandboxOutputError("capture failed"))
        assert first._held.unusable
        if delete_failed:
            with pytest.raises(SandboxOutputError, match="dispose an invalidated sandbox"):
                await backend.acquire(key, changed)
            assert client.create_calls == 1
            assert backend._registry[("s", "t", "a", "", original.kind)] is first._held
            client.delete_fails = False
        replacement = await backend.acquire(key, changed)
        assert first.instance_id in client.deleted
        assert replacement.instance_id != first.instance_id
        assert replacement._held.egress == (Egress.ALLOWLIST, frozenset({"api.example"}))
        assert client.create_calls == 2

    asyncio.run(scenario())


@pytest.mark.parametrize("operation", ["key", "kind"])
@pytest.mark.parametrize("older_failed", [False, True])
@pytest.mark.parametrize("newer_failed", [False, True])
def test_invalidated_acquire_reconciles_concurrent_disposal(
    operation, older_failed, newer_failed, monkeypatch
):
    from maf_sandbox import SandboxOutputError

    from maf_sandbox_acas._backend import _Deletion

    client = _GuestGroupClient(_guest_removing(True), delete_fails=True)
    backend = _backend_with(client)
    key, spec = SandboxKey("s", "t", "a"), _spec()
    prefix = (key.scope, key.thread_id, key.agent_dir, key.call_id)
    original_delete = backend._delete
    calls = 0

    async def scenario():
        first = await backend.acquire(key, spec)
        await first._invalidate_after_exec(SandboxOutputError("capture failed"))
        client.delete_fails = False
        started, release = asyncio.Event(), asyncio.Event()

        async def delete(group, sandbox_id):
            nonlocal calls
            calls += 1
            attempt = calls
            assert sandbox_id == first.instance_id
            if attempt == 1:
                started.set()
                await release.wait()
            if (attempt == 1 and older_failed) or (attempt == 2 and newer_failed):
                return _Deletion(False, DisposalFailure("unreachable", "deletion failed"))
            return await original_delete(group, sandbox_id)

        monkeypatch.setattr(backend, "_delete", delete)
        acquire = asyncio.create_task(backend.acquire(key, spec))
        await asyncio.wait_for(started.wait(), 5)
        try:
            failure = await backend.dispose(key, kind=spec.kind if operation == "kind" else None)
            assert (failure is not None) == newer_failed
        finally:
            release.set()
        if older_failed or newer_failed:
            message = "invalidated sandbox" if older_failed else "retained disposal"
            with pytest.raises(SandboxOutputError, match=message):
                await acquire
            assert client.create_calls == 1
        else:
            assert (await acquire).instance_id != first.instance_id
        assert calls == 2
        assert backend._undeleted.get(prefix, set()) == (
            {first.instance_id} if newer_failed else set()
        )
        replacement = await backend.acquire(key, spec)
        assert replacement.instance_id != first.instance_id and client.create_calls == 2
        assert calls == (3 if newer_failed else 2)
        assert not backend._undeleted and not backend._disposal_tokens

    asyncio.run(asyncio.wait_for(scenario(), timeout=10))


@pytest.mark.parametrize("stage", ["create", "resume", "prepare"])
def test_scope_purge_reports_active_acquisition(stage, monkeypatch):
    from maf_sandbox_acas._backend import _AcasSandbox

    client = _GuestGroupClient(_guest_removing(True))
    backend = _backend_with(client)
    key, spec = SandboxKey("s", "t", "a"), _spec()

    async def scenario():
        if stage != "create":
            await backend.acquire(key, spec)
        owner, name = {
            "create": (client, "begin_create_sandbox"),
            "resume": (_GuestSandboxClient, "ensure_running"),
            "prepare": (_AcasSandbox, "prepare_work_dir"),
        }[stage]
        original = getattr(owner, name)
        started, release = asyncio.Event(), asyncio.Event()

        async def pause(*args, **kwargs):
            result = await original(*args, **kwargs)
            started.set()
            await release.wait()
            return result

        with monkeypatch.context() as patch:
            patch.setattr(owner, name, pause)
            acquire = asyncio.create_task(backend.acquire(key, spec))
            await asyncio.wait_for(started.wait(), 5)
            try:
                purge = await backend.dispose_scope(key.scope, key.thread_id)
                assert purge.disposed == 0 and purge.undisposed is not None
                assert purge.undisposed.code == "unknown"
                assert not client.deleted
            finally:
                release.set()
            result = await acquire
        purge = await backend.dispose_scope(key.scope, key.thread_id)
        assert purge.undisposed is None and purge.disposed == 1
        assert client.deleted == [result.instance_id]

    asyncio.run(asyncio.wait_for(scenario(), timeout=10))


@pytest.mark.parametrize("stage", ["listing", "deletion"])
def test_scope_purge_refuses_new_acquires_without_blocking_other_scopes(stage, monkeypatch):
    from maf_sandbox import SandboxOutputError

    client = _GuestGroupClient(_guest_removing(True))
    backend = _backend_with(client)
    key, spec = SandboxKey("s", "t", "a"), _spec()

    async def scenario():
        if stage == "deletion":
            await backend.acquire(key, spec)
        name = "_list_thread_sandbox_ids" if stage == "listing" else "_delete"
        original = getattr(backend, name)
        started, release = asyncio.Event(), asyncio.Event()

        async def pause(*args, **kwargs):
            result = await original(*args, **kwargs)
            started.set()
            await release.wait()
            return result

        with monkeypatch.context() as patch:
            patch.setattr(backend, name, pause)
            purge = asyncio.create_task(backend.dispose_scope(key.scope, key.thread_id))
            await asyncio.wait_for(started.wait(), 5)
            try:
                creates = client.create_calls
                for scoped_key, scoped_spec in (
                    (key, spec),
                    (replace(key, agent_dir="other"), spec),
                    (key, replace(spec, kind="other")),
                ):
                    with pytest.raises(SandboxOutputError, match="scope disposal is in progress"):
                        await backend.acquire(scoped_key, scoped_spec)
                assert client.create_calls == creates
                for other in (replace(key, scope="other"), replace(key, thread_id="other")):
                    assert (await backend.acquire(other, spec)).instance_id
            finally:
                release.set()
            assert (await purge).undisposed is None
        assert (await backend.acquire(key, spec)).instance_id

    asyncio.run(asyncio.wait_for(scenario(), timeout=10))


def test_scope_purge_refuses_acquire_on_another_event_loop(monkeypatch):
    from maf_sandbox import SandboxOutputError

    backend = _backend_with(_GuestGroupClient(_guest_removing(True)))
    key = SandboxKey("s", "t", "a")
    started, release = threading.Event(), threading.Event()

    async def listed(*args, **kwargs):
        started.set()
        assert release.wait(5)
        return []

    monkeypatch.setattr(backend, "_list_thread_sandbox_ids", listed)
    with ThreadPoolExecutor(max_workers=1) as pool:
        purge = pool.submit(asyncio.run, backend.dispose_scope(key.scope, key.thread_id))
        try:
            assert started.wait(5)
            with pytest.raises(SandboxOutputError, match="scope disposal is in progress"):
                asyncio.run(backend.acquire(key, _spec()))
        finally:
            release.set()
        assert purge.result(timeout=5).undisposed is None
    assert asyncio.run(backend.acquire(key, _spec())).instance_id


@pytest.mark.parametrize("first_end", ["success", "failure", "cancel"])
def test_overlapping_scope_purges_keep_admission_closed_until_both_finish(first_end, monkeypatch):
    from maf_sandbox import SandboxOutputError

    backend = _backend_with(_GuestGroupClient(_guest_removing(True)))
    key = SandboxKey("s", "t", "a")

    async def scenario():
        started = [asyncio.Event(), asyncio.Event()]
        release = [asyncio.Event(), asyncio.Event()]
        calls = 0

        async def listed(*args, **kwargs):
            nonlocal calls
            index = calls
            calls += 1
            started[index].set()
            await release[index].wait()
            return None if index == 0 and first_end == "failure" else []

        monkeypatch.setattr(backend, "_list_thread_sandbox_ids", listed)
        first = asyncio.create_task(backend.dispose_scope(key.scope, key.thread_id))
        await asyncio.wait_for(started[0].wait(), 5)
        second = asyncio.create_task(backend.dispose_scope(key.scope, key.thread_id))
        await asyncio.wait_for(started[1].wait(), 5)
        try:
            if first_end == "cancel":
                first.cancel()
            release[0].set()
            outcome = (await asyncio.gather(first, return_exceptions=True))[0]
            if first_end == "cancel":
                assert isinstance(outcome, asyncio.CancelledError)
            else:
                assert isinstance(outcome, ScopePurge)
                assert (outcome.undisposed is not None) == (first_end == "failure")
            with pytest.raises(SandboxOutputError, match="scope disposal is in progress"):
                await backend.acquire(key, _spec())
        finally:
            release[1].set()
        assert (await second).undisposed is None
        assert (await backend.acquire(key, _spec())).instance_id

    asyncio.run(asyncio.wait_for(scenario(), timeout=10))


class TestConcurrentAcquire:
    """Get-or-create is serialised per key, because a create cannot be made idempotent here.

    ``begin_create_sandbox`` names no sandbox, so the service has nothing to recognise a
    duplicate by. Two acquires that both miss the registry each get a running, billable sandbox,
    and only the second one to finish stays registered — the first is left with no handle in
    this process. The model reaching this is not exotic: the function calls in one assistant
    message are executed concurrently, so one message naming a key twice runs the tool body
    twice over.
    """

    def test_two_acquires_for_one_key_create_one_sandbox(self):
        client = _SlowCreateGroupClient()
        backend = _backend_with(client)
        key = SandboxKey(scope="scope-a", thread_id="thread-1", agent_dir="devops-engineer")

        async def both():
            return await asyncio.gather(
                backend.acquire(key, _spec()), backend.acquire(key, _spec())
            )

        first, second = asyncio.run(both())

        assert client.create_calls == 1
        assert client.peak_creates == 1
        assert first.sandbox_id == second.sandbox_id == "sbx-1"
        assert first.instance_id == second.instance_id == "sbx-1"
        assert backend._registry == {
            ("scope-a", "thread-1", "devops-engineer", "", "bicep"): _Held(
                "sbx-1", egress=(Egress.CLOSED, frozenset()), commands={"sh", "exec-capture"}
            )
        }

    def test_a_second_key_is_not_held_up_behind_the_first(self):
        """Per key, not one lock for the backend — two conversations must not serialise."""
        client = _SlowCreateGroupClient()
        backend = _backend_with(client)

        async def both():
            return await asyncio.gather(
                backend.acquire(
                    SandboxKey(scope="s", thread_id="thread-1", agent_dir="devops"), _spec()
                ),
                backend.acquire(
                    SandboxKey(scope="s", thread_id="thread-2", agent_dir="devops"), _spec()
                ),
            )

        first, second = asyncio.run(both())

        assert client.peak_creates == 2
        assert {first.sandbox_id, second.sandbox_id} == {"sbx-1", "sbx-2"}

    def test_a_second_event_loop_can_wait_on_the_same_key(self):
        """Successive event loops can each contend for the same key."""
        client = _SlowCreateGroupClient()
        backend = _backend_with(client)
        key = SandboxKey(scope="scope-a", thread_id="thread-1", agent_dir="devops-engineer")

        async def both():
            return await asyncio.gather(
                backend.acquire(key, _spec()), backend.acquire(key, _spec())
            )

        asyncio.run(both())
        asyncio.run(both())

        assert client.create_calls == 1

    @pytest.mark.parametrize("different_policy", [False, True])
    def test_overlapping_event_loops_do_not_create_competing_sandboxes(self, different_policy):
        creating, attempted, release = (threading.Event() for _ in range(3))

        class PausedCreate(_SlowCreateGroupClient):
            async def begin_create_sandbox(self, **kwargs):
                poller = await super().begin_create_sandbox(**kwargs)

                class PausedPoller:
                    async def result(self):
                        creating.set()
                        assert await asyncio.to_thread(release.wait, 5)
                        return await poller.result()

                return PausedPoller()

        client = PausedCreate()
        backend = _backend_with(client)
        key = SandboxKey("s", "t", "a")
        original = replace(_spec(), egress=Egress.ALLOWLIST, egress_allow=("api.example",))
        changed = replace(original, egress=Egress.CLOSED, egress_allow=())

        def acquire(spec, signal=None):
            async def run():
                task = asyncio.create_task(backend.acquire(key, spec))
                if signal is not None:
                    await asyncio.sleep(0)
                    signal.set()
                try:
                    return await task
                except AcasEgressPolicyConflict as conflict:
                    return conflict

            return asyncio.run(run())

        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(acquire, original)
            try:
                assert creating.wait(5)
                second = pool.submit(acquire, changed if different_policy else original, attempted)
                assert attempted.wait(5)
                concurrent_creates = client.create_calls
            finally:
                release.set()
            original_sandbox = first.result(timeout=5)
            other = second.result(timeout=5)

        assert concurrent_creates == client.create_calls == 1
        assert not isinstance(original_sandbox, AcasEgressPolicyConflict)
        if different_policy:
            assert isinstance(other, AcasEgressPolicyConflict)
        else:
            assert not isinstance(other, AcasEgressPolicyConflict)
            assert original_sandbox.instance_id == other.instance_id
        held = backend._registry[("s", "t", "a", "", original.kind)]
        assert held.sandbox_id == original_sandbox.instance_id
        assert held.egress == (Egress.ALLOWLIST, frozenset({"api.example"}))

    def test_cancelling_a_waiter_preserves_exclusion_for_other_waiters(self):
        backend = _backend_with(_SlowCreateGroupClient())
        identity = ("s", "t", "a", "", "kind")
        entered = []

        async def wait_for_owner(name):
            async with backend._acquire_lock(identity):
                entered.append(name)

        async def scenario():
            async with backend._acquire_lock(identity):
                cancelled = asyncio.create_task(wait_for_owner("cancelled"))
                follower = asyncio.create_task(wait_for_owner("follower"))
                await asyncio.sleep(0)
                cancelled.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await cancelled
                await asyncio.sleep(0)
                assert not follower.done()
                assert entered == []
            await asyncio.wait_for(follower, timeout=1)
            assert entered == ["follower"]
            assert backend._acquisitions == {}

        asyncio.run(scenario())

    @pytest.mark.parametrize("cancel_owner", [False, True])
    def test_an_interrupted_owner_releases_waiters(self, cancel_owner):
        backend = _backend_with(_SlowCreateGroupClient())
        identity = ("s", "t", "a", "", "kind")

        async def scenario():
            entered = asyncio.Event()
            fail = asyncio.Event()

            async def owner():
                async with backend._acquire_lock(identity):
                    entered.set()
                    await fail.wait()
                    raise RuntimeError("create failed")

            async def follower():
                async with backend._acquire_lock(identity):
                    return "acquired"

            task = asyncio.create_task(owner())
            await entered.wait()
            waiting = asyncio.create_task(follower())
            await asyncio.sleep(0)
            if cancel_owner:
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                fail.set()
                with pytest.raises(RuntimeError, match="create failed"):
                    await task
            assert await asyncio.wait_for(waiting, timeout=1) == "acquired"
            assert backend._acquisitions == {}

        asyncio.run(scenario())


class TestKindIdentity:
    """A sandbox belongs to (key, kind): two kinds on one agent never share one (#84)."""

    def test_two_kinds_on_one_key_create_two_sandboxes(self):
        from maf_sandbox import SandboxSpec

        client = _SlowCreateGroupClient()
        backend = _backend_with(client)
        key = SandboxKey(scope="scope-a", thread_id="thread-1", agent_dir="devops-engineer")

        async def one_after_the_other():
            first = await backend.acquire(key, SandboxSpec(kind="bicep", image_id="pinned-id"))
            second = await backend.acquire(key, SandboxSpec(kind="codeact", image_id="pinned-id"))
            return first, second

        first, second = asyncio.run(one_after_the_other())

        assert client.create_calls == 2
        assert first.sandbox_id != second.sandbox_id
        assert first.instance_id != second.instance_id
        assert ("scope-a", "thread-1", "devops-engineer", "", "bicep") in backend._registry
        assert ("scope-a", "thread-1", "devops-engineer", "", "codeact") in backend._registry

    def test_the_kind_label_is_written_at_create(self):
        from maf_sandbox import SandboxSpec

        from maf_sandbox_acas._backend import _LABEL_KIND, _sandbox_labels

        key = SandboxKey(scope="scope-a", thread_id="thread-1", agent_dir="devops-engineer")
        labels = _sandbox_labels(key, SandboxSpec(kind="bicep", image="i:1"))

        assert labels[_LABEL_KIND] == "bicep"

    def test_dispose_reclaims_every_kind_for_the_key(self):
        client = _FakeGroupClient()
        backend = _backend_with(client)
        key = SandboxKey(scope="scope-a", thread_id="thread-1", agent_dir="devops-engineer")
        backend._registry[(key.scope, key.thread_id, key.agent_dir, key.call_id, "bicep")] = _Held(
            "sbx-b", egress=(Egress.CLOSED, frozenset())
        )
        backend._registry[(key.scope, key.thread_id, key.agent_dir, key.call_id, "codeact")] = (
            _Held("sbx-c", egress=(Egress.CLOSED, frozenset()))
        )

        asyncio.run(backend.dispose(key))

        assert sorted(client.deleted) == ["sbx-b", "sbx-c"]
        assert backend._registry == {}


# ---------------------------------------------------------------------------
# error_detail adoption — the warning logs must carry status and body
# ---------------------------------------------------------------------------


class _HttpError(Exception):
    """Shaped like an azure-core `HttpResponseError`, without importing azure-core."""

    status_code = 400

    def __str__(self) -> str:
        return "Operation returned an invalid status 'Bad Request'"

    class response:  # noqa: N801 - mimics the SDK's attribute shape
        @staticmethod
        def text() -> str:
            return '{"error":"principal lacks a role on sandbox group acas-x"}'


class TestErrorDetailAdoption:
    """The resume, delete and list warning paths used to log a bare `%s` of the exception —
    `str()` on an azure-core error drops the response body, the exact gap `error_detail`
    exists to close.  These pin that the enriched detail actually reaches the log, and that
    the format strings the guardrail requires stay byte-identical (see the assertions on the
    literal template below each one)."""

    def test_resume_failure_logs_status_and_body(self, caplog):
        class _ResumeFailsSandboxClient:
            def __init__(self, sandbox_id: str) -> None:
                self.sandbox_id = sandbox_id

            async def ensure_running(self, timeout: float | None = None) -> None:
                raise _HttpError()

        class _CreatedSandbox(_FakeSandboxClient):
            async def set_lifecycle_policy(self, policy) -> None:
                return None

        class _Poller:
            async def result(self):
                return _CreatedSandbox("sbx-new")

        class _ResumeFailsGroupClient:
            def __init__(self) -> None:
                self.create_calls = 0

            def get_sandbox_client(self, sandbox_id: str):
                return _ResumeFailsSandboxClient(sandbox_id)

            async def begin_create_sandbox(self, *, disk_id, labels, egress_policy):
                self.create_calls += 1
                return _Poller()

        client = _ResumeFailsGroupClient()
        backend = _backend_with(client)
        key = SandboxKey(scope="scope-a", thread_id="thread-1", agent_dir="devops-engineer")
        backend._registry[(key.scope, key.thread_id, key.agent_dir, key.call_id, "bicep")] = _Held(
            "sbx-warm", egress=(Egress.CLOSED, frozenset())
        )

        from maf_sandbox import SandboxSpec

        with caplog.at_level(logging.INFO, logger="maf_sandbox_acas"):
            asyncio.run(backend.acquire(key, SandboxSpec(kind="bicep", image_id="pinned-id")))

        assert "status=400" in caplog.text, caplog.text
        assert "principal lacks a role" in caplog.text, caplog.text
        assert client.create_calls == 1
        resumed = [r for r in caplog.records if "did not resume" in r.getMessage()]
        assert len(resumed) == 1
        # The format string itself — unchanged. Only the argument grew richer.
        assert resumed[0].msg == "sandbox %s did not resume (%s); creating a replacement"

    def test_delete_failure_logs_status_and_body(self, caplog):
        class _FailingSandboxClient:
            async def begin_delete(self) -> None:
                raise _HttpError()

        class _DeleteFailsGroupClient:
            def get_sandbox_client(self, sandbox_id: str):
                return _FailingSandboxClient()

        backend = _backend_with(_DeleteFailsGroupClient())
        key = SandboxKey(scope="scope-a", thread_id="thread-1", agent_dir="devops-engineer")
        backend._registry[(key.scope, key.thread_id, key.agent_dir, key.call_id, "bicep")] = _Held(
            "sbx-1", egress=(Egress.CLOSED, frozenset())
        )

        with caplog.at_level(logging.WARNING, logger="maf_sandbox_acas"):
            asyncio.run(backend.dispose(key))

        assert "status=400" in caplog.text, caplog.text
        assert "principal lacks a role" in caplog.text, caplog.text
        failed = [r for r in caplog.records if "failed to delete sandbox" in r.getMessage()]
        assert len(failed) == 1
        assert failed[0].msg == "acas backend: failed to delete sandbox %s: %s"

    def test_list_failure_logs_status_and_body(self, caplog):
        class _ListFailsGroupClient:
            def list_sandboxes(self, *, labels=None):
                raise _HttpError()

        backend = _backend_with(_ListFailsGroupClient())

        with caplog.at_level(logging.WARNING, logger="maf_sandbox_acas"):
            asyncio.run(backend.dispose_scope("scope-a", "thread-1"))

        assert "status=400" in caplog.text, caplog.text
        assert "principal lacks a role" in caplog.text, caplog.text
        failed = [
            r for r in caplog.records if "could not list sandboxes for thread" in r.getMessage()
        ]
        assert len(failed) == 1
        assert failed[0].msg == "acas backend: could not list sandboxes for thread %s: %s"


class TestEgressPolicy:
    def test_a_held_sandbox_cannot_omit_its_policy(self):
        with pytest.raises(TypeError, match="egress"):
            _Held("unattributed")  # pyright: ignore[reportCallIssue]

    def test_router_distinguishes_policy_conflict_from_unsupported_mode(self):
        client = _SlowCreateGroupClient()
        backend = _backend_with(client)
        router = SandboxRouter([backend])
        key = SandboxKey("s", "t", "a")
        original = _spec()
        changed = replace(original, egress=Egress.ALLOWLIST, egress_allow=("api.example",))

        async def scenario():
            await backend.acquire(key, original)
            with pytest.raises(AcasEgressPolicyConflict) as conflict:
                await router.acquire(key, changed)
            assert isinstance(conflict.value, SandboxEgressNotEnforced)
            with pytest.raises(SandboxEgressNotEnforced) as unsupported:
                router.ensure_can_serve(replace(original, egress=Egress.UNRESTRICTED))
            assert not isinstance(unsupported.value, AcasEgressPolicyConflict)

        asyncio.run(scenario())

    @pytest.mark.parametrize("methods", [("GET",), ("POST", "PUT"), ("PROPFIND",)])
    def test_method_policy_refuses_before_reaching_the_service(self, methods, monkeypatch):
        backend = AcasSandboxBackend(_config())
        monkeypatch.setattr(backend, "_group_client", lambda: pytest.fail("contacted service"))
        scoped = SandboxSpec(
            kind="t",
            egress=Egress.ALLOWLIST,
            egress_allow=(EgressRule("api.example", methods),),
        )
        router = SandboxRouter([backend])
        assert Capability.EGRESS_METHODS not in backend.declarations.capabilities
        for acquire in (backend.acquire, router.acquire):
            with pytest.raises(SandboxCapabilityNotSupported):
                asyncio.run(acquire(SandboxKey("s", "t", "a"), scoped))
        with pytest.raises(SandboxCapabilityNotSupported):
            router.ensure_can_serve(scoped)
        with pytest.raises(SandboxCapabilityNotSupported):
            backend._egress_policy(scoped)

    def test_equivalent_host_policies_reuse_the_same_instance(self):
        client = _SlowCreateGroupClient()
        backend = _backend_with(client)
        key = SandboxKey("s", "t", "a")
        first = replace(
            _spec(), egress=Egress.ALLOWLIST, egress_allow=("api.example", "other.example")
        )
        equivalent = replace(
            first, egress_allow=("OTHER.example", EgressRule("API.example"), "api.example")
        )

        async def scenario():
            original = await backend.acquire(key, first)
            warm = await backend.acquire(key, equivalent)
            assert original.instance_id == warm.instance_id
            assert client.create_calls == 1

        asyncio.run(scenario())

    @pytest.mark.parametrize(
        ("first_mode", "first_hosts", "next_mode", "next_hosts"),
        [
            (Egress.ALLOWLIST, ("a.example",), Egress.ALLOWLIST, ("b.example",)),
            (Egress.ALLOWLIST, ("a.example",), Egress.ALLOWLIST, ("a.example", "b.example")),
            (Egress.ALLOWLIST, ("a.example", "b.example"), Egress.ALLOWLIST, ("a.example",)),
            (Egress.ALLOWLIST, ("a.example",), Egress.CLOSED, ()),
            (Egress.CLOSED, (), Egress.ALLOWLIST, ("a.example",)),
            (Egress.ALLOWLIST, (), Egress.CLOSED, ()),
        ],
    )
    def test_changed_policy_refuses_without_touching_the_original(
        self, first_mode, first_hosts, next_mode, next_hosts
    ):
        client = _SlowCreateGroupClient()
        backend = _backend_with(client)
        key = SandboxKey("s", "t", "a")
        original = replace(_spec(), egress=first_mode, egress_allow=first_hosts)
        changed = replace(original, egress=next_mode, egress_allow=next_hosts)

        async def scenario():
            first = await backend.acquire(key, original)
            held = backend._registry[("s", "t", "a", "", original.kind)]
            with pytest.raises(AcasEgressPolicyConflict, match="dispose_kind"):
                await backend.acquire(key, changed)
            assert backend._registry[("s", "t", "a", "", original.kind)] is held
            assert not client.resumed and client.create_calls == 1
            assert (await backend.acquire(key, original)).instance_id == first.instance_id

        asyncio.run(scenario())

    def test_concurrent_different_policies_do_not_share_or_replace_an_instance(self):
        client = _SlowCreateGroupClient()
        backend = _backend_with(client)
        key = SandboxKey("s", "t", "a")
        first = replace(_spec(), egress=Egress.ALLOWLIST, egress_allow=("a.example",))
        second = replace(first, egress_allow=("b.example",))

        async def scenario():
            results = await asyncio.gather(
                backend.acquire(key, first), backend.acquire(key, second), return_exceptions=True
            )
            assert sum(isinstance(r, SandboxEgressNotEnforced) for r in results) == 1
            assert sum(hasattr(r, "instance_id") for r in results) == 1
            assert client.create_calls == 1

        asyncio.run(scenario())

    @pytest.mark.parametrize("delete_failed", [False, True])
    def test_explicit_disposal_requires_cleanup_before_a_new_policy(
        self, monkeypatch, delete_failed
    ):
        from maf_sandbox import SandboxOutputError

        client = _SlowCreateGroupClient()
        backend = _backend_with(client)
        key = SandboxKey("s", "t", "a")
        original = replace(_spec(), egress=Egress.ALLOWLIST, egress_allow=("a.example",))
        closed = replace(original, egress=Egress.CLOSED, egress_allow=())
        from maf_sandbox_acas._backend import _Deletion

        async def failure(group, sandbox_id):
            if delete_failed:
                return _Deletion(False, DisposalFailure("unreachable", "offline"))
            return _Deletion(True)

        monkeypatch.setattr(
            client, "list_sandboxes", lambda **kwargs: _FakePager([]), raising=False
        )
        monkeypatch.setattr(backend, "_delete", failure)

        async def scenario():
            nonlocal delete_failed
            first = await backend.acquire(key, original)
            with pytest.raises(SandboxEgressNotEnforced):
                await backend.acquire(key, closed)
            assert (await backend.dispose(key, kind=original.kind) is not None) == delete_failed
            assert (
                first.instance_id in backend._undeleted.get(("s", "t", "a", ""), set())
            ) == delete_failed
            if delete_failed:
                with pytest.raises(SandboxOutputError, match="retained"):
                    await backend.acquire(key, closed)
                assert client.create_calls == 1
                delete_failed = False
            replacement = await backend.acquire(key, closed)
            assert replacement.instance_id != first.instance_id
            assert not backend._undeleted
            assert client.create_calls == 2

        asyncio.run(scenario())

    def test_denies_by_default_and_allows_only_the_named_hosts(self):
        """Patterns pass through verbatim — including wildcards, which the Bicep spec's
        `*.data.mcr.microsoft.com` (MCR's blob endpoint) depends on."""
        from maf_sandbox import SandboxSpec

        backend = AcasSandboxBackend(_config())
        policy = backend._egress_policy(
            SandboxSpec(
                kind="t",
                egress=Egress.ALLOWLIST,
                egress_allow=("mcr.microsoft.com", "*.data.mcr.microsoft.com"),
            )
        )

        assert policy.default_action == "Deny"
        assert [r.pattern for r in policy.host_rules] == [
            "mcr.microsoft.com",
            "*.data.mcr.microsoft.com",
        ]
        assert [r.action for r in policy.host_rules] == ["Allow", "Allow"]

    def test_an_empty_allowlist_means_no_network(self):
        from maf_sandbox import SandboxSpec

        backend = AcasSandboxBackend(_config())
        policy = backend._egress_policy(SandboxSpec(kind="t"))

        assert policy.default_action == "Deny"
        assert policy.host_rules == []


# ---------------------------------------------------------------------------
# The pull surface — FILES_OUT and FILES_LIST
# ---------------------------------------------------------------------------

_WORK_DIR = "/maf-sandbox/work"
_SBX_PATH = "/subscriptions/sub/resourceGroups/rg/sandboxGroups/grp/sandboxes/sbx-1"
_API_VERSION = "2026-02-01-preview"

#: What `/etc/hostname` holds inside the guest — the file a symlink out of the working
#: directory reaches, and the bytes no read may ever return.
_HOSTNAME = b"a-real-host\n"
_REAL_CONTENT = b"hello world\n"

# The stat payloads a live sandbox group answered with, verbatim, on
# `azure-containerapps-sandbox` 0.1.0b4. Copied rather than constructed because this backend
# reads these fields itself instead of through the SDK's `FileInfo` (#136): a preview SDK or a
# service that renames one has to fail against these rather than silently degrade the
# confinement rule to nothing. Note `size` on the symlink — 13 is the length of
# `/etc/hostname`, the target *string*, not of anything readable.
_LIVE_REGULAR = {
    "name": "real.txt",
    "path": "/maf-sandbox/work/real.txt",
    "size": 12,
    "mode": 420,
    "isDir": False,
    "isSymlink": False,
    "modifiedTime": 1786404028,
}
_LIVE_SYMLINK = {
    "name": "link-out.txt",
    "path": "/maf-sandbox/work/link-out.txt",
    "size": 13,
    "mode": 511,
    "isDir": False,
    "isSymlink": True,
    "symlinkTarget": "/etc/hostname",
    "modifiedTime": 1786404028,
}
_LIVE_DIRECTORY = {
    "name": "sub",
    "path": "/maf-sandbox/work/sub",
    "size": 4096,
    "mode": 493,
    "isDir": True,
    "isSymlink": False,
    "modifiedTime": 1786404028,
}
_LIVE_NESTED = {
    "name": "child.txt",
    "path": "/maf-sandbox/work/sub/child.txt",
    "size": 5,
    "mode": 420,
    "isDir": False,
    "isSymlink": False,
    "modifiedTime": 1786404028,
}
_LIVE_WORK_DIR = {
    "name": "work",
    "path": _WORK_DIR,
    "size": 4096,
    "mode": 493,
    "isDir": True,
    "isSymlink": False,
    "modifiedTime": 1786404028,
}
_LIVE_MAFSANDBOX = {
    "name": "maf-sandbox",
    "path": "/maf-sandbox",
    "size": 4096,
    "mode": 493,
    "isDir": True,
    "isSymlink": False,
    "modifiedTime": 1786404028,
}
#: `ln -sfn /etc /maf-sandbox/work/out` inside the guest. `size` is 4 — the length of `/etc`, the target
#: *string*. The link itself types as OTHER; a path *through* it carries nothing that says so.
_LIVE_SYMLINK_DIR = {
    "name": "out",
    "path": "/maf-sandbox/work/out",
    "size": 4,
    "mode": 511,
    "isDir": False,
    "isSymlink": True,
    "symlinkTarget": "/etc",
    "modifiedTime": 1786404028,
}
_LIVE_ETC = {
    "name": "etc",
    "path": "/etc",
    "size": 4096,
    "mode": 493,
    "isDir": True,
    "isSymlink": False,
    "modifiedTime": 1786404028,
}
_LIVE_ETC_HOSTNAME = {
    "name": "hostname",
    "path": "/etc/hostname",
    "size": 12,
    "mode": 420,
    "isDir": False,
    "isSymlink": False,
    "modifiedTime": 1786404028,
}

#: The guest filesystem the fake answers about. `/etc` stands in for the real one — one entry
#: where a live listing returned 121 — because it is what a symlink out of the working
#: directory reaches, and what no read or listing may ever answer with.
_GUEST_FILESYSTEM = {
    "/maf-sandbox": _LIVE_MAFSANDBOX,
    _WORK_DIR: _LIVE_WORK_DIR,
    "/maf-sandbox/work/real.txt": _LIVE_REGULAR,
    "/maf-sandbox/work/link-out.txt": _LIVE_SYMLINK,
    "/maf-sandbox/work/sub": _LIVE_DIRECTORY,
    "/maf-sandbox/work/sub/child.txt": _LIVE_NESTED,
    "/etc": _LIVE_ETC,
    "/etc/hostname": _LIVE_ETC_HOSTNAME,
}
_GUEST_CONTENTS = {
    "/maf-sandbox/work/real.txt": _REAL_CONTENT,
    "/maf-sandbox/work/sub/child.txt": b"child",
    "/etc/hostname": _HOSTNAME,
}


class _FakeDataPlaneClient:
    """The slice of the SDK's sandbox client the pull surface reaches.

    It serves the **raw** data-plane payloads rather than `FileInfo`, and its `read_file`
    follows a symlink to its target exactly as the live service does.
    """

    def __init__(self, entries=None, contents=None) -> None:
        self.sandbox_id = "sbx-1"
        self._sbx_path = _SBX_PATH
        self._api_version = _API_VERSION
        self._entries = dict(entries if entries is not None else _GUEST_FILESYSTEM)
        self._contents = dict(contents if contents is not None else _GUEST_CONTENTS)
        self.gets: list[tuple[str, dict]] = []
        self.reads: list[str] = []
        self.deletes: list[tuple[str, bool]] = []

    async def delete_file(self, path, *, recursive: bool = False) -> None:
        self.deletes.append((path, recursive))

    async def _dp_get(self, path, *, params=None):
        from azure.core.exceptions import ResourceNotFoundError

        params = dict(params or {})
        self.gets.append((path, params))
        requested = params["path"]
        if path == f"{_SBX_PATH}/files/stat":
            resolved = self._follow(requested, follow_last=False)
            entry = self._entries.get(resolved)
            if entry is None:
                raise ResourceNotFoundError(message=f"no such path: {requested}")
            # Live-verified: the service echoes the path that was asked for, so following a
            # symlinked component is invisible in the answer. Only rewritten where a follow
            # actually happened, so a test can still inject a hostile `path` of its own.
            return {**entry, "path": requested} if resolved != requested else dict(entry)
        if path == f"{_SBX_PATH}/files/list":
            target = self._follow(requested, follow_last=True)
            entry = self._entries.get(target)
            if entry is None or not entry.get("isDir"):
                raise ResourceNotFoundError(message=f"no such directory: {requested}")
            children = self._children(target)
            if target != requested:
                children = [
                    {**child, "path": posixpath.join(requested, child["name"])}
                    for child in children
                ]
            return {"path": requested, "entries": children}
        raise AssertionError(f"unexpected data-plane GET: {path}")

    async def read_file(self, path):
        from azure.core.exceptions import HttpResponseError, ResourceNotFoundError

        self.reads.append(path)
        target = self._follow(path, follow_last=True)
        entry = self._entries.get(target)
        if entry is None:
            raise ResourceNotFoundError(message=f"no such file: {path}")
        if entry.get("isDir"):
            raise HttpResponseError(message=f"{path} is a directory")
        content = self._contents.get(target)
        if content is None:
            # The guest deleted it after the stat — the SDK's own answer, not `FileNotFoundError`.
            raise ResourceNotFoundError(message=f"no such file: {path}")
        return content

    def _follow(self, path, *, follow_last):
        """Where the service actually looks, resolving a symlinked component as it goes.

        A stat describes the last component itself and follows only what is above it; a read
        and a listing follow all of them. That asymmetry is the whole leak: `stat out` reports
        the link, `stat out/hostname` reports a file inside `/etc`.
        """
        segments = [segment for segment in path.split("/") if segment]
        resolved = ""
        for index, segment in enumerate(segments):
            resolved = f"{resolved}/{segment}"
            entry = self._entries.get(resolved)
            if entry is None:
                continue
            if entry.get("isSymlink") and (follow_last or index < len(segments) - 1):
                resolved = entry["symlinkTarget"]
        return resolved

    def _children(self, directory):
        prefix = directory.rstrip("/") + "/"
        return [
            dict(payload)
            for path, payload in self._entries.items()
            if path.startswith(prefix) and "/" not in path[len(prefix) :]
        ]


def _sandbox(client=None, read_timeout: float = 30.0):
    from maf_sandbox_acas._backend import _AcasSandbox

    return _AcasSandbox(client if client is not None else _FakeDataPlaneClient(), read_timeout)


def _stat(sandbox, path, working_directory=_WORK_DIR):
    return asyncio.run(sandbox.stat_file(path, working_directory=working_directory))


class TestTheWireShape:
    """The payload this backend depends on, pinned so a change to it fails here.

    The backend reads the raw data-plane JSON because the SDK's `FileInfo` drops the fields
    the confinement rule needs (#136, upstream microsoft/azure-container-apps#1806). That is a
    dependency on an undocumented preview shape, and the point of these assertions is that the
    day it moves, the failure is a red test rather than a symlink quietly typing as a regular
    file.
    """

    def test_the_backend_maps_what_the_service_actually_sends(self):
        from maf_sandbox import EntryKind

        sandbox = _sandbox()
        assert _stat(sandbox, "real.txt").kind is EntryKind.FILE
        assert _stat(sandbox, "link-out.txt").kind is EntryKind.SYMLINK
        assert _stat(sandbox, "sub").kind is EntryKind.DIRECTORY

    def test_the_typed_fileinfo_cannot_serve_the_confinement_rule(self):
        """#136's removal gate: when this fails, go back to the typed surface and delete the raw read."""
        from azure.containerapps.sandbox import FileInfo

        symlink = FileInfo._from_dict(dict(_LIVE_SYMLINK))
        assert not hasattr(symlink, "is_symlink")
        assert not hasattr(symlink, "symlink_target")
        # `_from_dict` reads `isDirectory`, a key the service does not send, so even a
        # directory comes back through the typed surface looking like a regular file.
        assert FileInfo._from_dict(dict(_LIVE_DIRECTORY)).is_directory is False

    def test_mode_carries_permission_bits_only(self):
        """Which is why nothing parses it for a type: 0o777 is a symlink and a loose file alike."""
        import stat

        for payload in (_LIVE_REGULAR, _LIVE_SYMLINK, _LIVE_DIRECTORY):
            assert stat.S_IFMT(payload["mode"]) == 0

    def test_the_route_and_query_are_the_ones_the_service_answers(self):
        client = _FakeDataPlaneClient()
        _stat(_sandbox(client), "real.txt")

        # `/maf-sandbox` then `/maf-sandbox/work` first: a stat walks every parent of the working
        # directory before describing the entry itself.
        assert client.gets == [
            (f"{_SBX_PATH}/files/stat", {"path": "/maf-sandbox", "api-version": _API_VERSION}),
            (f"{_SBX_PATH}/files/stat", {"path": _WORK_DIR, "api-version": _API_VERSION}),
            (
                f"{_SBX_PATH}/files/stat",
                {"path": "/maf-sandbox/work/real.txt", "api-version": _API_VERSION},
            ),
        ]


class TestStatFile:
    def test_a_regular_file_carries_its_size(self):
        assert _stat(_sandbox(), "real.txt").size_bytes == len(_REAL_CONTENT)

    def test_a_symlink_size_is_not_reported_as_content(self):
        """13 is the length of `/etc/hostname`, so passing it on would be a lie about bytes."""
        assert _stat(_sandbox(), "link-out.txt").size_bytes is None

    def test_a_missing_path_is_none(self):
        assert _stat(_sandbox(), "nothing-here.txt") is None

    def test_the_entry_path_is_relative_to_the_working_directory(self):
        assert _stat(_sandbox(), "sub/child.txt").path == "sub/child.txt"

    def test_a_non_normalized_working_directory_still_resolves(self):
        assert (
            _stat(_sandbox(), "real.txt", working_directory="/maf-sandbox/work/").path == "real.txt"
        )

    def test_a_traversal_is_refused_before_the_service_is_asked(self):
        client = _FakeDataPlaneClient()
        with pytest.raises(ValueError, match="outside working directory"):
            _stat(_sandbox(client), "../etc/hostname")
        assert client.gets == []

    def test_an_absolute_path_outside_the_working_directory_is_refused(self):
        with pytest.raises(ValueError, match="outside working directory"):
            _stat(_sandbox(), "/etc/hostname")

    def test_a_backslash_is_refused_as_a_separator(self):
        """The protocol has one path grammar, and `\\` is not a separator in it."""
        with pytest.raises(ValueError, match="backslash"):
            _stat(_sandbox(), "sub\\child.txt")

    def test_a_sibling_sharing_a_prefix_is_not_read_as_a_descendant(self):
        with pytest.raises(ValueError, match="outside working directory"):
            _stat(_sandbox(), "/maf-sandbox/work2/real.txt")


class TestFailsClosedOnAMissingTypeFlag:
    """A payload that cannot say what it is, is refused — never assumed to be a regular file.

    This is the tripwire for the service changing shape under this backend, and it is the whole
    reason the confinement rule can be claimed at all: read follows symlinks here, so an entry
    of unknown type is a read of an unknown file.

    It is deliberately not a `ValueError`: `collect_outputs` reads one of those as a confinement
    failure, which would report a renamed wire field as path traversal and mask this tripwire.
    """

    def _without(self, field):
        payload = {k: v for k, v in _LIVE_REGULAR.items() if k != field}
        return _FakeDataPlaneClient(entries={"/maf-sandbox/work/real.txt": payload})

    def test_a_payload_without_the_symlink_flag_is_refused(self):
        with pytest.raises(AcasEntryPayloadIncomplete, match="isSymlink"):
            _stat(_sandbox(self._without("isSymlink")), "real.txt")

    def test_a_payload_without_the_directory_flag_is_refused(self):
        with pytest.raises(AcasEntryPayloadIncomplete, match="isDir"):
            _stat(_sandbox(self._without("isDir")), "real.txt")

    def test_a_non_boolean_flag_is_refused(self):
        """A string `"false"` is truthy, so type-checking the flag is not pedantry."""
        payload = {**_LIVE_REGULAR, "isSymlink": "false"}
        client = _FakeDataPlaneClient(entries={"/maf-sandbox/work/real.txt": payload})
        with pytest.raises(AcasEntryPayloadIncomplete, match="isSymlink"):
            _stat(_sandbox(client), "real.txt")

    def test_the_refusal_is_not_one_a_confinement_check_would_raise(self):
        """`_backend_refusals` translates `ValueError` and `OSError`; this must pass through both."""
        assert not issubclass(AcasEntryPayloadIncomplete, ValueError | OSError)

    def test_an_absent_size_is_unknown_rather_than_zero(self):
        """`None` fails closed upstream; zero would make every cap read that file as free."""
        payload = {k: v for k, v in _LIVE_REGULAR.items() if k != "size"}
        client = _FakeDataPlaneClient(entries={"/maf-sandbox/work/real.txt": payload})
        assert _stat(_sandbox(client), "real.txt").size_bytes is None

    def test_a_negative_size_is_unknown_too(self):
        """Worse than zero: it clears every cap and is then subtracted from the running total."""
        client = _FakeDataPlaneClient(
            entries={"/maf-sandbox/work/real.txt": {**_LIVE_REGULAR, "size": -1}}
        )
        assert _stat(_sandbox(client), "real.txt").size_bytes is None


class TestReadFile:
    def test_a_regular_file_comes_back_byte_identical(self):
        sandbox = _sandbox()
        content = asyncio.run(
            sandbox.read_file("real.txt", working_directory=_WORK_DIR, max_bytes=64)
        )
        assert content == _REAL_CONTENT

    def test_the_service_really_does_follow_a_symlink(self):
        """The premise of the refusal below: without it, this read leaves the working directory."""
        client = _FakeDataPlaneClient()
        assert asyncio.run(client.read_file("/maf-sandbox/work/link-out.txt")) == _HOSTNAME

    def test_a_symlink_is_refused_and_never_read(self):
        client = _FakeDataPlaneClient()
        sandbox = _sandbox(client)
        with pytest.raises(OSError, match="regular file"):
            asyncio.run(
                sandbox.read_file("link-out.txt", working_directory=_WORK_DIR, max_bytes=64)
            )
        assert client.reads == []

    def test_a_directory_is_refused(self):
        with pytest.raises(OSError, match="regular file"):
            asyncio.run(_sandbox().read_file("sub", working_directory=_WORK_DIR, max_bytes=64))

    def test_a_missing_file_raises_file_not_found(self):
        with pytest.raises(FileNotFoundError):
            asyncio.run(
                _sandbox().read_file("nothing.txt", working_directory=_WORK_DIR, max_bytes=64)
            )

    def test_a_size_over_the_cap_is_refused_before_a_byte_moves(self):
        from maf_sandbox import SandboxTransferCapExceeded

        client = _FakeDataPlaneClient()
        with pytest.raises(SandboxTransferCapExceeded):
            asyncio.run(
                _sandbox(client).read_file("real.txt", working_directory=_WORK_DIR, max_bytes=1)
            )
        assert client.reads == []

    def test_more_bytes_than_the_stat_promised_are_refused_not_truncated(self):
        """A stat is a promise about a file the guest is still free to rewrite."""
        from maf_sandbox import SandboxTransferCapExceeded

        client = _FakeDataPlaneClient(contents={"/maf-sandbox/work/real.txt": b"x" * 4096})
        with pytest.raises(SandboxTransferCapExceeded, match="read back"):
            asyncio.run(
                _sandbox(client).read_file(
                    "real.txt", working_directory=_WORK_DIR, max_bytes=len(_REAL_CONTENT)
                )
            )

    def test_an_unknown_size_is_refused(self):
        from maf_sandbox import SandboxOutputSizeUnknown

        payload = {k: v for k, v in _LIVE_REGULAR.items() if k != "size"}
        client = _FakeDataPlaneClient(entries={"/maf-sandbox/work/real.txt": payload})
        with pytest.raises(SandboxOutputSizeUnknown):
            asyncio.run(
                _sandbox(client).read_file("real.txt", working_directory=_WORK_DIR, max_bytes=64)
            )

    def test_a_negative_size_is_refused_rather_than_read(self):
        """A negative passes `size_bytes > max_bytes`, so without this the read still happens."""
        from maf_sandbox import SandboxOutputSizeUnknown

        client = _FakeDataPlaneClient(
            entries={
                **_GUEST_FILESYSTEM,
                "/maf-sandbox/work/real.txt": {**_LIVE_REGULAR, "size": -1},
            }
        )
        with pytest.raises(SandboxOutputSizeUnknown):
            asyncio.run(
                _sandbox(client).read_file("real.txt", working_directory=_WORK_DIR, max_bytes=64)
            )
        assert client.reads == []

    def test_the_guest_path_reaches_the_sdk_resolved(self):
        client = _FakeDataPlaneClient()
        asyncio.run(
            _sandbox(client).read_file(
                "./sub/../real.txt", working_directory=_WORK_DIR, max_bytes=64
            )
        )
        assert client.reads == ["/maf-sandbox/work/real.txt"]

    def test_a_nested_path_through_real_directories_still_reads(self):
        """The component walk refuses links, not depth."""
        content = asyncio.run(
            _sandbox().read_file("sub/child.txt", working_directory=_WORK_DIR, max_bytes=64)
        )
        assert content == _GUEST_CONTENTS["/maf-sandbox/work/sub/child.txt"]

    def test_a_file_that_vanishes_after_the_stat_is_a_file_not_found(self):
        """The SDK answers a late deletion with `ResourceNotFoundError`, which is no `OSError`.

        Untranslated it reaches `collect_outputs` as an azure-core type that nothing in the
        refusal family covers — see `TestThroughCollectOutputs`.
        """
        client = _FakeDataPlaneClient(contents={})
        with pytest.raises(FileNotFoundError):
            asyncio.run(
                _sandbox(client).read_file("real.txt", working_directory=_WORK_DIR, max_bytes=64)
            )
        assert client.reads == ["/maf-sandbox/work/real.txt"]


class TestASymlinkedParentEscapesLexicalConfinement:
    """`ln -sfn /etc /maf-sandbox/work/out`, the escape a lexical check cannot see.

    Verified against a live sandbox group: `stat out` is OTHER, but `stat out/hostname` is a
    regular 12-byte file, reading it returns `/etc/hostname`, and listing `out` enumerates
    `/etc`. Nothing in the final entry's payload records that a parent was a link, so
    confinement has to stat every component rather than classify the last one.
    """

    @staticmethod
    def _client():
        return _FakeDataPlaneClient(
            entries={**_GUEST_FILESYSTEM, "/maf-sandbox/work/out": _LIVE_SYMLINK_DIR}
        )

    @staticmethod
    def _stat_route(path):
        return (f"{_SBX_PATH}/files/stat", {"path": path, "api-version": _API_VERSION})

    def test_the_service_answers_from_outside_the_working_directory(self):
        """The premise of every refusal below: the path through the link resolves service-side.

        Asked through the unconfined `_stat_guest` the walk itself uses, because the public
        `stat_file` now refuses exactly this — and without the premise a refusal would also
        pass against a fake that could not reach outside in the first place.
        """
        from maf_sandbox import EntryKind

        client = self._client()
        through = asyncio.run(
            _sandbox(client)._stat_guest("/maf-sandbox/work/out/hostname", "out/hostname")
        )
        assert through.kind is EntryKind.FILE
        assert through.size_bytes == len(_HOSTNAME)
        assert asyncio.run(client.read_file("/maf-sandbox/work/out/hostname")) == _HOSTNAME

        listed = asyncio.run(
            client._dp_get(
                f"{_SBX_PATH}/files/list",
                params={"path": "/maf-sandbox/work/out", "api-version": _API_VERSION},
            )
        )
        # Live-verified: the service echoes the REQUESTED prefix, so every escaped entry looks
        # like it sits under the working directory. The per-entry check cannot fire on these —
        # the component walk is the only thing standing between a kind and /etc.
        assert [entry["path"] for entry in listed["entries"]] == ["/maf-sandbox/work/out/hostname"]

    def test_a_final_component_link_is_described_rather_than_refused(self):
        """Only the parents are refused: reporting a link as `SYMLINK` is how a caller learns."""
        from maf_sandbox import EntryKind

        assert _stat(_sandbox(self._client()), "out").kind is EntryKind.SYMLINK

    def test_a_bare_stat_through_a_symlinked_parent_is_refused(self):
        """No bytes escape, but a type and a size do — metadata from outside the boundary."""
        client = self._client()
        with pytest.raises(ValueError, match="real directory"):
            _stat(_sandbox(client), "out/hostname")
        assert client.gets == [
            self._stat_route("/maf-sandbox"),
            self._stat_route(_WORK_DIR),
            self._stat_route("/maf-sandbox/work/out"),
        ]

    def test_the_escape_is_decided_by_the_payload_flag_the_protocol_now_carries(self):
        """`isSymlink` reaches the walk as `SYMLINK`, and is still what decides.

        `OTHER` is the protocol's word for every other non-regular entry, and a non-directory
        that is not a link stays `ENOTDIR`.
        """
        from maf_sandbox import EntryKind

        sandbox = _sandbox(self._client())
        assert (
            asyncio.run(sandbox._stat_guest("/maf-sandbox/work/out", "out")).kind
            is EntryKind.SYMLINK
        )
        plain = asyncio.run(sandbox._stat_guest("/maf-sandbox/work/real.txt", "real.txt"))
        assert plain.kind is EntryKind.FILE

    def test_a_read_through_a_symlinked_parent_is_refused(self):
        client = self._client()
        with pytest.raises(ValueError, match="real directory"):
            asyncio.run(
                _sandbox(client).read_file(
                    "out/hostname", working_directory=_WORK_DIR, max_bytes=64
                )
            )
        assert client.reads == []
        assert client.gets == [
            self._stat_route("/maf-sandbox"),
            self._stat_route(_WORK_DIR),
            self._stat_route("/maf-sandbox/work/out"),
        ]

    def test_a_listing_through_a_symlinked_directory_is_refused(self):
        """The listing is never requested: the walk covers the directory named, not only its parents."""
        client = self._client()
        with pytest.raises(ValueError, match="real directory"):
            asyncio.run(_sandbox(client).list_dir("out", working_directory=_WORK_DIR))
        assert client.gets == [
            self._stat_route("/maf-sandbox"),
            self._stat_route(_WORK_DIR),
            self._stat_route("/maf-sandbox/work/out"),
        ]

    def test_a_path_through_a_regular_file_is_not_reported_as_an_escape(self):
        """`ENOTDIR` is not a confinement failure, and only a link makes it one."""
        client = self._client()
        with pytest.raises(NotADirectoryError):
            asyncio.run(
                _sandbox(client).read_file(
                    "real.txt/child", working_directory=_WORK_DIR, max_bytes=64
                )
            )


class _ConformanceSubject:
    """Plants the shared suite's hostile layout into the live-payload simulator above.

    Not `PosixGuestSubject`: this backend's `exec` goes to a real sandbox service, so there is
    no `ln` to run here. Planting writes the payloads the service would have answered with,
    which is the same fidelity the rest of this module runs at — every field copied from a live
    stat, and `_FakeDataPlaneClient` following a link exactly as the service does.
    """

    def __init__(self, client: _FakeDataPlaneClient, capabilities) -> None:
        self._client = client
        self.sandbox = _sandbox(client)
        self.working_directory = _WORK_DIR
        self.capabilities = capabilities

    def _ancestors(self, path: str) -> None:
        """Directory payloads for every parent, because the component walk stats each one."""
        walked = ""
        for segment in [s for s in posixpath.dirname(path).split("/") if s]:
            walked = f"{walked}/{segment}"
            self._client._entries.setdefault(
                walked, {**_LIVE_DIRECTORY, "name": segment, "path": walked}
            )

    async def plant_file(self, path: str, content: bytes) -> None:
        self._ancestors(path)
        self._client._entries[path] = {
            **_LIVE_REGULAR,
            "name": posixpath.basename(path),
            "path": path,
            "size": len(content),
        }
        self._client._contents[path] = content

    async def plant_symlink(self, path: str, target: str) -> None:
        self._ancestors(path)
        self._client._entries[path] = {
            **_LIVE_SYMLINK,
            "name": posixpath.basename(path),
            "path": path,
            # A live stat reports the length of the target *string*, which is why the backend
            # answers `None` for a link's size rather than passing this on.
            "size": len(target),
            "symlinkTarget": target,
        }

    async def exists(self, path: str) -> bool:
        """The simulator's own entries, which is a stat that follows nothing."""
        return path in self._client._entries

    async def plant_directory_the_guest_owns(self, path: str) -> bool:
        """No guest program here to ask, and the reach probes never reach this subject."""
        raise NotImplementedError(f"no guest here to make {path!r}")

    async def plant_file_the_guest_owns(self, path: str) -> bool:
        """No guest program here to ask, and the reach probes never reach this subject."""
        raise NotImplementedError(f"no guest here to write {path!r}")

    async def the_guest_can_delete(self, path: str) -> bool:
        """The same: nothing here removes anything on a guest's behalf."""
        raise NotImplementedError(f"no guest here to remove {path!r}")

    async def the_guest_can_write(self, path: str) -> bool:
        """No guest program here to ask, and the reach probes never reach this subject."""
        raise NotImplementedError(f"no guest here to ask about {path!r}")


class TestTheSharedConformanceSuite:
    """`maf_sandbox.conformance`, answered by this backend.

    The probes are the rule every backend serving `FILES_OUT` is held to, rather than this
    package's own reading of it, which is what its own suite would otherwise agree with (#142).

    What this leg is worth is bounded and worth stating: the specimen is a simulator built from
    payloads a live sandbox group actually answered with, not a live sandbox group. The docker
    backend runs the same probes against a real engine on every pull request, but skips the four
    requiring `FILES_LIST`, which it does not declare — so this is the only place those four are
    answered at all without a subscription.

    **A live run now exists**: `test_acas_e2e.py` puts the same probes to a real sandbox group
    (#306). It needs a subscription and a preview enrolment a pull request cannot assume (#33),
    so it runs in `verify-live.yml` rather than here, and this leg remains what a pull request
    gets — the closest available on a runner, and no more than that.
    """

    @staticmethod
    def _subject() -> _ConformanceSubject:
        from maf_sandbox import Capability

        # Empty rather than `_GUEST_FILESYSTEM`: the suite plants everything it attacks, and a
        # pre-seeded /maf-sandbox/work would let a probe pass on a file it did not put there.
        client = _FakeDataPlaneClient(entries={}, contents={})
        return _ConformanceSubject(client, frozenset({Capability.FILES_OUT, Capability.FILES_LIST}))

    def test_it_answers_every_probe(self):
        from maf_sandbox.conformance import assert_files_out_conformance

        results = asyncio.run(assert_files_out_conformance(self._subject()))
        assert [r.skipped for r in results] == [None] * len(results)

    def test_the_capabilities_it_is_probed_against_are_the_ones_it_declares(self):
        """The suite skips what a backend never claimed, so the claim has to be the real one."""
        declared = AcasSandboxBackend(_config()).declarations.capabilities
        assert self._subject().capabilities <= declared


class _Relisting(_FakeDataPlaneClient):
    """Answers every listing with one entry of the caller's choosing, whatever was asked for."""

    def __init__(self, entry) -> None:
        super().__init__()
        self._listed = entry

    async def _dp_get(self, path, *, params=None):
        payload = await super()._dp_get(path, params=params)
        if path.endswith("files/list"):
            return {**payload, "entries": [dict(self._listed)]}
        return payload


class TestListDir:
    def test_every_kind_in_one_listing_is_mapped(self):
        from maf_sandbox import EntryKind

        entries = asyncio.run(_sandbox().list_dir(".", working_directory=_WORK_DIR))
        assert {entry.path: entry.kind for entry in entries} == {
            "real.txt": EntryKind.FILE,
            "link-out.txt": EntryKind.SYMLINK,
            "sub": EntryKind.DIRECTORY,
        }

    def test_a_nested_listing_reports_paths_relative_to_the_working_directory(self):
        entries = asyncio.run(_sandbox().list_dir("sub", working_directory=_WORK_DIR))
        assert [entry.path for entry in entries] == ["sub/child.txt"]

    def test_only_regular_files_carry_a_size(self):
        entries = {
            e.path: e.size_bytes
            for e in asyncio.run(_sandbox().list_dir(".", working_directory=_WORK_DIR))
        }
        assert entries == {"real.txt": 12, "link-out.txt": None, "sub": None}

    def test_a_missing_directory_raises_file_not_found(self):
        """Translated out of the SDK's vocabulary, so a kind need not import azure-core."""
        with pytest.raises(FileNotFoundError):
            asyncio.run(_sandbox().list_dir("nowhere", working_directory=_WORK_DIR))

    def test_a_listed_entry_outside_the_working_directory_fails_the_listing(self):
        escaped = {**_LIVE_REGULAR, "path": "/etc/hostname"}
        client = _FakeDataPlaneClient(
            entries={_WORK_DIR: _LIVE_WORK_DIR, "/maf-sandbox/work/real.txt": escaped}
        )
        with pytest.raises(ValueError, match="outside working directory"):
            asyncio.run(_sandbox(client).list_dir(".", working_directory=_WORK_DIR))

    def test_a_listed_entry_without_a_type_flag_is_refused(self):
        payload = {k: v for k, v in _LIVE_REGULAR.items() if k != "isSymlink"}
        client = _FakeDataPlaneClient(
            entries={_WORK_DIR: _LIVE_WORK_DIR, "/maf-sandbox/work/real.txt": payload}
        )
        with pytest.raises(AcasEntryPayloadIncomplete, match="isSymlink"):
            asyncio.run(_sandbox(client).list_dir(".", working_directory=_WORK_DIR))

    def test_a_listing_with_no_entries_list_is_refused_rather_than_read_as_empty(self):
        """The service sends an explicit `[]` for an empty directory — verified against a live
        group — so an absent key is a changed payload, and defaulting it to empty would hide
        every declared output behind a listing that looks legitimately empty.
        """

        class _NoEntries(_FakeDataPlaneClient):
            async def _dp_get(self, path, *, params=None):
                payload = await super()._dp_get(path, params=params)
                if path.endswith("files/list"):
                    return {k: v for k, v in payload.items() if k != "entries"}
                return payload

        with pytest.raises(AcasEntryPayloadIncomplete, match="entries"):
            asyncio.run(_sandbox(_NoEntries()).list_dir(".", working_directory=_WORK_DIR))

    def test_a_listed_entry_that_is_not_an_object_is_refused(self):
        class _Scalar(_FakeDataPlaneClient):
            async def _dp_get(self, path, *, params=None):
                payload = await super()._dp_get(path, params=params)
                if path.endswith("files/list"):
                    return {**payload, "entries": ["real.txt"]}
                return payload

        with pytest.raises(AcasEntryPayloadIncomplete, match="not an"):
            asyncio.run(_sandbox(_Scalar()).list_dir(".", working_directory=_WORK_DIR))

    def test_a_listed_entry_with_no_path_is_refused_as_a_wire_shape_failure(self):
        """Where an entry sits is as load-bearing as what it is, and as absent from the payload."""
        payload = {k: v for k, v in _LIVE_REGULAR.items() if k != "path"}
        client = _FakeDataPlaneClient(
            entries={_WORK_DIR: _LIVE_WORK_DIR, "/maf-sandbox/work/real.txt": payload}
        )
        with pytest.raises(AcasEntryPayloadIncomplete, match="no 'path'"):
            asyncio.run(_sandbox(client).list_dir(".", working_directory=_WORK_DIR))

    def test_the_directory_itself_is_refused_when_it_is_outside(self):
        with pytest.raises(ValueError, match="outside working directory"):
            asyncio.run(_sandbox().list_dir("/etc", working_directory=_WORK_DIR))

    def test_a_listed_sibling_of_the_directory_is_refused(self):
        """`list_dir("sub")` enumerates one level, so `/maf-sandbox/work/real.txt` is not an answer to it."""
        with pytest.raises(AcasEntryPayloadIncomplete, match="one level"):
            asyncio.run(
                _sandbox(_Relisting(_LIVE_REGULAR)).list_dir("sub", working_directory=_WORK_DIR)
            )

    def test_a_listed_grandchild_is_refused(self):
        """Confined and under the working directory, and still not a child of what was listed."""
        with pytest.raises(AcasEntryPayloadIncomplete, match="one level"):
            asyncio.run(
                _sandbox(_Relisting(_LIVE_NESTED)).list_dir(".", working_directory=_WORK_DIR)
            )


class TestAReadThatNeverReturns:
    """A FIFO is reported exactly as an empty regular file, so only a bound can stop it."""

    def test_a_read_that_hangs_is_refused_rather_than_held_open(self):
        import asyncio as _asyncio

        class _Hangs(_FakeDataPlaneClient):
            async def read_file(self, path):
                await _asyncio.sleep(3600)

        with pytest.raises(TimeoutError, match="did not return"):
            asyncio.run(
                _sandbox(_Hangs(), read_timeout=0.05).read_file(
                    "real.txt", working_directory=_WORK_DIR, max_bytes=999
                )
            )

    def test_the_timeout_reaches_a_kind_as_an_output_failure(self):
        """`TimeoutError` is an `OSError`, so the glue already folds it into the family."""
        assert issubclass(TimeoutError, OSError)


class TestThroughCollectOutputs:
    """The surface as `maf_sandbox` actually drives it — the pair a kind depends on."""

    @staticmethod
    def _sink():
        from maf_sandbox import LandedArtifact, OutputSink

        delivered = []

        async def deliver(artifact):
            delivered.append(artifact)
            return LandedArtifact(name=artifact.name, display=artifact.name)

        return OutputSink(deliver=deliver), delivered

    @staticmethod
    def _spec(path):
        from maf_sandbox import DeclaredOutput, SandboxSpec

        return SandboxSpec(
            kind="k",
            work_dir=_WORK_DIR,
            declared_outputs=(DeclaredOutput(path=path, media_type="text/plain"),),
        )

    def test_a_declared_regular_output_lands(self):
        from maf_sandbox import collect_outputs

        sink, delivered = self._sink()
        landed = asyncio.run(collect_outputs(_sandbox(), self._spec("real.txt"), sink=sink))

        assert [artifact.name for artifact in landed] == ["real.txt"]
        assert delivered[0].content == _REAL_CONTENT

    def test_a_declared_symlink_output_is_refused_as_not_regular(self):
        from maf_sandbox import SandboxOutputNotRegular, collect_outputs

        client = _FakeDataPlaneClient()
        sink, delivered = self._sink()
        with pytest.raises(SandboxOutputNotRegular):
            asyncio.run(collect_outputs(_sandbox(client), self._spec("link-out.txt"), sink=sink))

        assert client.reads == []
        assert delivered == []

    def test_a_wire_shape_refusal_is_not_reported_as_traversal(self):
        """The glue maps a backend `ValueError` to `SandboxOutputNotConfined`. A payload that
        cannot say what an entry is has to arrive as itself, or the tripwire reads as a bad path."""
        from maf_sandbox import collect_outputs

        payload = {k: v for k, v in _LIVE_REGULAR.items() if k != "isSymlink"}
        client = _FakeDataPlaneClient(
            entries={_WORK_DIR: _LIVE_WORK_DIR, "/maf-sandbox/work/real.txt": payload}
        )
        sink, _ = self._sink()
        with pytest.raises(AcasEntryPayloadIncomplete):
            asyncio.run(collect_outputs(_sandbox(client), self._spec("real.txt"), sink=sink))

    def test_a_file_that_vanishes_after_the_stat_lands_in_the_refusal_family(self):
        """A kind must never need azure-core to catch a file the guest deleted mid-collection."""
        from maf_sandbox import SandboxOutputUnreachable, collect_outputs

        client = _FakeDataPlaneClient(contents={})
        sink, delivered = self._sink()
        with pytest.raises(SandboxOutputUnreachable):
            asyncio.run(collect_outputs(_sandbox(client), self._spec("real.txt"), sink=sink))
        assert delivered == []

    def test_a_traversing_declaration_never_reaches_the_service(self):
        """The glue settles this one from the declaration alone; the backend's own refusal
        (`TestStatFile`) is the floor under a caller that does not go through `collect_outputs`."""
        from maf_sandbox import SandboxArtifactNameInvalid, collect_outputs

        client = _FakeDataPlaneClient()
        sink, _ = self._sink()
        with pytest.raises(SandboxArtifactNameInvalid):
            asyncio.run(collect_outputs(_sandbox(client), self._spec("../etc/hostname"), sink=sink))
        assert client.gets == []


# ---------------------------------------------------------------------------
# Dependency discipline — every import must be traceable to a reason
# ---------------------------------------------------------------------------

#: A requirement string's distribution name is not always its import name: `pip install
#: agent-framework-core` puts `agent_framework` on the path, `maf-sandbox` puts
#: `maf_sandbox` on it, and `azure-identity` and `azure-containerapps-sandbox` both extend
#: the single `azure` namespace package rather than each owning a top-level name of their
#: own. Anything not listed here is assumed to import under its distribution name with
#: hyphens turned to underscores — true of every dependency any of the three maf-sandbox*
#: packages declares today. A dependency where that guess is wrong fails the test below
#: with a readable "imports X" message, which is the right place to notice a new exception
#: belongs here.
_DISTRIBUTION_TO_IMPORT_NAME = {
    "agent-framework-core": "agent_framework",
    "maf-sandbox": "maf_sandbox",
    "azure-identity": "azure",
    "azure-containerapps-sandbox": "azure",
}


def _package_modules():
    """Every module in the installed `maf_sandbox_acas`, as `{stem: path}`."""
    import pathlib

    import maf_sandbox_acas

    root = pathlib.Path(maf_sandbox_acas.__file__).parent  # type: ignore[arg-type]
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


def _declared_import_names():
    """The import names `pyproject.toml` licenses `maf_sandbox_acas` to reach for, or `None`.

    `None` means there is no `pyproject.toml` next to the installed package — an
    sdist/wheel-only install with no source tree alongside it — and the caller must skip
    rather than let an empty dependency list pass the scan below vacuously.
    """
    import pathlib
    import re
    import tomllib

    import maf_sandbox_acas

    root = pathlib.Path(maf_sandbox_acas.__file__).parents[2]  # type: ignore[arg-type]
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
        assert len(_package_modules()) >= 4

    def test_every_module_only_imports_what_it_is_declared_to_need(self):
        import sys

        declared = _declared_import_names()
        if declared is None:
            pytest.skip(
                "pyproject.toml is not next to the installed maf_sandbox_acas package — "
                "this check only runs against a source checkout, not an installed-only wheel"
            )

        allowed = set(sys.stdlib_module_names) | declared | {"maf_sandbox_acas"}
        offenders = [
            f"{path.name}: import {name}"
            for _, path in sorted(_package_modules().items())
            for name in _imported_top_levels(path)
            if name not in allowed
        ]
        assert offenders == [], (
            f"these maf_sandbox_acas modules import something outside the standard library, "
            f"the package itself, and pyproject.toml's declared dependencies: {offenders}. "
            "Either the import is a mistake, or the dependency belongs in pyproject.toml."
        )


class TestRunCode:
    """This backend declares no RUN_CODE, and says why rather than failing bare."""

    def test_run_code_raises_notimplementederror(self):
        """Not for want of an interpreter: the sandbox group's image may well carry one. The
        backend resolves an image reference without looking inside it, so declaring the
        capability would be a claim about an artefact it does not own."""
        with pytest.raises(NotImplementedError, match="RUN_CODE"):
            asyncio.run(_sandbox().run_code("print(1)", timeout=5.0))


class _GuestRemovalClient(_FakeDataPlaneClient):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.commands = []
        self.exec_raises: Exception | None = None
        self.answer = _GuestAnswer()
        self.removes = True

    async def exec(self, command, *, working_directory):
        self.commands.append((command, working_directory))
        if self.exec_raises is not None:
            raise self.exec_raises
        if self.removes:
            target = shlex.split(command)[-1]
            for name in list(self._entries):
                if name == target or name.startswith(target + "/"):
                    self._entries.pop(name)
        return self.answer


class TestRemove:
    """Removal stays on guest exec, with service observation of its outcome."""

    @pytest.mark.parametrize(
        ("path", "recursive", "flags"),
        [("sub", True, "-rf"), ("real.txt", False, "-f"), ("link-out.txt", False, "-f")],
    )
    def test_removal_runs_as_the_guest(self, path, recursive, flags):
        client = _GuestRemovalClient()
        asyncio.run(_sandbox(client).remove(path, working_directory=_WORK_DIR, recursive=recursive))
        assert client.commands == [(f"rm {flags} -- {_WORK_DIR}/{path}", "/")]
        assert client.deletes == []

    def test_a_directory_is_refused_without_recursive(self):
        client = _GuestRemovalClient()
        with pytest.raises(OSError):
            asyncio.run(_sandbox(client).remove("sub", working_directory=_WORK_DIR))
        assert client.commands == client.deletes == []

    def test_a_final_link_to_a_directory_is_passed_without_a_trailing_slash(self):
        client = _GuestRemovalClient(
            entries={**_GUEST_FILESYSTEM, "/maf-sandbox/work/out": _LIVE_SYMLINK_DIR}
        )
        asyncio.run(_sandbox(client).remove("out/", working_directory=_WORK_DIR, recursive=True))
        assert client.commands == [(f"rm -rf -- {_WORK_DIR}/out", "/")]
        assert client.deletes == []

    @pytest.mark.parametrize("path", [".", "../escape", "link-out.txt/child"])
    def test_a_refused_path_never_reaches_exec(self, path):
        client = _GuestRemovalClient()
        with pytest.raises(ValueError):
            asyncio.run(_sandbox(client).remove(path, working_directory=_WORK_DIR, recursive=True))
        assert client.commands == client.deletes == []

    def test_a_path_that_is_not_there_needs_no_command(self):
        client = _GuestRemovalClient()
        asyncio.run(_sandbox(client).remove("missing", working_directory=_WORK_DIR))
        assert client.commands == client.deletes == []

    def test_a_service_failure_arrives_as_oserror(self):
        client = _GuestRemovalClient()
        client.exec_raises = _HttpError()
        with pytest.raises(OSError) as failure:
            asyncio.run(_sandbox(client).remove("real.txt", working_directory=_WORK_DIR))
        assert failure.value.__cause__ is client.exec_raises
        assert client.deletes == []

    @pytest.mark.parametrize("failure_kind", ["service", "transport"])
    @pytest.mark.parametrize("failed_path", ["/maf-sandbox", f"{_WORK_DIR}/real.txt"])
    def test_preflight_provider_failures_arrive_as_oserror(
        self, monkeypatch, failure_kind, failed_path
    ):
        from azure.core.exceptions import HttpResponseError, ServiceRequestError

        client = _GuestRemovalClient()
        original = client._dp_get
        provider_failure = (
            HttpResponseError("stat refused")
            if failure_kind == "service"
            else ServiceRequestError("stat connection failed")
        )

        async def stat(path, *, params):
            if params["path"] == failed_path:
                raise provider_failure
            return await original(path, params=params)

        monkeypatch.setattr(client, "_dp_get", stat)
        with pytest.raises(OSError) as failure:
            asyncio.run(_sandbox(client).remove("real.txt", working_directory=_WORK_DIR))
        assert failure.value.__cause__ is provider_failure
        assert client.commands == client.deletes == []

    @pytest.mark.parametrize("diagnostic", ["", "Permission denied\n", "rm: not found\n"])
    def test_a_failed_command_is_reported_even_if_the_entry_disappeared(self, diagnostic):
        client = _GuestRemovalClient()
        client.answer = _GuestAnswer(exit_code=1, stderr=diagnostic)
        with pytest.raises(OSError, match="exited 1") as failure:
            asyncio.run(_sandbox(client).remove("real.txt", working_directory=_WORK_DIR))
        assert diagnostic.strip() in str(failure.value)
        assert client.deletes == []

    def test_a_successful_command_that_leaves_the_entry_is_refused(self):
        client = _GuestRemovalClient()
        client.removes = False
        with pytest.raises(OSError, match="still reports the entry"):
            asyncio.run(_sandbox(client).remove("real.txt", working_directory=_WORK_DIR))
        assert client.deletes == []

    @pytest.mark.parametrize("phase", ["exec", "observation"])
    def test_the_command_and_its_observation_are_bounded(self, monkeypatch, phase):
        client = _GuestRemovalClient()
        original = client._dp_get

        async def hang(*args, **kwargs):
            await asyncio.Event().wait()

        async def observe(*args, **kwargs):
            if client.commands:
                await hang()
            return await original(*args, **kwargs)

        monkeypatch.setattr(
            client, "exec" if phase == "exec" else "_dp_get", hang if phase == "exec" else observe
        )
        with pytest.raises(TimeoutError):
            asyncio.run(
                _sandbox(client, read_timeout=0.01).remove("real.txt", working_directory=_WORK_DIR)
            )
        assert client.deletes == []

    def test_an_unreadable_observation_is_not_success(self, monkeypatch):
        client = _GuestRemovalClient()
        original = client._dp_get

        async def observe(*args, **kwargs):
            if client.commands:
                raise _HttpError()
            return await original(*args, **kwargs)

        monkeypatch.setattr(client, "_dp_get", observe)
        with pytest.raises(OSError):
            asyncio.run(_sandbox(client).remove("real.txt", working_directory=_WORK_DIR))
        assert client.deletes == []

    def test_cancellation_never_falls_back_to_the_host_plane(self, monkeypatch):
        client = _GuestRemovalClient()

        async def cancel(*args, **kwargs):
            raise asyncio.CancelledError

        monkeypatch.setattr(client, "exec", cancel)
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(_sandbox(client).remove("real.txt", working_directory=_WORK_DIR))
        assert client.deletes == []

    def test_a_hostile_name_is_one_quoted_argument(self):
        path = "-x; $(id) 'quoted'"
        client = _GuestRemovalClient(
            entries={**_GUEST_FILESYSTEM, f"{_WORK_DIR}/{path}": _LIVE_REGULAR}
        )
        asyncio.run(_sandbox(client).remove(path, working_directory=_WORK_DIR))
        assert shlex.split(client.commands[0][0]) == ["rm", "-f", "--", f"{_WORK_DIR}/{path}"]
        assert client.deletes == []


class TestReclaim:
    @pytest.mark.parametrize(
        "directory",
        ["/maf-sandbox/work/call/", "/tmp/linked/call", "/", "relative"],
    )
    def test_reclaim_refuses_without_accessing_the_service(self, directory):
        accesses: list[str] = []

        class _NoClientCalls:
            def __getattr__(self, name):
                accesses.append(name)
                raise AttributeError(f"reclaim accessed the service client: {name}")

        sandbox = _sandbox(_NoClientCalls())
        with pytest.raises(NotImplementedError, match="RECLAIM.*Dispose the sandbox"):
            asyncio.run(sandbox.reclaim(directory, working_directory=_WORK_DIR, timeout=30))
        assert accesses == []

    @pytest.mark.parametrize("confined", [False, True])
    @pytest.mark.parametrize("floor", list(Cleanup))
    def test_every_workload_resolves_to_disposal(self, confined, floor):
        backend = AcasSandboxBackend(_config())
        router = SandboxRouter([backend], min_cleanup=floor)
        spec = SandboxSpec(kind="test", confined_to_guest_call_path=confined)
        assert Capability.RECLAIM not in backend.declarations.capabilities
        assert Capability.SNAPSHOT not in backend.declarations.capabilities
        assert router.effective_cleanup(spec) is Cleanup.DISPOSE


@pytest.mark.parametrize("override", [None, "/image/base"])
def test_relative_storage_contract_through_the_data_plane(override):
    from dataclasses import replace

    from maf_sandbox.conformance import assert_storage_base_conformance

    async def scenario():
        client = _GuestGroupClient(_guest_removing(True))
        backend = _backend_with(client)
        spec = replace(_spec_requiring(Capability.FILES_OUT), work_dir=override)
        sandbox = await backend.acquire(SandboxKey("storage", "thread", "agent"), spec)
        capabilities = backend.declarations.capabilities - {Capability.EXEC}
        await assert_storage_base_conformance(sandbox, capabilities)

    asyncio.run(scenario())


@pytest.mark.parametrize("override", [None, "/image/base"])
def test_warm_storage_binding_refuses_retargeting(override):
    from dataclasses import replace

    async def scenario():
        client = _GuestGroupClient(_guest_removing(True))
        backend = _backend_with(client)
        key = SandboxKey("storage", "thread", "agent")
        spec = replace(_spec_requiring(Capability.FILES_OUT), work_dir=override)
        first = await backend.acquire(key, spec)
        await first.write_file("keep", b"keep", working_directory=".")
        before = list(client.created_directories)
        with pytest.raises(ValueError, match="storage base"):
            await backend.acquire(key, replace(spec, work_dir="/other/base"))
        assert client.created_directories == before
        again = await backend.acquire(key, spec)
        assert again.instance_id == first.instance_id
        assert await again.read_file("keep", working_directory=".", max_bytes=100) == b"keep"

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# The isolation scope — one sandbox per call, declared and keyed (#436)
# ---------------------------------------------------------------------------


def _entry(key: SandboxKey, kind: str) -> tuple[str, str, str, str, str]:
    """The registry tuple a key and kind are filed under, call included."""
    return (key.scope, key.thread_id, key.agent_dir, key.call_id, kind)


class TestTheIsolationScope:
    """That a key naming a call is a different sandbox, and is disposed on its own.

    This backend derives no name — the service mints the id — so the whole of its call-scope
    identity is the registry entry it files a sandbox under and the label it creates it with.
    """

    _KEY = SandboxKey(scope="scope-a", thread_id="thread-1", agent_dir="devops-engineer")
    _CALL_A = replace(_KEY, call_id="call-a")
    _CALL_B = replace(_KEY, call_id="call-b")
    _SPEC = SandboxSpec(kind="bicep", image="mcr.example/bicep:1")

    def test_declares_both_scopes(self):
        scopes = AcasSandboxBackend(_config()).declarations.isolation_scopes
        assert scopes == frozenset({IsolationScope.CONVERSATION, IsolationScope.CALL})

    def test_a_conversation_sandbox_carries_no_call_label(self):
        """Absence is what keeps the label selector reaching sandboxes an earlier release
        created, which carry the four labels this one still writes and nothing more.
        """
        assert "call" not in _sandbox_labels(self._KEY, self._SPEC)

    def test_a_call_scoped_sandbox_is_labelled_with_its_call(self):
        assert _sandbox_labels(self._CALL_A, self._SPEC)["call"] == "call-a"

    def test_a_call_scoped_disposal_selects_on_the_call(self):
        client = _FakeGroupClient()
        backend = _backend_with(client)
        asyncio.run(backend.dispose(self._CALL_A, kind="bicep"))
        assert client.last_labels is not None
        assert client.last_labels["call"] == "call-a"

    def test_a_conversation_disposal_does_not_filter_on_a_call(self):
        """A conversation's key adds no call filter, so it keeps reaching the sandboxes a
        release before this one labelled with four labels and no fifth.
        """
        client = _FakeGroupClient()
        backend = _backend_with(client)
        asyncio.run(backend.dispose(self._KEY, kind="bicep"))
        assert client.last_labels is not None
        assert "call" not in client.last_labels

    def test_two_calls_are_two_registry_entries(self):
        """Get-or-create reads the registry, so two calls that file separately are never
        handed each other's warm sandbox.
        """
        backend = _backend_with(_FakeGroupClient())
        held = _Held("sbx-a", egress=(Egress.CLOSED, frozenset()))
        backend._registry[_entry(self._CALL_A, "bicep")] = held
        backend._registry[_entry(self._CALL_B, "bicep")] = replace(held, sandbox_id="sbx-b")
        assert len(backend._registry) == 2

    def test_disposing_one_call_leaves_the_other_calls_sandbox(self):
        """`assert_call_scope_conformance`'s last probe: ending one call must not delete the
        sandbox of the sibling call still running beside it.
        """
        client = _FakeGroupClient()
        backend = _backend_with(client)
        held = _Held("sbx-a", egress=(Egress.CLOSED, frozenset()))
        backend._registry[_entry(self._CALL_A, "bicep")] = held
        backend._registry[_entry(self._CALL_B, "bicep")] = replace(held, sandbox_id="sbx-b")

        assert asyncio.run(backend.dispose(self._CALL_A, kind="bicep")) is None
        assert client.deleted == ["sbx-a"]
        assert {key[3] for key in backend._registry} == {"call-b"}

    def test_the_purge_selects_on_scope_and_thread_and_not_on_the_call(self):
        """The documented backstop: a per-call delete that does not land leaves a sandbox no
        later call can address, and the purge's labels are what still reach it.
        """
        client = _FakeGroupClient()
        backend = _backend_with(client)
        held = _Held("sbx-a", egress=(Egress.CLOSED, frozenset()))
        backend._registry[_entry(self._CALL_A, "bicep")] = held

        asyncio.run(backend.dispose_scope(self._KEY.scope, self._KEY.thread_id))
        assert client.deleted == ["sbx-a"]
        assert client.last_labels is not None
        assert "call" not in client.last_labels
