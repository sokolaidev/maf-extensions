"""Offline tests for the ACA Sandboxes backend (issues #408, #663).

No live sandbox group and no host application: the group client is replaced by a fake, and
the disk-image tests build the **real** SDK dataclasses so the shape they assert is the
SDK's rather than one the code and the fake happen to agree on.
"""

from __future__ import annotations

import asyncio

import pytest
from maf_aca_sandboxes import AcaConfig, AcaSandboxBackend, disk_image_base, resolve_disk_image_id
from sandbox_router import Isolation, SandboxBackend, SandboxKey

_ENDPOINT = "https://management.example.azuredevcompute.io"


def _config(**overrides) -> AcaConfig:
    return AcaConfig(endpoint=_ENDPOINT, **overrides)


def _disk_image(image_id: str, reference: str):
    """A listed disk image, built from the real SDK dataclasses.

    Deliberately not hand-rolled: ``DiskImage.image`` is a ``DiskImageSpec`` whose ``base``
    carries the reference, and an earlier fake that set ``.image`` to a plain string made
    every resolution test pass against a resolver that could never match a real listing.
    """
    from azure.containerapps.sandbox import DiskImage, DiskImageSpec

    return DiskImage(id=image_id, image=DiskImageSpec(base=reference))


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


class _FakeSandboxClient:
    def __init__(self, sandbox_id: str) -> None:
        self.sandbox_id = sandbox_id
        self.deleted = False

    async def begin_delete(self) -> None:
        self.deleted = True


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


def _backend_with(group_client, config: AcaConfig | None = None) -> AcaSandboxBackend:
    """A backend whose group client is the given fake.

    Injected by overriding the one protected accessor rather than by patching
    ``sys.modules``: the seam exists precisely so the backend can be exercised without
    Azure, and using it here is what proves it is a real seam.
    """
    backend = AcaSandboxBackend(config or _config())
    backend._group_client = lambda: group_client  # type: ignore[method-assign]
    return backend


# ---------------------------------------------------------------------------
# Backend identity — read by the router's deployed check
# ---------------------------------------------------------------------------


class TestBackendIdentity:
    def test_satisfies_the_backend_protocol(self):
        assert isinstance(AcaSandboxBackend(_config()), SandboxBackend)

    def test_declares_vm_isolation(self):
        """The router permits this backend in a deployed environment because of this value."""
        assert AcaSandboxBackend(_config()).isolation == Isolation.VM

    def test_is_named_aca(self):
        assert AcaSandboxBackend(_config()).name == "aca"


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
        from maf_aca_sandboxes._images import qualify_image_reference

        assert qualify_image_reference("acr.azurecr.io", "ats-bicep-sandbox:0.46.1") == (
            "acr.azurecr.io/ats-bicep-sandbox:0.46.1"
        )

    def test_a_tag_colon_is_not_mistaken_for_a_port(self):
        """`ats-bicep-sandbox:0.46.1` has a colon but no registry — the trap in this rule."""
        from maf_aca_sandboxes._images import qualify_image_reference

        assert qualify_image_reference("acr.azurecr.io", "img:1.2.3").startswith("acr.azurecr.io/")

    def test_leaves_an_already_qualified_reference_alone(self):
        """Double-prefixing surfaces only as "no disk image was built from …", far away."""
        from maf_aca_sandboxes._images import qualify_image_reference

        assert qualify_image_reference("acr.azurecr.io", "other.azurecr.io/img:1") == (
            "other.azurecr.io/img:1"
        )

    def test_a_repository_path_is_not_a_registry(self):
        from maf_aca_sandboxes._images import qualify_image_reference

        assert qualify_image_reference("acr.azurecr.io", "library/ubuntu:22.04") == (
            "acr.azurecr.io/library/ubuntu:22.04"
        )

    def test_localhost_and_ports_count_as_registries(self):
        from maf_aca_sandboxes._images import qualify_image_reference

        assert qualify_image_reference("acr.io", "localhost/img:1") == "localhost/img:1"
        assert qualify_image_reference("acr.io", "reg:5000/img:1") == "reg:5000/img:1"

    def test_no_registry_configured_leaves_the_image_untouched(self):
        from maf_aca_sandboxes._images import qualify_image_reference

        assert qualify_image_reference("", "img:1") == "img:1"

    def test_a_trailing_slash_on_the_registry_does_not_double_up(self):
        from maf_aca_sandboxes._images import qualify_image_reference

        assert qualify_image_reference("acr.azurecr.io/", "img:1") == "acr.azurecr.io/img:1"


class TestResolveDiskImageId:
    def setup_method(self):
        from maf_aca_sandboxes._images import _disk_image_cache

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

    def test_raises_when_nothing_configured(self):
        with pytest.raises(ValueError, match="No sandbox image is configured"):
            asyncio.run(resolve_disk_image_id(_FakeGroupClient(), None, None))

    def test_raises_when_reference_not_imported(self):
        client = _FakeGroupClient(images=[_disk_image("img-1", "acr.io/other:1")])
        with pytest.raises(ValueError, match="import_disk_image"):
            asyncio.run(resolve_disk_image_id(client, None, "acr.io/x:1"))


# ---------------------------------------------------------------------------
# dispose_scope — cross-replica purge
# ---------------------------------------------------------------------------


class TestDisposeScope:
    def test_reaches_sandboxes_this_process_never_created(self):
        """The registry is a fast path; the service is the source of truth (issue #375)."""
        client = _FakeGroupClient(sandboxes=[_FakeSandbox("sbx-remote")])
        backend = _backend_with(client)

        assert asyncio.run(backend.dispose_scope("scope-a", "thread-1")) == 1
        assert client.deleted == ["sbx-remote"]
        assert client.last_labels == {"scope": "scope-a", "thread": "thread-1"}

    def test_unions_the_registry_with_the_service_listing(self):
        client = _FakeGroupClient(sandboxes=[_FakeSandbox("sbx-remote")])
        backend = _backend_with(client)
        backend._registry[("scope-a", "thread-1", "devops-engineer")] = "sbx-local"

        assert asyncio.run(backend.dispose_scope("scope-a", "thread-1")) == 2
        assert sorted(client.deleted) == ["sbx-local", "sbx-remote"]

    def test_does_not_delete_another_scopes_sandbox(self):
        client = _FakeGroupClient()
        backend = _backend_with(client)
        backend._registry[("scope-b", "thread-1", "devops-engineer")] = "sbx-other"

        assert asyncio.run(backend.dispose_scope("scope-a", "thread-1")) == 0
        assert client.deleted == []
        assert ("scope-b", "thread-1", "devops-engineer") in backend._registry

    def test_registry_entries_are_dropped_even_when_the_delete_fails(self):
        """A stale entry is worse than none — the next acquire would try to resume it."""
        backend = _backend_with(_ExplodingGroupClient())
        backend._registry[("scope-a", "thread-1", "devops-engineer")] = "sbx-local"

        asyncio.run(backend.dispose_scope("scope-a", "thread-1"))
        assert backend._registry == {}

    def test_a_service_failure_degrades_to_zero_rather_than_raising(self):
        """Purge must not fail a conversation delete."""
        backend = _backend_with(_ExplodingGroupClient())
        assert asyncio.run(backend.dispose_scope("scope-a", "thread-1")) == 0


class TestFileWrites:
    """A workload may hand the backend a nested path, so parents must be created.

    `infra/main.bicep` is the example in the bicep tool's own description, and the file API
    docs say nothing about whether a write creates missing parents — only the SDK signature
    does (`create_dirs: bool = True`). Since that is a `0.1.0bN` default doing load-bearing
    work, the backend passes it explicitly and this pins that it does.
    """

    def test_requests_parent_directory_creation(self):
        from maf_aca_sandboxes._backend import _AcaSandbox

        class _RecordingClient:
            def __init__(self) -> None:
                self.calls: list[tuple] = []

            async def write_file(self, path, content, **kwargs):
                self.calls.append((path, content, kwargs))

        client = _RecordingClient()
        asyncio.run(_AcaSandbox(client).write_file("/work/infra/main.bicep", "param x string"))

        assert client.calls == [("/work/infra/main.bicep", "param x string", {"create_dirs": True})]


class TestDispose:
    def test_deletes_the_keyed_sandbox_and_forgets_it(self):
        client = _FakeGroupClient()
        backend = _backend_with(client)
        key = SandboxKey(scope="scope-a", thread_id="thread-1", agent_dir="devops-engineer")
        backend._registry[(key.scope, key.thread_id, key.agent_dir)] = "sbx-1"

        asyncio.run(backend.dispose(key))
        assert client.deleted == ["sbx-1"]
        assert backend._registry == {}

    def test_is_a_no_op_for_an_unknown_key(self):
        client = _FakeGroupClient()
        backend = _backend_with(client)
        key = SandboxKey(scope="scope-a", thread_id="thread-1", agent_dir="devops-engineer")

        asyncio.run(backend.dispose(key))
        assert client.deleted == []


# ---------------------------------------------------------------------------
# Egress policy — built from the spec, not from configuration
# ---------------------------------------------------------------------------


class TestEgressPolicy:
    def test_denies_by_default_and_allows_only_the_named_hosts(self):
        from sandbox_router import SandboxSpec

        backend = AcaSandboxBackend(_config())
        policy = backend._egress_policy(SandboxSpec(kind="t", egress_allow=("mcr.microsoft.com",)))

        assert policy.default_action == "Deny"
        assert [r.pattern for r in policy.host_rules] == ["mcr.microsoft.com"]
        assert [r.action for r in policy.host_rules] == ["Allow"]

    def test_an_empty_allowlist_means_no_network(self):
        from sandbox_router import SandboxSpec

        backend = AcaSandboxBackend(_config())
        policy = backend._egress_policy(SandboxSpec(kind="t"))

        assert policy.default_action == "Deny"
        assert policy.host_rules == []
